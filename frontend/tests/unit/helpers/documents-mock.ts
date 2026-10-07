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
  deleteCalls: [] as number[],
  draftApproveCalls: [] as number[],
  draftRejectCalls: [] as { id: number; reason: string }[],
  /** GET /api/documents/{id}/preview 已被调用（预览弹窗加载） */
  previewCalls: [] as number[],
  nextDocId: 100,
}

export function resetBackend(): void {
  backend.docs = []
  backend.suggestion = {}
  backend.zipSummary = null
  backend.uploadCalls = []
  backend.retryCalls = []
  backend.confirmCalls = []
  backend.deleteCalls = []
  backend.draftApproveCalls = []
  backend.draftRejectCalls = []
  backend.previewCalls = []
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
    parseErrorDetail: null,
    parseStage: null,
    parseProgress: null,
    sourceType: 'upload',
    reviewStatus: null,
    rejectReason: null,
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
  const sourceType = q.get('sourceType')
  const reviewStatus = q.get('reviewStatus')
  const filtered = backend.docs.filter(
    (doc) =>
      (!dept || doc.deptCode === dept) &&
      (!status || doc.status === (status as DocumentStatus)) &&
      (!sourceType || doc.sourceType === sourceType) &&
      (!reviewStatus || doc.reviewStatus === reviewStatus) &&
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
  const remove = /^\/api\/documents\/(\d+)$/.exec(url)
  if (remove !== null && method === 'DELETE') {
    const id = Number(remove[1])
    backend.deleteCalls.push(id)
    backend.docs = backend.docs.filter((d) => d.id !== id)
    return okEnvelope({ id, deleted: true })
  }
  // 草稿审核（agent-evolution #05）：approve/reject 记录调用并同步翻转行内审核状态
  const draftApprove = /^\/api\/documents\/(\d+)\/draft\/approve$/.exec(url)
  if (draftApprove !== null && method === 'POST') {
    const id = Number(draftApprove[1])
    backend.draftApproveCalls.push(id)
    const doc = backend.docs.find((d) => d.id === id)
    if (doc) {
      doc.reviewStatus = '已通过'
      doc.status = 'parsing'
    }
    return okEnvelope({ id, reviewStatus: '已通过', status: 'parsing' })
  }
  const draftReject = /^\/api\/documents\/(\d+)\/draft\/reject$/.exec(url)
  if (draftReject !== null && method === 'POST') {
    const id = Number(draftReject[1])
    // client.ts 对 JSON body 走 JSON.stringify：此处读回需 parse（非 FormData）
    const rawBody = init.body as unknown
    const parsed = typeof rawBody === 'string' ? (JSON.parse(rawBody) as { reason?: unknown }) : {}
    const reason = String(parsed.reason ?? '')
    backend.draftRejectCalls.push({ id, reason })
    const doc = backend.docs.find((d) => d.id === id)
    if (doc) {
      doc.reviewStatus = '已驳回'
      doc.rejectReason = reason
    }
    return okEnvelope({ id, reviewStatus: '已驳回', rejectReason: reason })
  }
  // 预览/原文件字节流：Blob 变体（DocPreviewDialog 走 fetch→res.blob()）
  const preview = /^\/api\/documents\/(\d+)\/(preview|file)$/.exec(url)
  if (preview !== null && method === 'GET') {
    const id = Number(preview[1])
    if (!backend.docs.some((d) => d.id === id)) {
      return makeResponse(404, 'application/json', { code: 1003, message: '文档不存在' })
    }
    if (preview[2] === 'preview') backend.previewCalls.push(id)
    return blobResponse('%PDF-1.4\n%mock-preview')
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

/** 字节流响应（blob() 可用；jsdom 无真实流，Blob 直接承载文本字节） */
export function blobResponse(content: string, contentType = 'application/pdf'): Response {
  const blob = new Blob([content], { type: contentType })
  return {
    ok: true,
    status: 200,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? contentType : null),
    },
    body: null,
    blob: async () => blob,
    json: async () => {
      throw new Error('no json body')
    },
  } as unknown as Response
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
