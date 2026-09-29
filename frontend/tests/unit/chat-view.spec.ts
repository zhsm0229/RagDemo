/**
 * P2 问答工作台集成测试（mock 全局 fetch：/chat/stream 返回真 ReadableStream 字节流，
 * 会话/历史端点返回统一信封——全链路走真 client.ts + parseSseFrames + streamChat）。
 * 覆盖：事件时序（citations 先于 token）、done 分支（refused/cacheHit/用量）、
 * 断流重试（CHAT-12）、中途网络断流可重试与 SSE 健壮性（T8 评审清单①②）、
 * 发送中停止（清单③ AbortController）、引用角标↔面板联动、原文核对弹窗三态、
 * 点赞/点踩反馈（前端校验拦截 + 422 兜底内联）、字数校验、会话删除二次确认、
 * 历史渲染（中断占位/已反馈态）、Enter/Shift+Enter、首问自动建会话。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MessageBoxData } from 'element-plus'
import type { CitationContent, CitationItem } from '@/api/types'
import type { MessageItem } from '@/api/chat'
import ChatView from '@/views/ChatView.vue'

const fetchMock = vi.fn()

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  // jsdom 未实现 scrollIntoView（角标→面板滚动定位用）
  Element.prototype.scrollIntoView = vi.fn()
  resetBackend()
  fetchMock.mockImplementation(routeFetch)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

// ---- mock 后端状态与路由 ----

interface DemoConv {
  id: number
  title: string
  updatedAt: string
}

const T0 = '2026-09-28T14:20:00+08:00'
let conversations: DemoConv[]
let history: Map<number, MessageItem[]>
/** 引用正文库：key 为 `${refType}:${refId}`，缺省 404/3003 */
let citationContents: Map<string, CitationContent>
let streamQueue: Array<StreamPart[] | Response>
let feedbackCalls: Array<{ id: number; body: Record<string, unknown> }>
let feedbackError: { code: number; message: string } | null
let nextConvId: number
let onStream: (() => void) | null

function resetBackend(): void {
  conversations = []
  history = new Map()
  citationContents = new Map()
  streamQueue = []
  feedbackCalls = []
  feedbackError = null
  nextConvId = 100
  onStream = null
}

async function routeFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const method = init.method ?? 'GET'
  if (url === '/api/chat/stream') {
    onStream?.()
    const next = streamQueue.shift()
    if (next === undefined) throw new Error('no queued stream response')
    if (Array.isArray(next)) {
      return sseResponse(next, (init.signal as AbortSignal | null) ?? undefined)
    }
    return next
  }
  if (url === '/api/conversations' && method === 'POST') {
    const id = ++nextConvId
    conversations = [{ id, title: '新会话', updatedAt: T0 }, ...conversations]
    return okEnvelope({ id })
  }
  if (url.startsWith('/api/conversations?')) {
    return okEnvelope({ items: conversations, total: conversations.length, page: 1, pageSize: 50 })
  }
  const deleted = /^\/api\/conversations\/(\d+)$/.exec(url)
  if (deleted !== null && method === 'DELETE') {
    const id = Number(deleted[1])
    conversations = conversations.filter((c) => c.id !== id)
    return okEnvelope({ id, isActive: false })
  }
  const msgs = /^\/api\/conversations\/(\d+)\/messages$/.exec(url)
  if (msgs !== null) return okEnvelope({ items: history.get(Number(msgs[1])) ?? [] })
  const content = /^\/api\/citations\/content\?refType=(doc|qa)&refId=(\d+)$/.exec(url)
  if (content !== null) {
    const refType = content[1] as 'doc' | 'qa'
    const found = citationContents.get(`${refType}:${content[2]}`)
    if (found === undefined) {
      return makeResponse(404, 'application/json', null, {
        code: 3003,
        message: '引用不存在或无权访问',
        data: null,
      })
    }
    return okEnvelope(found)
  }
  const feedback = /^\/api\/messages\/(\d+)\/feedback$/.exec(url)
  if (feedback !== null && method === 'POST') {
    const id = Number(feedback[1])
    const body = JSON.parse(String(init.body)) as Record<string, unknown>
    feedbackCalls.push({ id, body })
    if (feedbackError !== null) {
      return makeResponse(422, 'application/json', null, {
        code: feedbackError.code,
        message: feedbackError.message,
        data: null,
      })
    }
    const down = body.feedback === 'down'
    return okEnvelope({
      id,
      feedback: body.feedback,
      feedbackCategory: down ? (body.category ?? null) : null,
      feedbackComment: down ? (body.comment ?? null) : null,
      badcaseStatus: down ? '待处理' : null,
    })
  }
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

// ---- 响应构造 ----

class Gate {
  private resolveFn: () => void = () => undefined
  readonly promise: Promise<void> = new Promise<void>((resolve) => {
    this.resolveFn = resolve
  })
  open(): void {
    this.resolveFn()
  }
}

function makeResponse(
  status: number,
  contentType: string,
  body: ReadableStream<Uint8Array> | null,
  json?: unknown,
): Response {
  return {
    ok: status < 400,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? contentType : null),
    },
    body,
    json:
      json === undefined
        ? async () => {
            throw new Error('no json body')
          }
        : async () => json,
  } as unknown as Response
}

/** SSE 字节流段：text 入队；gate 控制到达时机；crash 模拟 read 中途网络断流（清单①） */
interface StreamPart {
  text?: string
  gate?: Gate
  crash?: 'network'
}

/** SSE 字节流响应：支持外部 signal（停止→AbortError）与 crash（TypeError 网络断流） */
function sseResponse(parts: StreamPart[], signal?: AbortSignal): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    async start(controller) {
      let errored = false
      const abortError = (): DOMException =>
        new DOMException('The user aborted a request.', 'AbortError')
      const fail = (err: unknown): void => {
        if (errored) return
        errored = true
        controller.error(err)
      }
      signal?.addEventListener('abort', () => fail(abortError()))
      try {
        for (const part of parts) {
          if (part.gate !== undefined) await part.gate.promise
          if (signal?.aborted) {
            fail(abortError())
            return
          }
          if (part.text !== undefined) controller.enqueue(encoder.encode(part.text))
          if (part.crash === 'network') throw new TypeError('read ECONNRESET')
        }
        controller.close()
      } catch (error) {
        fail(error)
      }
    },
  })
  return makeResponse(200, 'text/event-stream', body)
}

function okEnvelope(data: unknown): Response {
  return makeResponse(200, 'application/json', null, { code: 0, message: 'ok', data })
}

// ---- SSE 帧构造（docs/api.md §3.1 事件 DTO） ----

const frame = (event: string, data: unknown): string =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
const citationsFrame = (items: unknown[]): string => frame('citations', { messageId: 101, items })
const tokenFrame = (content: string): string => frame('token', { messageId: 101, content })
const doneFrame = (extra: Record<string, unknown> = {}): string =>
  frame('done', {
    messageId: 101,
    refused: false,
    cacheHit: false,
    promptTokens: 12,
    completionTokens: 34,
    durationMs: 56,
    ...extra,
  })
const errorFrame = (): string =>
  frame('error', { code: 5001, message: '生成服务暂时不可用', retryable: true })

// ---- 数据构造 ----

const CITE: CitationItem[] = [
  {
    n: 1,
    refType: 'doc',
    refId: 11,
    title: '客服手册',
    page: 3,
    titlePath: '第3章>3.2',
    score: 0.91,
  },
]

const CITES: CitationItem[] = [
  ...CITE,
  { n: 2, refType: 'qa', refId: 7, title: 'FAQ问答对', page: null, titlePath: null, score: 0.88 },
]

function conv(id: number, title: string): DemoConv {
  return { id, title, updatedAt: T0 }
}

function userItem(id: number, content: string): MessageItem {
  return {
    id,
    role: 'user',
    content,
    citations: null,
    feedback: null,
    feedbackCategory: null,
    feedbackComment: null,
    answerType: null,
    cacheHit: false,
    createdAt: T0,
  }
}

function assistantItem(
  id: number,
  content: string,
  citations: CitationItem[] = [],
  answerType = 'rag',
  cacheHit = false,
  feedback: 'up' | 'down' | null = null,
  feedbackCategory: string | null = null,
): MessageItem {
  return {
    id,
    role: 'assistant',
    content,
    citations,
    feedback,
    feedbackCategory,
    feedbackComment: feedback === 'down' ? '引用与问题无关' : null,
    answerType,
    cacheHit,
    createdAt: T0,
  }
}

/** 登记引用正文（不登记 → 该引用查询得 404/3003） */
function giveContent(refType: 'doc' | 'qa', refId: number, content: string): void {
  citationContents.set(`${refType}:${refId}`, {
    refType,
    refId,
    content,
    title: refType === 'doc' ? '客服手册' : 'FAQ问答对',
    page: refType === 'doc' ? 3 : null,
    titlePath: refType === 'doc' ? '第3章>3.2' : null,
  })
}

// ---- 断言/操作辅助 ----

function streamCalls(): Array<[string, RequestInit]> {
  return fetchMock.mock.calls.filter(
    (call): call is [string, RequestInit] => call[0] === '/api/chat/stream',
  )
}

function deleteCalls(): Array<[string, RequestInit]> {
  return fetchMock.mock.calls.filter(
    (call) => typeof call[0] === 'string' && /^\/api\/conversations\/\d+$/.test(call[0]),
  ) as Array<[string, RequestInit]>
}

function contentCalls(): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes('/api/citations/content'))
}

async function waitFor(cond: () => boolean, label: string): Promise<void> {
  const deadline = Date.now() + 4000
  while (!cond()) {
    if (Date.now() > deadline) throw new Error(`waitFor timeout: ${label}`)
    await new Promise((resolve) => setTimeout(resolve, 10))
    await flushPromises()
  }
}

function sendButton(wrapper: VueWrapper) {
  const button = wrapper.findAll('button').find((b) => b.text().includes('发送'))
  expect(button, '发送按钮应存在').toBeDefined()
  return button!
}

async function ask(wrapper: VueWrapper, question: string): Promise<void> {
  await wrapper.find('textarea').setValue(question)
  await sendButton(wrapper).trigger('click')
  await flushPromises()
}

async function mountChat(): Promise<VueWrapper> {
  const wrapper = mount(ChatView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

// ---- 弹窗操作（el-dialog 默认 appendToBody=false 渲染在组件树内，优先在 wrapper 内找） ----

function dialogEl(wrapper: VueWrapper): HTMLElement {
  const el =
    (wrapper.element.querySelector('.el-dialog') as HTMLElement | null) ??
    (document.body.querySelector('.el-dialog') as HTMLElement | null)
  expect(el, '弹窗应已打开').not.toBeNull()
  return el!
}

function dialogText(wrapper: VueWrapper): string {
  return dialogEl(wrapper).textContent ?? ''
}

async function clickDialogButton(wrapper: VueWrapper, text: string): Promise<void> {
  const btn = Array.from(dialogEl(wrapper).querySelectorAll('button')).find(
    (b) => b.textContent?.trim() === text,
  )
  expect(btn, `弹窗按钮 ${text} 应存在`).toBeDefined()
  ;(btn as HTMLElement).click()
  await flushPromises()
}

async function chooseCategory(wrapper: VueWrapper, label: string): Promise<void> {
  const radio = Array.from(dialogEl(wrapper).querySelectorAll('.el-radio')).find((r) =>
    r.textContent?.includes(label),
  )
  expect(radio, `分类项 ${label} 应存在`).toBeDefined()
  // jsdom 对 radio input.click() 不触发 EP 绑定的 change，手工置选并派发冒泡 change
  const input = (radio as HTMLElement).querySelector('input') as HTMLInputElement
  input.checked = true
  input.dispatchEvent(new Event('change', { bubbles: true }))
  await flushPromises()
}

function dialogTextarea(wrapper: VueWrapper): HTMLTextAreaElement {
  const ta = dialogEl(wrapper).querySelector('textarea')
  expect(ta, '弹窗正文输入框应存在').not.toBeNull()
  return ta as HTMLTextAreaElement
}

/** 轮询用谓词：正文 textarea 未渲染（loading/失败态）时返回 false 而非抛错 */
function textareaValueIncludes(wrapper: VueWrapper, text: string): boolean {
  const ta = dialogEl(wrapper).querySelector('textarea')
  return ta !== null && (ta as HTMLTextAreaElement).value.includes(text)
}

async function setComment(wrapper: VueWrapper, value: string): Promise<void> {
  const textarea = dialogEl(wrapper).querySelector('textarea')
  expect(textarea, '备注输入框应存在').not.toBeNull()
  const el = textarea as HTMLTextAreaElement
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

// ---- 用例 ----

describe('会话列表与历史渲染', () => {
  it('进入页面拉取会话列表并自动选中最近会话，历史按序渲染（引用角标与面板联动）', async () => {
    conversations = [conv(1, '退货流程问答'), conv(2, '年假咨询')]
    history.set(1, [
      userItem(11, '退货流程是什么？'),
      assistantItem(12, '根据资料[1]，48 小时内原路退款。', CITE),
    ])
    const wrapper = await mountChat()
    expect(wrapper.findAll('.conv-item')).toHaveLength(2)
    expect(wrapper.find('.conv-item.active').text()).toContain('退货流程问答')
    expect(wrapper.text()).toContain('退货流程是什么？')
    expect(wrapper.text()).toContain('根据资料')
    expect(wrapper.findAll('.bubble.assistant .cite')).toHaveLength(1)
    expect(wrapper.find('.cite-card').text()).toContain('客服手册')
    expect(wrapper.text()).toContain('相似度 0.91')
  })

  it('点击其他会话切换并拉取该会话历史渲染', async () => {
    conversations = [conv(1, '退货流程问答'), conv(2, '年假咨询')]
    history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答')])
    history.set(2, [userItem(21, '年假问题'), assistantItem(22, '年假回答')])
    const wrapper = await mountChat()
    expect(wrapper.text()).toContain('退货回答')
    await wrapper.findAll('.conv-item')[1]!.trigger('click')
    await waitFor(() => wrapper.text().includes('年假回答'), '切换后渲染目标会话历史')
    expect(wrapper.find('.conv-item.active').text()).toContain('年假咨询')
  })

  it('新会话按钮 POST /conversations 建会话并选中空会话（空态引导可见）', async () => {
    const wrapper = await mountChat()
    const newBtn = wrapper.find('button.new-btn')
    expect(newBtn.exists()).toBe(true)
    await newBtn.trigger('click')
    await flushPromises()
    const postConv = fetchMock.mock.calls.filter(
      (call) => call[0] === '/api/conversations' && (call[1] as RequestInit).method === 'POST',
    )
    expect(postConv).toHaveLength(1)
    expect(wrapper.findAll('.conv-item')).toHaveLength(1)
    expect(wrapper.find('.conv-item.active').text()).toContain('新会话')
    expect(wrapper.text()).toContain('问点什么吧')
  })

  it('会话删除需二次确认：取消不调 DELETE，确认后软删并从列表消失、自动切换下一会话', async () => {
    conversations = [conv(1, '退货流程问答'), conv(2, '年假咨询')]
    history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答')])
    history.set(2, [userItem(21, '年假问题'), assistantItem(22, '年假回答')])
    const wrapper = await mountChat()
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    await wrapper.findAll('.conv-item')[0]!.find('button.del').trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(deleteCalls()).toHaveLength(0)

    // MessageBoxData 为 MessageBoxInputData & Action 交叉类型，字面量须经断言绕过其类型瑕疵
    confirmSpy.mockResolvedValue({ action: 'confirm', value: '' } as unknown as MessageBoxData)
    await wrapper.findAll('.conv-item')[0]!.find('button.del').trigger('click')
    await waitFor(() => wrapper.findAll('.conv-item').length === 1, '删除后列表移除')
    expect(deleteCalls()).toHaveLength(1)
    expect(wrapper.text()).not.toContain('退货流程问答')
    await waitFor(() => wrapper.text().includes('年假回答'), '自动切换到下一会话')
    expect(wrapper.find('.conv-item.active').text()).toContain('年假咨询')
  })
})

describe('SSE 流式渲染（mock fetch 流）', () => {
  it('citations 恒先于 token：引用面板先上板，气泡文本随后增量拼接', async () => {
    const gate = new Gate()
    streamQueue = [
      [
        { text: citationsFrame(CITE) },
        {
          text: `${tokenFrame('根据资料')}${tokenFrame('[1]，48 小时内原路退款。')}${doneFrame()}`,
          gate,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准是多少')
    await waitFor(() => wrapper.find('.cite-card').exists(), 'citations 先上板')
    expect(wrapper.find('.bubble.assistant').text()).not.toContain('根据资料')
    gate.open()
    await waitFor(() => wrapper.text().includes('48 小时内原路退款'), 'token 拼接完成')
    expect(wrapper.find('.bubble.assistant').text()).toContain('根据资料')
    expect(wrapper.findAll('.bubble.assistant .cite')).toHaveLength(1)
  })

  it('done cacheHit=true 气泡显示"来自缓存"标注', async () => {
    streamQueue = [
      [{ text: `${tokenFrame('报销标准为每天 200 元。')}${doneFrame({ cacheHit: true })}` }],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准')
    await waitFor(() => wrapper.text().includes('来自缓存'), '缓存标注')
    expect(wrapper.text()).toContain('报销标准为每天 200 元。')
  })

  it('done 事件 promptTokens/completionTokens/durationMs 展示在消息 meta（对照原型 msg-meta）', async () => {
    streamQueue = [
      [
        {
          text: `${tokenFrame('报销标准为每天 200 元。')}${doneFrame({
            promptTokens: 912,
            completionTokens: 36,
            durationMs: 2400,
          })}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准')
    await waitFor(() => wrapper.text().includes('200 元'), '回答完成')
    expect(wrapper.text()).toContain('token: in 912 / out 36 · 2400ms')
  })

  it('done refused=true 展示精确拒答话术，无引用角标、无缓存标注、面板空态', async () => {
    streamQueue = [
      [
        {
          text: `${citationsFrame([])}${tokenFrame('知识库中未找到相关内容')}${doneFrame({ refused: true })}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '公司年会抽奖规则')
    await waitFor(
      () => wrapper.find('.bubble.assistant').text().includes('知识库中未找到相关内容'),
      '拒答话术',
    )
    expect(wrapper.find('.bubble.assistant').text()).toContain('知识库中未找到相关内容')
    expect(wrapper.findAll('.bubble.assistant .cite')).toHaveLength(0)
    expect(wrapper.text()).toContain('已拒答')
    expect(wrapper.text()).not.toContain('来自缓存')
    expect(wrapper.text()).toContain('无引用')
  })

  it('error 事件 retryable：气泡标生成失败+重试按钮；重试以相同问题新发一条（CHAT-12）', async () => {
    streamQueue = [
      [{ text: `${tokenFrame('根据资料[1]')}${errorFrame()}` }],
      [
        {
          text: `${citationsFrame(CITE)}${tokenFrame('根据资料[1]，48 小时内原路退款。')}${doneFrame()}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '退货流程')
    await waitFor(() => wrapper.text().includes('生成失败'), '失败标注')
    expect(wrapper.text()).toContain('生成服务暂时不可用')
    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await waitFor(() => wrapper.findAll('.bubble.user').length === 2, '重试新发 user 气泡')
    await waitFor(() => wrapper.text().includes('48 小时内原路退款'), '重试流完成')
    const calls = streamCalls()
    expect(calls).toHaveLength(2)
    expect(JSON.parse(String(calls[0]![1].body)).question).toBe('退货流程')
    expect(JSON.parse(String(calls[1]![1].body)).question).toBe('退货流程')
    expect(wrapper.text()).toContain('生成失败')
  })

  it('中途网络断流（read 抛 TypeError）：失败态"网络异常"可重试，重试新流成功（T8 清单①）', async () => {
    streamQueue = [
      [{ text: tokenFrame('根据资料'), crash: 'network' }],
      [{ text: `${tokenFrame('根据资料[1]，48 小时内原路退款。')}${doneFrame()}` }],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '退货流程')
    await waitFor(() => wrapper.text().includes('网络异常，请检查网络后重试'), '断流失败文案内联')
    expect(wrapper.text()).toContain('生成失败')
    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')
    expect(retry, '断流失败可重试').toBeDefined()
    await retry!.trigger('click')
    await waitFor(() => wrapper.text().includes('48 小时内原路退款'), '重试新流完成')
    expect(streamCalls()).toHaveLength(2)
    expect(JSON.parse(String(streamCalls()[1]![1].body)).question).toBe('退货流程')
  })

  it('心跳注释块与坏 JSON 帧跳过不中止流，后续事件正常应用（T8 清单②）', async () => {
    const heartbeat = ': keep-alive\n\n'
    const badFrame = 'event: token\ndata: {oops-not-json}\n\n'
    streamQueue = [
      [
        {
          text: `${heartbeat}${tokenFrame('前面')}${badFrame}${heartbeat}${tokenFrame('后面')}${doneFrame()}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '退货流程')
    await waitFor(
      () => wrapper.find('.bubble.assistant').text().includes('前面后面'),
      '坏帧前后 token 均拼接',
    )
    expect(wrapper.text()).not.toContain('生成失败')
    expect(wrapper.text()).toContain('token: in 12 / out 34 · 56ms')
  })

  it('未知事件名忽略：不判 error、流继续到 done（T8 清单②）', async () => {
    streamQueue = [
      [
        {
          text: `${tokenFrame('根据资料回答')}${frame('metrics', { elapsedMs: 3 })}${doneFrame()}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '退货流程')
    await waitFor(() => wrapper.text().includes('根据资料回答'), '未知事件后流继续完成')
    expect(wrapper.text()).not.toContain('生成失败')
  })

  it('发送中点"停止"：fetch signal 已中止，气泡标"已停止"可重试，重试同问题新发一条（T8 清单③）', async () => {
    const gate = new Gate()
    // 首段 token 即到（流式中），done 段被 gate 扣住保持流式态
    streamQueue = [[{ text: tokenFrame('部分回答') }, { text: doneFrame(), gate }]]
    const wrapper = await mountChat()
    await wrapper.find('textarea').setValue('差旅报销标准是多少')
    await sendButton(wrapper).trigger('click')
    await waitFor(() => wrapper.text().includes('部分回答'), '首段 token 到达')
    const stop = wrapper.findAll('button').find((b) => b.text() === '停止')
    expect(stop, '发送中显示停止按钮').toBeDefined()
    await stop!.trigger('click')
    await waitFor(() => wrapper.text().includes('已停止'), '气泡标已停止')
    expect(wrapper.text()).not.toContain('生成失败')
    const first = streamCalls()[0]!
    expect((first[1].signal as AbortSignal).aborted).toBe(true)
    streamQueue = [[{ text: `${tokenFrame('完整回答内容')}${doneFrame()}` }]]
    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')
    expect(retry, '停止后可重问').toBeDefined()
    await retry!.trigger('click')
    await waitFor(() => wrapper.text().includes('完整回答内容'), '重问流完成')
    expect(streamCalls()).toHaveLength(2)
    expect(JSON.parse(String(streamCalls()[1]![1].body)).question).toBe('差旅报销标准是多少')
  })

  it('开流前 429/3002 限额错误：气泡内联失败原因且不提供重试按钮', async () => {
    streamQueue = [
      makeResponse(429, 'application/json', null, {
        code: 3002,
        message: '今日提问次数已达上限，请明天再试',
        data: null,
      }),
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准')
    await waitFor(() => wrapper.text().includes('今日提问次数已达上限'), '限额错误内联')
    expect(wrapper.text()).toContain('生成失败')
    expect(wrapper.findAll('button').find((b) => b.text() === '重试')).toBeUndefined()
  })
})

describe('引用联动与原文核对弹窗（F-202，kotaemon 式溯源）', () => {
  it('点气泡角标 → 右侧面板对应卡片高亮（滚动定位）', async () => {
    conversations = [conv(1, '退货流程问答')]
    history.set(1, [
      userItem(11, '退货流程是什么？'),
      assistantItem(12, '根据资料[1]，48 小时内原路退款。', CITE),
    ])
    const wrapper = await mountChat()
    expect(wrapper.find('.cite-card').classes()).not.toContain('hl')
    await wrapper.find('.bubble.assistant .cite').trigger('click')
    await flushPromises()
    expect(wrapper.find('.cite-card').classes()).toContain('hl')
  })

  it('点引用卡 → "查看原文"弹窗：调 citations/content，展示标题/页码/位置 + 正文 readOnly 置灰（doc/qa 各按 refType 查询）', async () => {
    conversations = [conv(1, '退货流程问答')]
    history.set(1, [userItem(11, '两连问'), assistantItem(12, '根据资料[1][2]作答。', CITES)])
    giveContent('doc', 11, '3.2 差旅报销标准：住宿费每晚 400 元封顶，须提供发票。')
    giveContent('qa', 7, 'Q：保修期多久？A：整机保修 24 个月。')

    // doc 卡（独立挂载：jsdom 下 EP 弹窗 X/ESC 不触发 close，为环境限制而非实现缺陷）
    const wrapper = await mountChat()
    await wrapper.findAll('.cite-card')[0]!.trigger('click')
    // 标题/页码/位置来自 citation 兜底即刻可见；正文在 textarea value（textContent 不含）
    expect(dialogText(wrapper)).toContain('客服手册')
    await waitFor(() => textareaValueIncludes(wrapper, '住宿费每晚 400 元封顶'), 'doc 正文加载展示')
    expect(dialogText(wrapper)).toContain('第3章>3.2')
    expect(dialogText(wrapper)).toContain('相似度：0.91')
    const ta = dialogTextarea(wrapper)
    expect(ta.readOnly).toBe(true)
    expect(ta.value).toContain('3.2 差旅报销标准')
    expect(dialogEl(wrapper).querySelector('.readonly-grey')).not.toBeNull()
    expect(contentCalls()[0]).toContain('refType=doc&refId=11')
    // 点卡片同时高亮气泡对应角标（task-9 brief：点卡片高亮对应角标）
    expect(wrapper.find('.bubble.assistant .cite.hl').exists()).toBe(true)
    wrapper.unmount()

    // qa 卡：refType=qa 查问答对，正文为答案文本
    const qaWrapper = await mountChat()
    await qaWrapper.findAll('.cite-card')[1]!.trigger('click')
    expect(dialogText(qaWrapper)).toContain('FAQ问答对')
    await waitFor(() => textareaValueIncludes(qaWrapper, '整机保修 24 个月'), 'qa 答案文本加载展示')
    expect(contentCalls()).toHaveLength(2)
    expect(contentCalls()[1]).toContain('refType=qa&refId=7')
  })

  it('原文弹窗 404/3003 失败态：错误内联弹窗不关，重试成功后展示正文', async () => {
    conversations = [conv(1, '退货流程问答')]
    history.set(1, [userItem(11, '退货流程是什么？'), assistantItem(12, '根据资料[1]作答。', CITE)])
    const wrapper = await mountChat()
    await wrapper.find('.cite-card').trigger('click')
    await waitFor(() => dialogText(wrapper).includes('引用不存在或无权访问'), '失败错误内联')
    expect(dialogEl(wrapper).querySelector('.err-inline')).not.toBeNull()
    const retryBtn = Array.from(dialogEl(wrapper).querySelectorAll('button')).find((b) =>
      b.textContent?.includes('重试'),
    )
    expect(retryBtn, '失败态提供重试按钮').toBeDefined()
    giveContent('doc', 11, '3.2 差旅报销标准：住宿费每晚 400 元封顶。')
    await retryBtn!.click()
    await flushPromises()
    await waitFor(() => textareaValueIncludes(wrapper, '住宿费每晚 400 元封顶'), '重试后展示正文')
    expect(dialogText(wrapper)).not.toContain('引用不存在或无权访问')
  })
})

describe('消息反馈（F-208/CHAT-09）', () => {
  it('点赞轻交互：POST feedback up，meta 👍 高亮', async () => {
    conversations = [conv(1, '退货流程问答')]
    history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答内容', CITE)])
    const wrapper = await mountChat()
    const like = wrapper.findAll('button').find((b) => b.text() === '👍')
    expect(like, '点赞按钮应存在').toBeDefined()
    await like!.trigger('click')
    await flushPromises()
    expect(feedbackCalls).toHaveLength(1)
    expect(feedbackCalls[0]).toMatchObject({ id: 12, body: { feedback: 'up' } })
    const likeAfter = wrapper.findAll('button').find((b) => b.text() === '👍')
    expect(likeAfter!.classes()).toContain('el-button--primary')
  })

  it('点踩弹窗：前端校验拦截（无分类/无备注均不发请求），补齐后提交成功回填"已点踩：分类"', async () => {
    conversations = [conv(1, '退货流程问答')]
    history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答内容', CITE)])
    const wrapper = await mountChat()
    const dislike = wrapper.findAll('button').find((b) => b.text() === '👎')
    await dislike!.trigger('click')
    await flushPromises()
    expect(dialogText(wrapper)).toContain('反馈问题（点踩）')

    await clickDialogButton(wrapper, '提交反馈')
    expect(dialogText(wrapper)).toContain('请选择问题分类')
    expect(feedbackCalls).toHaveLength(0)

    await chooseCategory(wrapper, '答案错误')
    await clickDialogButton(wrapper, '提交反馈')
    expect(dialogText(wrapper)).toContain('点踩必须填写备注（帮助定位 badcase）')
    expect(feedbackCalls).toHaveLength(0)

    await setComment(wrapper, '引用与问题无关')
    await clickDialogButton(wrapper, '提交反馈')
    expect(feedbackCalls).toHaveLength(1)
    expect(feedbackCalls[0]).toMatchObject({
      id: 12,
      body: { feedback: 'down', category: '答案错误', comment: '引用与问题无关' },
    })
    await waitFor(() => wrapper.text().includes('已点踩：答案错误'), 'meta 回填已点踩分类')
  })

  it('点踩后端 422 兜底：错误内联弹窗不关闭，修正后可重提成功', async () => {
    conversations = [conv(1, '退货流程问答')]
    history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答内容', CITE)])
    feedbackError = { code: 1003, message: '备注长度非法' }
    const wrapper = await mountChat()
    const dislike = wrapper.findAll('button').find((b) => b.text() === '👎')
    await dislike!.trigger('click')
    await flushPromises()
    await chooseCategory(wrapper, '答案错误')
    await setComment(wrapper, '引用与问题无关')
    await clickDialogButton(wrapper, '提交反馈')
    await waitFor(() => dialogText(wrapper).includes('备注长度非法'), '422 兜底错误内联')
    expect(feedbackCalls).toHaveLength(1)
    expect(dialogEl(wrapper).querySelector('textarea')).not.toBeNull()
    feedbackError = null
    await clickDialogButton(wrapper, '提交反馈')
    await waitFor(() => wrapper.text().includes('已点踩：答案错误'), '重提成功回填')
    expect(feedbackCalls).toHaveLength(2)
  })

  it('历史已点踩消息：meta 展示"已点踩：答案错误"且 👎 高亮', async () => {
    conversations = [conv(1, '退货流程问答')]
    history.set(1, [
      userItem(11, '退货问题'),
      assistantItem(12, '退货回答内容', CITE, 'rag', false, 'down', '答案错误'),
    ])
    const wrapper = await mountChat()
    await waitFor(() => wrapper.text().includes('已点踩：答案错误'), '历史反馈状态渲染')
    const down = wrapper.findAll('button').find((b) => b.text() === '👎')
    expect(down!.classes()).toContain('el-button--danger')
  })
})

describe('输入校验与键盘行为', () => {
  it('空输入与超 2000 字禁发送：超长计数红色提示且不发起请求', async () => {
    const wrapper = await mountChat()
    expect(sendButton(wrapper).attributes('disabled')).toBeDefined()
    await wrapper.find('textarea').setValue('字'.repeat(2001))
    expect(sendButton(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('2001/2000')
    await sendButton(wrapper).trigger('click')
    await flushPromises()
    expect(streamCalls()).toHaveLength(0)
    await wrapper.find('textarea').setValue('正常问题')
    expect(sendButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('Enter 发送问题，Shift+Enter 换行不发送', async () => {
    streamQueue = [[{ text: `${tokenFrame('好的')}${doneFrame()}` }]]
    const wrapper = await mountChat()
    const textarea = wrapper.find('textarea')
    await textarea.setValue('差旅报销标准')
    await textarea.trigger('keydown', { key: 'Enter' })
    await waitFor(() => streamCalls().length === 1, 'Enter 触发发送')
    await textarea.setValue('第二问')
    await textarea.trigger('keydown', { key: 'Enter', shiftKey: true })
    await flushPromises()
    expect(streamCalls()).toHaveLength(1)
  })
})

describe('首问自动建会话', () => {
  it('conversationId 传 null 直接提问，done 后刷新列表并选中后端自建会话（消息桶迁移）', async () => {
    onStream = () => {
      // 后端在首问时自建会话（首问前 20 字命题），此处模拟其落库结果
      conversations = [conv(9, '报销标准是多少'), ...conversations]
    }
    streamQueue = [[{ text: `${tokenFrame('报销标准为每天 200 元。')}${doneFrame()}` }]]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准是多少')
    await waitFor(() => wrapper.findAll('.conv-item').length === 1, '列表出现自建会话')
    const body = JSON.parse(String(streamCalls()[0]![1].body))
    expect(body.conversationId).toBeNull()
    expect(body.question).toBe('报销标准是多少')
    expect(wrapper.find('.conv-item.active').text()).toContain('报销标准是多少')
    // 消息桶已迁移到真实会话，问答气泡仍在视图上
    expect(wrapper.findAll('.bubble.user')).toHaveLength(1)
    expect(wrapper.text()).toContain('报销标准为每天 200 元。')
  })
})

describe('历史中断占位（CHAT-12）', () => {
  it('user 消息后无 assistant（中断行被过滤）→ 渲染"生成失败可重试"占位', async () => {
    conversations = [conv(1, '退货流程问答')]
    history.set(1, [userItem(11, '退货流程是什么？')])
    const wrapper = await mountChat()
    await waitFor(() => wrapper.text().includes('生成失败'), '中断占位渲染')
    expect(wrapper.text()).toContain('退货流程是什么？')
    expect(wrapper.findAll('.bubble.assistant')).toHaveLength(1)
    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')
    expect(retry).toBeDefined()
    // 占位重试同样走新发一条
    streamQueue = [[{ text: `${tokenFrame('48 小时内原路退款。')}${doneFrame()}` }]]
    await retry!.trigger('click')
    await waitFor(() => streamCalls().length === 1, '占位重试发起新流')
    expect(JSON.parse(String(streamCalls()[0]![1].body)).question).toBe('退货流程是什么？')
  })
})
