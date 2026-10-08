/**
 * 模板生成视图测试（agent-ui-design §3，agent-evolution #08）：空态引导、事件序驱动
 * 状态机（RUN/STEP/TOOL/TEXT → 时间线 + 草稿卡）、运行中配置锁定与停止、RUN_ERROR
 * 失败态可重试且草稿保留、开流前业务错误 retryable=false、完成态汇总与"去审核"跳转
 * 参数（对齐 #05 草稿筛选字段）、"再生成一批"新运行、未知事件忽略、运行中离页二次
 * 确认与卸载中止。事件应用逻辑在 useAgentRun（经视图集成驱动），SSE 帧解析在
 * sse.spec.ts 直测；此处 mock api 模块（照 services-health-panel.spec.ts 惯例）。
 */
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { streamAgentRun, type AgentEventHandler, type AgentRunPayload } from '@/api/agentSse'
import { ABORTED_CODE, ABORTED_MESSAGE, ApiClientError } from '@/api/client'
import { listDocuments, type DocumentItem } from '@/api/documents'
import type { PageData } from '@/api/types'
import TemplateGenView from '@/views/TemplateGenView.vue'

vi.mock('@/api/agentSse', () => ({
  streamAgentRun: vi.fn(),
  AGENT_NON_RETRYABLE_PRESTREAM_CODES: new Set([1003, 3002]),
}))

vi.mock('@/api/documents', () => ({
  listDocuments: vi.fn(),
}))

const mockedStream = vi.mocked(streamAgentRun)
const mockedListDocs = vi.mocked(listDocuments)

function docItem(overrides: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id: 11,
    title: '客服制度手册',
    deptId: 1,
    deptCode: 'service',
    originalFilename: 'kefu.pdf',
    mime: 'application/pdf',
    sizeBytes: 1024,
    version: 1,
    effectiveDate: '2026-10-01',
    expiryDate: null,
    ownerName: 'alice',
    status: 'ready',
    parseEngine: 'pypdf',
    parseError: null,
    parseErrorDetail: null,
    parseStage: null,
    parseProgress: null,
    sourceType: 'upload',
    reviewStatus: null,
    rejectReason: null,
    docType: 'general',
    isActive: true,
    uploadedBy: 7,
    createdAt: '2026-10-01T10:00:00',
    updatedAt: '2026-10-01T10:00:00',
    ...overrides,
  }
}

function docsPage(items: DocumentItem[]): PageData<DocumentItem> {
  return { items, total: items.length, page: 1, pageSize: 100 }
}

// ---- 流驱动替身：mockImplementation 捕获回调，测试用 emit/settle 驱动事件序 ----

let currentOnEvent: AgentEventHandler | null = null
let currentResolve: (() => void) | null = null
let currentSignal: AbortSignal | null = null

function installStreamStub(): void {
  mockedStream.mockImplementation(
    (_payload: AgentRunPayload, onEvent: AgentEventHandler, signal?: AbortSignal) =>
      new Promise<void>((resolve, reject) => {
        currentOnEvent = onEvent
        currentResolve = resolve
        currentSignal = signal ?? null
        // 忠实还原真实传输：abort 使 reader.read() 抛 AbortError → ApiClientError(-2)
        signal?.addEventListener('abort', () => {
          reject(new ApiClientError(ABORTED_CODE, ABORTED_MESSAGE))
        })
      }),
  )
}

type EmitInput = { event: string } & Record<string, unknown>

function emitEvent(event: EmitInput): void {
  currentOnEvent?.(event)
}

function settleStream(): void {
  currentResolve?.()
  currentResolve = null
}

// ---- AG-UI 事件构造（载荷键名随协议 snake_case） ----

const stepStarted = (n: number) => ({ event: 'STEP_STARTED', step_name: `step-${n}` })
const stepFinished = (n: number) => ({ event: 'STEP_FINISHED', step_name: `step-${n}` })
const toolStart = (id: string, name: string) => ({
  event: 'TOOL_CALL_START',
  tool_call_id: id,
  tool_call_name: name,
})
const toolArgs = (id: string, delta: string) => ({
  event: 'TOOL_CALL_ARGS',
  tool_call_id: id,
  delta,
})
const toolEnd = (id: string) => ({ event: 'TOOL_CALL_END', tool_call_id: id })
const textStart = (id: string) => ({
  event: 'TEXT_MESSAGE_START',
  message_id: id,
  role: 'assistant',
})
const textContent = (id: string, delta: string) => ({
  event: 'TEXT_MESSAGE_CONTENT',
  message_id: id,
  delta,
})
const textEnd = (id: string) => ({ event: 'TEXT_MESSAGE_END', message_id: id })

// ---- 挂载（真实 router + memory history，跳转可断言） ----

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

let router: Router
let wrapper: VueWrapper | null = null

async function mountView(): Promise<VueWrapper> {
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/kb/template-gen',
        name: 'kb-template-gen',
        component: TemplateGenView,
        meta: { title: '知识库·模板生成' },
      },
      // 去审核落点 / 再生成分支用的占位路由（DocumentsView 本体不在本测试范围）
      {
        path: '/kb/documents',
        name: 'kb-documents',
        component: defineComponent({ render: () => null }),
      },
      { path: '/chat', name: 'chat', component: defineComponent({ render: () => null }) },
    ],
  })
  await router.push('/kb/template-gen')
  await router.isReady()
  // 经 RouterView 挂载：onBeforeRouteLeave 需要组件处于 router-view 上下文，
  // 直挂视图拿不到 active route record，守卫静默不注册（离页确认用例依赖它）
  wrapper = mount(
    defineComponent({
      name: 'RouterViewHarness',
      setup: () => () => h(RouterView),
    }),
    { global: { plugins: [ElementPlus, router] } },
  )
  await flushPromises()
  return wrapper
}

async function waitFor(cond: () => boolean, label: string): Promise<void> {
  const deadline = Date.now() + 5000
  while (!cond()) {
    if (Date.now() > deadline) throw new Error(`waitFor timeout: ${label}`)
    await new Promise((resolve) => setTimeout(resolve, 10))
    await flushPromises()
  }
}

/** 选来源文档与目标部门（el-select teleport 到 body），点开始生成 */
async function startRun(w: VueWrapper): Promise<void> {
  await w.find('.cfg-doc .el-select__wrapper').trigger('click')
  await waitFor(() => document.querySelector('.el-select-dropdown__item') !== null, '文档下拉渲染')
  const docOption = [...document.querySelectorAll('.el-select-dropdown__item')].find((el) =>
    el.textContent?.includes('客服制度手册'),
  )!
  await new DOMWrapper(docOption).trigger('click')
  await flushPromises()
  await w.find('.cfg-dept .el-select__wrapper').trigger('click')
  await waitFor(
    () =>
      [...document.querySelectorAll('.el-select-dropdown')].some((d) =>
        d.textContent?.includes('客服'),
      ),
    '部门下拉渲染',
  )
  const deptOption = [...document.querySelectorAll('.el-select-dropdown__item')].find(
    (el) => el.textContent?.trim() === '客服',
  )!
  await new DOMWrapper(deptOption).trigger('click')
  await flushPromises()
  const startBtn = w.findAll('button').find((b) => b.text() === '开始生成')!
  await startBtn.trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  installStreamStub()
  mockedListDocs.mockResolvedValue(docsPage([docItem()]))
})

afterEach(async () => {
  wrapper?.unmount()
  wrapper = null
  ElMessageBox.close() // 复位 MessageBox 单例（未关弹窗会扣住下一个用例）
  document.body
    .querySelectorAll('.el-overlay, .el-message-box, .el-message, .el-select__popper')
    .forEach((n) => n.remove())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('模板生成视图', () => {
  it('初始空态有引导文案，挂载即拉就绪文档，配置可用', async () => {
    const w = await mountView()
    expect(mockedListDocs).toHaveBeenCalledTimes(1)
    expect(mockedListDocs.mock.calls[0]?.[0]).toMatchObject({ status: 'ready' })
    expect(w.text()).toContain('选择文档后点“开始生成”，运行过程与草稿将在这里呈现')
    expect(w.find('[data-test="run-status"]').text()).toContain('状态：空闲')
    const startBtn = w.findAll('button').find((b) => b.text() === '开始生成')!
    expect(startBtn.attributes('disabled')).toBeUndefined()
  })

  it('AG-UI 事件序驱动状态机：伪节点收口、步骤/工具时间线、ARGS 折叠、TEXT 增量进草稿卡', async () => {
    const w = await mountView()
    await startRun(w)
    expect(mockedStream).toHaveBeenCalledTimes(1)
    const payload = mockedStream.mock.calls[0]?.[0]
    expect(payload?.taskType).toBe('template_draft')
    expect(payload?.input).toContain('客服制度手册')
    expect(payload?.input).toContain('客服')
    expect(payload?.input).toContain('3 套')
    // 提交即锁定配置 + 0 号伪节点
    const startBtn = w.findAll('button').find((b) => b.text() === '开始生成')!
    expect(startBtn.attributes('disabled')).toBeDefined()
    expect(w.find('[data-test="timeline"]').text()).toContain('已提交生成任务')

    emitEvent({ event: 'RUN_STARTED', thread_id: 'agent-run-1', run_id: '1' })
    await flushPromises()
    emitEvent(stepStarted(1))
    await flushPromises()
    emitEvent(toolStart('call-1', 'knowledge_base_search'))
    await flushPromises()
    expect(w.find('[data-test="timeline"]').text()).toContain('步骤 1')
    expect(w.find('[data-test="timeline"]').text()).toContain('工具：knowledge_base_search')
    expect(w.find('[data-test="timeline"]').text()).toContain('进行中')
    emitEvent(toolArgs('call-1', '{"query":"客服 制度"}'))
    await flushPromises()
    expect(w.find('[data-test="timeline"]').text()).toContain('客服 制度')
    emitEvent(toolEnd('call-1'))
    emitEvent(stepFinished(1))
    await flushPromises()
    expect(w.find('[data-test="timeline"]').text()).toContain('完成')

    emitEvent(textStart('msg-1'))
    emitEvent(textContent('msg-1', '【退货流程模板】'))
    emitEvent(textContent('msg-1', '第一步：…'))
    await flushPromises()
    expect(w.find('[data-test="draft-card-1"]').text()).toContain('【退货流程模板】第一步：…')

    emitEvent({
      event: 'RUN_FINISHED',
      thread_id: 'agent-run-1',
      run_id: '1',
      result: { text: 'x' },
    })
    await flushPromises()
    settleStream()
    await flushPromises()
    expect(w.find('[data-test="run-status"]').text()).toContain('状态：已完成')
    const summary = w.find('[data-test="run-summary"]')
    expect(summary.text()).toContain('生成完成：共 1 条草稿')
    expect(summary.text()).toContain('去审核')
    expect(summary.text()).toContain('再生成一批')
    expect(
      w
        .findAll('button')
        .find((b) => b.text() === '开始生成')!
        .attributes('disabled'),
    ).toBeUndefined()
  })

  it('完成态"去审核"跳文档管理并携带 #05 草稿筛选参数；"再生成一批"发起新运行且旧运行折叠', async () => {
    const w = await mountView()
    await startRun(w)
    emitEvent({ event: 'RUN_STARTED', thread_id: 'a', run_id: '9' })
    emitEvent(textStart('m1'))
    emitEvent(textContent('m1', '草稿正文'))
    emitEvent(textEnd('m1'))
    emitEvent({ event: 'RUN_FINISHED', thread_id: 'a', run_id: '9' })
    await flushPromises()
    settleStream()
    await flushPromises()

    await w
      .findAll('button')
      .find((b) => b.text() === '去审核')!
      .trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/kb/documents')
    expect(router.currentRoute.value.query.sourceType).toBe('draft')
    expect(router.currentRoute.value.query.reviewStatus).toBe('待审核')
  })

  it('再生成一批：旧成功运行折叠进历史，新运行重新开始', async () => {
    const w = await mountView()
    await startRun(w)
    emitEvent({ event: 'RUN_STARTED', thread_id: 'a', run_id: '1' })
    emitEvent(textStart('m1'))
    emitEvent(textContent('m1', '第一批'))
    emitEvent({ event: 'RUN_FINISHED', thread_id: 'a', run_id: '1' })
    await flushPromises()
    settleStream()
    await flushPromises()

    await w
      .findAll('button')
      .find((b) => b.text() === '再生成一批')!
      .trigger('click')
    await flushPromises()
    expect(mockedStream).toHaveBeenCalledTimes(2)
    expect(mockedStream.mock.calls[1]?.[0].input).toBe(mockedStream.mock.calls[0]?.[0].input)
    expect(w.find('[data-test="run-history"]').exists()).toBe(true)
    expect(w.find('[data-test="run-history"]').text()).toContain('第 1 次运行')
    expect(w.find('[data-test="run-status"]').text()).toContain('状态：生成中')
  })

  it('RUN_ERROR：失败态可重试、已生成草稿保留；重试=同参数新运行', async () => {
    const w = await mountView()
    await startRun(w)
    emitEvent({ event: 'RUN_STARTED', thread_id: 'a', run_id: '1' })
    emitEvent(textStart('m1'))
    emitEvent(textContent('m1', '半批草稿'))
    emitEvent({ event: 'RUN_ERROR', message: '生成服务暂时不可用，请重试', code: '5001' })
    await flushPromises()
    settleStream()
    await flushPromises()

    const failure = w.find('[data-test="run-failure"]')
    expect(failure.exists()).toBe(true)
    expect(failure.text()).toContain('生成失败，可重试')
    expect(failure.text()).toContain('生成服务暂时不可用，请重试')
    expect(w.find('[data-test="draft-card-1"]').text()).toContain('半批草稿')
    expect(w.find('[data-test="run-status"]').text()).toContain('状态：已失败')

    await w
      .findAll('button')
      .find((b) => b.text() === '重试')!
      .trigger('click')
    await flushPromises()
    expect(mockedStream).toHaveBeenCalledTimes(2)
    expect(mockedStream.mock.calls[1]?.[0].input).toBe(mockedStream.mock.calls[0]?.[0].input)
    expect(w.find('[data-test="run-history"]').text()).toContain('生成服务暂时不可用')
  })

  it('开流前业务错误（422/1003）：失败态 retryable=false 不出重试按钮', async () => {
    const w = await mountView()
    mockedStream.mockRejectedValueOnce(new ApiClientError(1003, '不支持的任务类型'))
    await startRun(w)
    await flushPromises()
    const failure = w.find('[data-test="run-failure"]')
    expect(failure.exists()).toBe(true)
    expect(failure.text()).toContain('不支持的任务类型')
    expect(failure.text()).not.toContain('生成失败，可重试')
    expect(w.findAll('button').find((b) => b.text() === '重试')).toBeUndefined()
    expect(w.find('[data-test="run-status"]').text()).toContain('状态：已失败')
  })

  it('用户停止：abort 传达给传输层，stopped 态可重试', async () => {
    const w = await mountView()
    await startRun(w)
    emitEvent({ event: 'RUN_STARTED', thread_id: 'a', run_id: '1' })
    emitEvent(stepStarted(1))
    await flushPromises()

    await w
      .findAll('button')
      .find((b) => b.text() === '停止')!
      .trigger('click')
    await flushPromises()
    expect(currentSignal?.aborted).toBe(true)
    const stopped = w.find('[data-test="run-stopped"]')
    expect(stopped.exists()).toBe(true)
    expect(stopped.text()).toContain('已停止')
    expect(w.find('[data-test="timeline"]').text()).toContain('已停止')
    expect(w.findAll('button').find((b) => b.text() === '重试')).toBeDefined()
    // 停止后可重试：同参数新运行
    await w
      .findAll('button')
      .find((b) => b.text() === '重试')!
      .trigger('click')
    await flushPromises()
    expect(mockedStream).toHaveBeenCalledTimes(2)
  })

  it('未知事件名忽略不判错（心跳兼容），后续正常收口', async () => {
    const w = await mountView()
    await startRun(w)
    emitEvent({ event: 'CUSTOM_HEARTBEAT', ts: 1 })
    emitEvent({ event: 'RUN_STARTED', thread_id: 'a', run_id: '1' })
    emitEvent(textStart('m1'))
    emitEvent(textContent('m1', '正文'))
    emitEvent({ event: 'RUN_FINISHED', thread_id: 'a', run_id: '1' })
    await flushPromises()
    settleStream()
    await flushPromises()
    expect(w.find('[data-test="run-summary"]').exists()).toBe(true)
    expect(w.find('[data-test="draft-card-1"]').text()).toContain('正文')
  })

  it('运行中离页需二次确认：取消则留在本页', async () => {
    const w = await mountView()
    await startRun(w)
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    const failure = await router.push('/chat')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(String(confirmSpy.mock.calls[0]?.[0])).toContain('离开将不再看到运行过程')
    expect(failure).not.toBeUndefined() // 导航被守卫拦下
    expect(router.currentRoute.value.path).toBe('/kb/template-gen')
  })

  it('组件卸载即中止本地消费（服务端断连兜底，不留僵尸流）', async () => {
    const w = await mountView()
    await startRun(w)
    expect(currentSignal?.aborted).toBe(false)
    w.unmount()
    wrapper = null
    expect(currentSignal?.aborted).toBe(true)
  })
})
