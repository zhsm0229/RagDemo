/**
 * P2 问答工作台三个集成 spec（chat-view / chat-stream-robustness / chat-feedback）
 * 共享的 mock 后端与工具。本文件非 *.spec.ts，vitest 不收集为测试；
 * 模块状态按测试文件隔离（vitest 每文件独立模块图），文件内各用例经 resetBackend 复位。
 *
 * mock 全局 fetch：/chat/stream 返回真 ReadableStream 字节流，会话/历史端点返回
 * 统一信封——全链路走真 client.ts + parseSseFrames + streamChat（各 spec 的
 * beforeEach 里 stubGlobal('fetch', fetchMock) + resetBackend，见各文件顶部）。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { expect, vi } from 'vitest'
import type { MessageItem } from '@/api/chat'
import type { CitationContent, CitationItem } from '@/api/types'
import ChatView from '@/views/ChatView.vue'

export const fetchMock = vi.fn()

// ---- mock 后端状态与路由 ----

export interface DemoConv {
  id: number
  title: string
  updatedAt: string
}

export const T0 = '2026-09-28T14:20:00+08:00'

/** 模块级后端状态（各 spec 在 beforeEach 复位） */
export const backend = {
  conversations: [] as DemoConv[],
  history: new Map<number, MessageItem[]>(),
  /** 引用正文库：key 为 `${refType}:${refId}`，缺省 404/3003 */
  citationContents: new Map<string, CitationContent>(),
  streamQueue: [] as Array<StreamPart[] | Response>,
  feedbackCalls: [] as Array<{ id: number; body: Record<string, unknown> }>,
  feedbackError: null as { code: number; message: string } | null,
  nextConvId: 100,
  onStream: null as (() => void) | null,
}

export function resetBackend(): void {
  backend.conversations = []
  backend.history = new Map()
  backend.citationContents = new Map()
  backend.streamQueue = []
  backend.feedbackCalls = []
  backend.feedbackError = null
  backend.nextConvId = 100
  backend.onStream = null
}

export async function routeFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const method = init.method ?? 'GET'
  if (url === '/api/chat/stream') {
    backend.onStream?.()
    const next = backend.streamQueue.shift()
    if (next === undefined) throw new Error('no queued stream response')
    if (Array.isArray(next)) {
      return sseResponse(next, (init.signal as AbortSignal | null) ?? undefined)
    }
    return next
  }
  if (url === '/api/conversations' && method === 'POST') {
    const id = ++backend.nextConvId
    backend.conversations = [{ id, title: '新会话', updatedAt: T0 }, ...backend.conversations]
    return okEnvelope({ id })
  }
  if (url.startsWith('/api/conversations?')) {
    return okEnvelope({
      items: backend.conversations,
      total: backend.conversations.length,
      page: 1,
      pageSize: 50,
    })
  }
  const deleted = /^\/api\/conversations\/(\d+)$/.exec(url)
  if (deleted !== null && method === 'DELETE') {
    const id = Number(deleted[1])
    backend.conversations = backend.conversations.filter((c) => c.id !== id)
    return okEnvelope({ id, isActive: false })
  }
  const msgs = /^\/api\/conversations\/(\d+)\/messages$/.exec(url)
  if (msgs !== null) return okEnvelope({ items: backend.history.get(Number(msgs[1])) ?? [] })
  const content = /^\/api\/citations\/content\?refType=(doc|qa)&refId=(\d+)$/.exec(url)
  if (content !== null) {
    const refType = content[1] as 'doc' | 'qa'
    const found = backend.citationContents.get(`${refType}:${content[2]}`)
    if (found === undefined) {
      return makeResponse(404, 'application/json', null, {
        code: 3003,
        message: '引用不存在或无权访问',
        data: null,
      })
    }
    return okEnvelope(found)
  }
  // 在线预览字节流（DocPreviewDialog fetch→blob；jsdom Blob 直接承载）
  if (/^\/api\/documents\/\d+\/preview$/.exec(url) !== null && method === 'GET') {
    const blob = new Blob(['%PDF-1.4\n%mock-chat-preview'], { type: 'application/pdf' })
    return {
      ok: true,
      status: 200,
      headers: {
        get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/pdf' : null),
      },
      body: null,
      blob: async () => blob,
      json: async () => {
        throw new Error('no json body')
      },
    } as unknown as Response
  }
  const feedback = /^\/api\/messages\/(\d+)\/feedback$/.exec(url)
  if (feedback !== null && method === 'POST') {
    const id = Number(feedback[1])
    const body = JSON.parse(String(init.body)) as Record<string, unknown>
    backend.feedbackCalls.push({ id, body })
    if (backend.feedbackError !== null) {
      return makeResponse(422, 'application/json', null, {
        code: backend.feedbackError.code,
        message: backend.feedbackError.message,
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

export class Gate {
  private resolveFn: () => void = () => undefined
  readonly promise: Promise<void> = new Promise<void>((resolve) => {
    this.resolveFn = resolve
  })
  open(): void {
    this.resolveFn()
  }
}

export function makeResponse(
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
export interface StreamPart {
  text?: string
  gate?: Gate
  crash?: 'network'
}

/** SSE 字节流响应：支持外部 signal（停止→AbortError）与 crash（TypeError 网络断流） */
export function sseResponse(parts: StreamPart[], signal?: AbortSignal): Response {
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

export function okEnvelope(data: unknown): Response {
  return makeResponse(200, 'application/json', null, { code: 0, message: 'ok', data })
}

// ---- SSE 帧构造（docs/api.md §3.1 事件 DTO） ----

export const frame = (event: string, data: unknown): string =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
export const citationsFrame = (items: unknown[]): string =>
  frame('citations', { messageId: 101, items })
export const tokenFrame = (content: string): string => frame('token', { messageId: 101, content })
export const doneFrame = (extra: Record<string, unknown> = {}): string =>
  frame('done', {
    messageId: 101,
    refused: false,
    cacheHit: false,
    promptTokens: 12,
    completionTokens: 34,
    durationMs: 56,
    ...extra,
  })
export const errorFrame = (): string =>
  frame('error', { code: 5001, message: '生成服务暂时不可用', retryable: true })

// ---- 数据构造 ----

export const CITE: CitationItem[] = [
  {
    n: 1,
    refType: 'doc',
    refId: 11,
    docId: 55, // 文档主键（≠chunk id 11）：原文件/图片/预览按文档寻址
    title: '客服手册',
    page: 3,
    titlePath: '第3章>3.2',
    score: 0.91,
  },
]

export const CITES: CitationItem[] = [
  ...CITE,
  { n: 2, refType: 'qa', refId: 7, title: 'FAQ问答对', page: null, titlePath: null, score: 0.88 },
]

export function conv(id: number, title: string): DemoConv {
  return { id, title, updatedAt: T0 }
}

export function userItem(id: number, content: string): MessageItem {
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

export function assistantItem(
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
export function giveContent(refType: 'doc' | 'qa', refId: number, content: string): void {
  backend.citationContents.set(`${refType}:${refId}`, {
    refType,
    refId,
    content,
    title: refType === 'doc' ? '客服手册' : 'FAQ问答对',
    page: refType === 'doc' ? 3 : null,
    titlePath: refType === 'doc' ? '第3章>3.2' : null,
  })
}

// ---- 断言/操作辅助 ----

export function streamCalls(): Array<[string, RequestInit]> {
  return fetchMock.mock.calls.filter(
    (call): call is [string, RequestInit] => call[0] === '/api/chat/stream',
  )
}

export function deleteCalls(): Array<[string, RequestInit]> {
  return fetchMock.mock.calls.filter(
    (call) => typeof call[0] === 'string' && /^\/api\/conversations\/\d+$/.test(call[0]),
  ) as Array<[string, RequestInit]>
}

export function contentCalls(): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes('/api/citations/content'))
}

export async function waitFor(cond: () => boolean, label: string): Promise<void> {
  const deadline = Date.now() + 4000
  while (!cond()) {
    if (Date.now() > deadline) throw new Error(`waitFor timeout: ${label}`)
    await new Promise((resolve) => setTimeout(resolve, 10))
    await flushPromises()
  }
}

export function sendButton(wrapper: VueWrapper) {
  const button = wrapper.findAll('button').find((b) => b.text().includes('发送'))
  expect(button, '发送按钮应存在').toBeDefined()
  return button!
}

export async function ask(wrapper: VueWrapper, question: string): Promise<void> {
  await wrapper.find('textarea').setValue(question)
  await sendButton(wrapper).trigger('click')
  await flushPromises()
}

export async function mountChat(): Promise<VueWrapper> {
  const wrapper = mount(ChatView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}
