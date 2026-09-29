/**
 * P4 问答对管理 spec（qa-pairs-view.spec.ts）共享的 mock 后端与工具。
 * 全链路走真 client.ts（统一信封 + FormData multipart），mock 全局 fetch 按
 * docs/api.md §4.1 契约应答；模块状态经 resetBackend 复位。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { expect, vi } from 'vitest'
import { type QaPairImportErrorRow, type QaPairItem, type QaPairStatus } from '@/api/qaPairs'
import QaPairsView from '@/views/QaPairsView.vue'

export const fetchMock = vi.fn()

export const T0 = '2026-09-28T10:00:00+08:00'

export interface ImportError {
  message: string
  rows: QaPairImportErrorRow[]
}

export const backend = {
  qaPairs: [] as QaPairItem[],
  /** POST /import 的 201 应答（默认 null = 未配置） */
  importResult: null as { imported: number; batchNo: string } | null,
  /** POST /import 的 422/1003 应答（优先于 importResult） */
  importError: null as ImportError | null,
  importCalls: [] as FormData[],
  toggleCalls: [] as { id: number; status: QaPairStatus }[],
  updateCalls: [] as { question: string; answer: string }[],
  deleteCalls: [] as number[],
  /** PATCH/PUT/DELETE 的 422 失败应答（null=成功；导入不走此项） */
  mutationError: null as string | null,
  nextId: 100,
}

export function resetBackend(): void {
  backend.qaPairs = []
  backend.importResult = null
  backend.importError = null
  backend.importCalls = []
  backend.toggleCalls = []
  backend.updateCalls = []
  backend.deleteCalls = []
  backend.mutationError = null
  backend.nextId = 100
}

export function qaItem(overrides: Partial<QaPairItem> = {}): QaPairItem {
  backend.nextId += 1
  return {
    id: backend.nextId,
    deptId: 1,
    question: '退货流程是什么？',
    answer: '质量问题退货：联系客服登记 → 寄回商品 → 验货后退款。',
    status: 'enabled',
    batchNo: 'QA-20260901-0001',
    createdAt: T0,
    isActive: true,
    ...overrides,
  }
}

function listResponse(url: string): Response {
  const q = new URLSearchParams(url.split('?')[1] ?? '')
  const deptId = q.get('deptId')
  const status = q.get('status')
  const keyword = q.get('keyword')
  const filtered = backend.qaPairs.filter(
    (qa) =>
      (!deptId || qa.deptId === Number(deptId)) &&
      (!status || qa.status === (status as QaPairStatus)) &&
      (!keyword || qa.question.includes(keyword) || qa.answer.includes(keyword)),
  )
  const page = Number(q.get('page') ?? 1)
  const pageSize = Number(q.get('pageSize') ?? 20)
  const start = (page - 1) * pageSize
  return okEnvelope({
    items: filtered.slice(start, start + pageSize),
    total: filtered.length,
    page,
    pageSize,
  })
}

export async function routeFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const method = init.method ?? 'GET'
  if (url.startsWith('/api/admin/qa-pairs?') && method === 'GET') return listResponse(url)
  if (url === '/api/admin/qa-pairs/import' && method === 'POST') {
    backend.importCalls.push(init.body as FormData)
    if (backend.importError !== null) {
      return makeResponse(422, 'application/json', {
        code: 1003,
        message: backend.importError.message,
        data: backend.importError.rows,
      })
    }
    if (backend.importResult === null) throw new Error('importResult not configured')
    return makeResponse(201, 'application/json', {
      code: 0,
      message: 'ok',
      data: backend.importResult,
    })
  }
  const toggle = /^\/api\/admin\/qa-pairs\/(\d+)\/status$/.exec(url)
  if (toggle !== null && method === 'PATCH') {
    const id = Number(toggle[1])
    const body = JSON.parse(String(init.body)) as { status: QaPairStatus }
    backend.toggleCalls.push({ id, status: body.status })
    if (backend.mutationError !== null) return errEnvelope(backend.mutationError)
    const row = backend.qaPairs.find((qa) => qa.id === id)
    if (row) row.status = body.status
    return okEnvelope({ id, status: body.status })
  }
  const edit = /^\/api\/admin\/qa-pairs\/(\d+)$/.exec(url)
  if (edit !== null && method === 'PUT') {
    const id = Number(edit[1])
    const body = JSON.parse(String(init.body)) as { question: string; answer: string }
    backend.updateCalls.push(body)
    if (backend.mutationError !== null) return errEnvelope(backend.mutationError)
    const row = backend.qaPairs.find((qa) => qa.id === id)
    if (row) {
      row.question = body.question
      row.answer = body.answer
    }
    return okEnvelope({ id, question: body.question, answer: body.answer })
  }
  if (edit !== null && method === 'DELETE') {
    const id = Number(edit[1])
    backend.deleteCalls.push(id)
    if (backend.mutationError !== null) return errEnvelope(backend.mutationError)
    backend.qaPairs = backend.qaPairs.filter((qa) => qa.id !== id)
    return okEnvelope({ id, isActive: false })
  }
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

// ---- 响应构造（与 documents-mock 同口径） ----

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

function errEnvelope(message: string): Response {
  return makeResponse(422, 'application/json', { code: 1003, message, data: null })
}

// ---- 断言/操作辅助 ----

export function listCalls(): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.startsWith('/api/admin/qa-pairs?'))
}

export function lastListCall(): string {
  const calls = listCalls()
  expect(calls.length, '应已发起列表请求').toBeGreaterThan(0)
  return calls[calls.length - 1]!
}

export async function waitFor(cond: () => boolean, label: string): Promise<void> {
  const deadline = Date.now() + 4000
  while (!cond()) {
    if (Date.now() > deadline) throw new Error(`waitFor timeout: ${label}`)
    await new Promise((resolve) => setTimeout(resolve, 10))
    await flushPromises()
  }
}

const mountedWrappers: VueWrapper[] = []

export async function mountQaPairs(): Promise<VueWrapper> {
  // 与 main.ts 同口径安装 zh-cn locale（分页"共 N 条"等文案依赖）
  const wrapper = mount(QaPairsView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mountedWrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

export function unmountAllQaPairs(): void {
  while (mountedWrappers.length > 0) {
    const wrapper = mountedWrappers.pop()
    wrapper?.unmount()
  }
}

/** 构造 jsdom File（前端白名单只看扩展名，魔数由后端把关） */
export function makeFile(name: string, size = 10): File {
  return new File([new ArrayBuffer(size)], name, { type: 'application/octet-stream' })
}
