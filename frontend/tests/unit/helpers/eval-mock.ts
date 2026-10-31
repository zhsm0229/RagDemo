/**
 * P9 评测管理 spec（eval-view.spec.ts）共享的 mock 后端与工具。
 * 全链路走真 client.ts，mock 全局 fetch 按 docs/api.md §4.2 契约应答；
 * 轮询容错口径如实模拟：running 期 detail 仅含 items 无 pendingCount 键、
 * failed 态无 pendingCount 键（W3 交接契约）。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { vi } from 'vitest'
import type {
  EvalImportErrorRow,
  EvalQuestionItem,
  EvalReport,
  EvalReportDocTypeGroup,
  EvalReportFourMetrics,
  EvalRunConfig,
  EvalRunDetailItem,
  EvalRunItem,
  ThresholdScanResult,
} from '@/api/evalAdmin'
import EvalView from '@/views/EvalView.vue'

export const fetchMock = vi.fn()

export const T0 = '2026-09-28T10:00:00+08:00'

/** 完整跑分行（列表项 + config 快照 + detail 明细），应答时按端点裁剪 */
export type MockRun = EvalRunItem & {
  config: EvalRunConfig | null
  detail: { items: EvalRunDetailItem[]; pendingCount?: number } | null
}

export const backend = {
  runs: [] as MockRun[],
  /** GET /admin/eval-questions? 返回的题库行（空=首用动线落题库 tab） */
  questions: [] as EvalQuestionItem[],
  nextId: 101,
  /** POST /admin/eval-runs 创建行的初始 total */
  newRunTotal: 0,
  /** GET /{id}/report 应答（null=未设置，返回空 totals 报告） */
  report: null as EvalReport | null,
  /** POST /{id}/threshold-scan 应答（null=422 未完成态） */
  scan: null as ThresholdScanResult | null,
  /** POST /admin/eval-questions/import：null=201 {imported}，否则 422 错误行 */
  importErrors: null as EvalImportErrorRow[] | null,
  imported: 12,
  detailCalls: [] as number[],
  scanCalls: [] as number[],
  createBody: null as { name: string; perspectiveDeptCode: string } | null,
  judgmentCalls: [] as { runId: number; items: { questionId: number; judgment: string }[] }[],
}

export function resetBackend(): void {
  backend.runs = []
  backend.questions = []
  backend.nextId = 101
  backend.newRunTotal = 0
  backend.report = null
  backend.scan = null
  backend.importErrors = null
  backend.imported = 12
  backend.detailCalls = []
  backend.scanCalls = []
  backend.createBody = null
  backend.judgmentCalls = []
}

export function runItem(overrides: Partial<EvalRunItem> = {}): EvalRunItem {
  return {
    id: 9,
    name: '9 月客服域回归',
    status: 'done',
    total: 10,
    correct: 8,
    accuracy: 0.8,
    createdAt: T0,
    ...overrides,
  }
}

export function detailItem(overrides: Partial<EvalRunDetailItem> = {}): EvalRunDetailItem {
  return {
    questionId: 1,
    deptId: 1,
    qtype: 'factual',
    expected: '7 个工作日',
    answerType: 'rag',
    answer: '退货周期为 7 个工作日。',
    citations: [
      { n: 1, refType: 'doc', refId: 33, title: '售后政策', page: 3, titlePath: null, score: 0.82 },
    ],
    autoJudgment: 'pending',
    error: null,
    ...overrides,
  }
}

/** 造一条 done 跑分：8 题 auto correct + 2 题 pending 的示例明细 */
export function doneRun(overrides: Partial<EvalRunItem> = {}): MockRun {
  const run: MockRun = {
    ...runItem(overrides),
    config: {
      model: 'glm-4-air',
      promptVersion: 'v1.2',
      qaThreshold: 0.85,
      refuseThreshold: 0.55,
      efSearch: 64,
      topK: 20,
      topN: 5,
      temperature: 0.3,
      manifestVersion: 'm7',
      judgePromptVersion: 'v1',
    },
    detail: {
      items: [
        detailItem({ questionId: 1, autoJudgment: 'correct' }),
        detailItem({ questionId: 2, autoJudgment: 'pending', answerType: 'qa' }),
      ],
      pendingCount: 2,
    },
  }
  backend.runs.unshift(run)
  return run
}

/** 四指标 fixture：recall 双题均分 0.85、precision 单题、relevancy 双题、correctness 单题 */
export function fourMetricsFixture(): EvalReportFourMetrics {
  return {
    contextRecall: { value: 0.85, evaluatedCount: 2 },
    contextPrecision: { value: 0.6, evaluatedCount: 1 },
    answerRelevancy: { value: 0.75, evaluatedCount: 2 },
    answerCorrectness: { value: null, evaluatedCount: 0 },
  }
}

/** 分型分组 fixture：general 有值组 + unassigned 只显题数组 */
export function docTypeGroupsFixture(): EvalReportDocTypeGroup[] {
  return [
    {
      docType: 'general',
      questionCount: 2,
      contextRecall: 0.85,
      contextPrecision: null,
      answerRelevancy: 0.75,
      answerCorrectness: null,
    },
    {
      docType: 'unassigned',
      questionCount: 1,
      contextRecall: null,
      contextPrecision: null,
      answerRelevancy: null,
      answerCorrectness: null,
    },
  ]
}

/** 造一条 running 跑分：detail 仅 items（无 pendingCount 键，轮询容错契约） */
export function runningRun(overrides: Partial<EvalRunItem> = {}): MockRun {
  const run: MockRun = {
    ...runItem({ status: 'running', total: 10, correct: 0, accuracy: 0, ...overrides }),
    config: null,
    detail: { items: [detailItem({ questionId: 1, autoJudgment: 'correct' })] },
  }
  backend.runs.unshift(run)
  return run
}

/** 阈值扫描结果 fixture：7 档拒答曲线 + 5 档 qa 曲线；推荐值排除"全员拒答"档（0.70） */
export function scanFixture(): ThresholdScanResult {
  return {
    refuseCurve: [
      { threshold: 0.4, accuracy: 0.7, refuseRate: 0.1 },
      { threshold: 0.45, accuracy: 0.7, refuseRate: 0.1 },
      { threshold: 0.5, accuracy: 0.75, refuseRate: 0.2 },
      { threshold: 0.55, accuracy: 0.8, refuseRate: 0.2 },
      { threshold: 0.6, accuracy: 0.8, refuseRate: 0.4 },
      { threshold: 0.65, accuracy: 0.9, refuseRate: 0.8 },
      { threshold: 0.7, accuracy: 0.9, refuseRate: 1.0 },
    ],
    qaCurve: [
      { threshold: 0.75, hitRate: 0.9 },
      { threshold: 0.8, hitRate: 0.8 },
      { threshold: 0.85, hitRate: 0.7 },
      { threshold: 0.9, hitRate: 0.5 },
      { threshold: 0.95, hitRate: 0.3 },
    ],
    recommended: { refuseThreshold: 0.65, qaThreshold: 0.75 },
    scannedAt: T0,
    scope: {
      refuseTotal: 8,
      qaTotal: 2,
      pendingSkipped: 2,
      errorSkipped: 0,
      note: 'qa 曲线仅统计 qa 题各档位仍命中的比例（档位敏感度），不含 rag 题反证数据。',
    },
  }
}

function summary(run: MockRun): EvalRunItem {
  return {
    id: run.id,
    name: run.name,
    status: run.status,
    total: run.total,
    correct: run.correct,
    accuracy: run.accuracy,
    createdAt: run.createdAt,
  }
}

const JUDGMENT_SCORES: Record<string, number> = { correct: 1, partial: 0.5, wrong: 0, pending: 0 }

/** 录分后按合并口径重算（api.md §4.2.6：correct=1/partial=0.5/wrong=0/pending=0） */
function recompute(run: MockRun): number {
  const items = run.detail?.items ?? []
  const correct = items.reduce(
    (sum, item) => sum + JUDGMENT_SCORES[item.judgment ?? item.autoJudgment],
    0,
  )
  run.correct = correct
  run.accuracy = run.total > 0 ? correct / run.total : 0
  return items.filter((item) => (item.judgment ?? item.autoJudgment) === 'pending').length
}

export async function routeFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const method = init.method ?? 'GET'
  // 题库/难例池面板随 EvalView 挂载即拉列表（02/03 票）：mock 缺路由会以
  // "unexpected fetch" 拒绝并留下未处理 rejection——空页数据兜底即可，面板非断言对象
  if (url.startsWith('/api/admin/eval-questions?') && method === 'GET') {
    const q = new URLSearchParams(url.split('?')[1] ?? '')
    return okEnvelope({
      items: backend.questions,
      total: backend.questions.length,
      page: Number(q.get('page') ?? 1),
      pageSize: Number(q.get('pageSize') ?? 20),
    })
  }
  if (url.startsWith('/api/admin/hardcase-seeds?') && method === 'GET') {
    const q = new URLSearchParams(url.split('?')[1] ?? '')
    return okEnvelope({
      items: [],
      total: 0,
      page: Number(q.get('page') ?? 1),
      pageSize: Number(q.get('pageSize') ?? 20),
    })
  }
  if (url.startsWith('/api/admin/eval-runs?') && method === 'GET') {
    const q = new URLSearchParams(url.split('?')[1] ?? '')
    const page = Number(q.get('page') ?? 1)
    const pageSize = Number(q.get('pageSize') ?? 20)
    return okEnvelope({
      items: backend.runs.slice((page - 1) * pageSize, page * pageSize).map(summary),
      total: backend.runs.length,
      page,
      pageSize,
    })
  }
  if (url === '/api/admin/eval-runs' && method === 'POST') {
    backend.createBody = JSON.parse(String(init.body)) as {
      name: string
      perspectiveDeptCode: string
    }
    const run: MockRun = {
      ...runItem({
        id: backend.nextId++,
        name: backend.createBody.name,
        status: 'running',
        total: backend.newRunTotal,
        correct: 0,
        accuracy: 0,
      }),
      config: null,
      detail: { items: [] },
    }
    backend.runs.unshift(run)
    return makeResponse(202, 'application/json', { code: 0, message: 'ok', data: { id: run.id } })
  }
  const runMatch = /^\/api\/admin\/eval-runs\/(\d+)(?:\/(judgments|threshold-scan|report))?$/.exec(
    url,
  )
  if (runMatch !== null) {
    const runId = Number(runMatch[1])
    const run = backend.runs.find((item) => item.id === runId)
    const action = runMatch[2]
    if (run === undefined)
      return makeResponse(404, 'application/json', {
        code: 3003,
        message: '评测任务不存在',
        data: null,
      })
    if (action === undefined && method === 'GET') {
      backend.detailCalls.push(runId)
      // running/failed 无 pendingCount 键（W3 交接契约：前端必须容忍键缺失）；
      // 深拷贝模拟真实 JSON 传输（新引用），原地变更不致引用相等跳过子组件渲染
      return okEnvelope({
        ...summary(run),
        config: run.config,
        detail: run.detail === null ? null : JSON.parse(JSON.stringify(run.detail)),
      })
    }
    if (action === 'judgments' && method === 'POST') {
      const body = JSON.parse(String(init.body)) as {
        items: { questionId: number; judgment: string }[]
      }
      backend.judgmentCalls.push({ runId, items: body.items })
      for (const entry of body.items) {
        const item = run.detail?.items.find((d) => d.questionId === entry.questionId)
        if (item) item.judgment = entry.judgment as EvalRunDetailItem['judgment']
      }
      if (run.detail) run.detail.pendingCount = recompute(run)
      return okEnvelope({ ...summary(run), pendingCount: run.detail?.pendingCount ?? 0 })
    }
    if (action === 'report' && method === 'GET') {
      return okEnvelope(
        backend.report ?? {
          config: run.config,
          status: run.status,
          totals: {
            total: run.total,
            correct: run.correct,
            accuracy: run.accuracy,
            pendingCount: 0,
          },
          byDept: [],
          byQtype: [],
          fourMetrics: {
            contextRecall: { value: null, evaluatedCount: 0 },
            contextPrecision: { value: null, evaluatedCount: 0 },
            answerRelevancy: { value: null, evaluatedCount: 0 },
            answerCorrectness: { value: null, evaluatedCount: 0 },
          },
          byDocType: [],
        },
      )
    }
    if (action === 'threshold-scan' && method === 'POST') {
      if (backend.scan === null) {
        return makeResponse(422, 'application/json', {
          code: 3004,
          message: '评测任务未完成，暂不能扫描阈值',
          data: null,
        })
      }
      backend.scanCalls.push(runId)
      return okEnvelope(backend.scan)
    }
  }
  if (url === '/api/admin/eval-questions/import' && method === 'POST') {
    if (backend.importErrors !== null) {
      return makeResponse(422, 'application/json', {
        code: 1003,
        message: `导入文件存在 ${backend.importErrors.length} 处错误，已整批退回，未入库`,
        data: backend.importErrors,
      })
    }
    return makeResponse(201, 'application/json', {
      code: 0,
      message: 'ok',
      data: { imported: backend.imported },
    })
  }
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

// ---- 响应构造（与 messages-mock 同口径） ----

export function makeResponse(status: number, contentType: string, json?: unknown): Response {
  return {
    ok: status < 400,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? contentType : null),
    },
    body: null,
    json:
      json === undefined
        ? async () => {
            throw new Error('no json body')
          }
        : async () => json,
  } as unknown as Response
}

export function okEnvelope(data: unknown): Response {
  return makeResponse(200, 'application/json', { code: 0, message: 'ok', data })
}

// ---- 断言/操作辅助 ----

export function listCalls(): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.startsWith('/api/admin/eval-runs?'))
}

export function detailCalls(): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => /^\/api\/admin\/eval-runs\/\d+$/.test(url))
}

const mountedWrappers: VueWrapper[] = []

export function makeFile(name: string, size = 10): File {
  return new File([new ArrayBuffer(size)], name, { type: 'application/octet-stream' })
}

export async function mountEval(): Promise<VueWrapper> {
  const wrapper = mount(EvalView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mountedWrappers.push(wrapper)
  await flushAll()
  return wrapper
}

export function unmountAll(): void {
  while (mountedWrappers.length > 0) {
    const wrapper = mountedWrappers.pop()
    wrapper?.unmount()
  }
}

/**
 * fake timers 兼容的微任务冲刷：advanceTimersByTimeAsync(0) 在推进计时器队列的
 * 同时清空待定微任务链（flushPromises 依赖的 setImmediate 在 fake timers 下不
 * 会触发，直接 await 会挂起）；未 mock 计时器时回退 flushPromises。
 */
export async function flushAll(): Promise<void> {
  try {
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(0)
  } catch {
    await flushPromises()
  }
}
