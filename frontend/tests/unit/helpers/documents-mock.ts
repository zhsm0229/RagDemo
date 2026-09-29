/**
 * P3 文档管理 spec（documents-view.spec.ts）共享的 mock 后端与工具。
 * 全链路走真 client.ts（统一信封 + FormData multipart），mock 全局 fetch 按
 * docs/api.md §2.5-§2.8 契约应答；模块状态经 resetBackend 复位。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { expect, vi } from 'vitest'
import { type DocumentItem, type DocumentStatus, type ZipImportSummary } from '@/api/documents'
import DocumentsView from '@/views/DocumentsView.vue'

export const fetchMock = vi.fn()

export const T0 = '2026-09-28T14:20:00+08:00'

export const backend = {
  docs: [] as DocumentItem[],
  /** POST /api/documents/metadata-suggest 的应答（默认 {} = 无建议） */
  suggestion: {} as Record<string, string>,
  zipSummary: null as ZipImportSummary | null,
  uploadCalls: [] as FormData[],
  retryCalls: [] as number[],
  confirmCalls: [] as number[],
  nextDocId: 100,
}

export function resetBackend(): void {
  backend.docs = []
  backend.suggestion = {}
  backend.zipSummary = null
  backend.uploadCalls = []
  backend.retryCalls = []
  backend.confirmCalls = []
  backend.nextDocId = 100
}

export function docItem(overrides: Partial<DocumentItem> = {}): DocumentItem {
  backend.nextDocId += 1
  return {
    id: backend.nextDocId,
    title: '客服_退货政策',
    deptId: 1,
    deptCode: 'service',
    originalFilename: 'a.pdf',
    mime: 'application/pdf',
    sizeBytes: 1024,
    version: 1,
    effectiveDate: '2026-01-10',
    expiryDate: null,
    ownerName: '张三',
    status: 'ready',
    parseEngine: 'mineru',
    parseError: null,
    isActive: true,
    uploadedBy: 1,
    createdAt: T0,
    updatedAt: T0,
    ...overrides,
  }
}

function listResponse(url: string): Response {
  const q = new URLSearchParams(url.split('?')[1] ?? '')
  const dept = q.get('dept')
  const status = q.get('status')
  const keyword = q.get('keyword')
  const filtered = backend.docs.filter(
    (doc) =>
      (!dept || doc.deptCode === dept) &&
      (!status || doc.status === (status as DocumentStatus)) &&
      (!keyword || doc.title.includes(keyword) || doc.originalFilename.includes(keyword)),
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
  if (url.startsWith('/api/documents?') && method === 'GET') return listResponse(url)
  if (url === '/api/documents' && method === 'POST') {
    backend.uploadCalls.push(init.body as FormData)
    const doc = docItem({ status: 'parsing', title: String((init.body as FormData).get('title')) })
    backend.docs.unshift(doc)
    return makeResponse(202, 'application/json', {
      code: 0,
      message: 'ok',
      data: { id: doc.id, status: 'parsing' },
    })
  }
  const retry = /^\/api\/documents\/(\d+)\/retry$/.exec(url)
  if (retry !== null && method === 'POST') {
    const id = Number(retry[1])
    backend.retryCalls.push(id)
    const doc = backend.docs.find((d) => d.id === id)
    if (doc) doc.status = 'parsing'
    return okEnvelope({ id, status: 'parsing' })
  }
  const confirm = /^\/api\/documents\/(\d+)\/confirm$/.exec(url)
  if (confirm !== null && method === 'POST') {
    const id = Number(confirm[1])
    backend.confirmCalls.push(id)
    const doc = backend.docs.find((d) => d.id === id)
    if (doc) doc.isActive = true
    return okEnvelope({ id, isActive: true })
  }
  if (url === '/api/documents/metadata-suggest' && method === 'POST') {
    return okEnvelope(backend.suggestion)
  }
  if (url === '/api/documents/import-zip' && method === 'POST') {
    if (backend.zipSummary === null) throw new Error('zipSummary not configured')
    return makeResponse(202, 'application/json', {
      code: 0,
      message: 'ok',
      data: backend.zipSummary,
    })
  }
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

// ---- 响应构造（与 chat-mock 同口径） ----

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
    .filter((url) => url.startsWith('/api/documents?'))
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

export async function mountDocuments(): Promise<VueWrapper> {
  // 与 main.ts 同口径安装 zh-cn locale（分页"共 N 条"等文案依赖）
  const wrapper = mount(DocumentsView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mountedWrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

/** 卸载全部挂载实例：视图含 30s 轮询定时器，泄漏会串扰后续用例的 fetchMock 计数 */
export function unmountAllDocuments(): void {
  while (mountedWrappers.length > 0) {
    const wrapper = mountedWrappers.pop()
    wrapper?.unmount()
  }
}

/** 构造 jsdom File（魔数/内容不校验，白名单在前端只看扩展名与体积） */
export function makeFile(name: string, size = 10): File {
  return new File([new ArrayBuffer(size)], name, { type: 'application/pdf' })
}
