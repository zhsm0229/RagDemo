/**
 * P8 badcase 管理 spec（badcase-view.spec.ts）共享的 mock 后端与工具。
 * 全链路走真 client.ts，mock 全局 fetch 按 docs/api.md §4.3.3/§4.3.4 契约应答；
 * 模块状态经 resetBackend 复位。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { expect, vi } from 'vitest'
import type { BadcaseItem } from '@/api/badcase'
import BadcaseView from '@/views/BadcaseView.vue'

export const fetchMock = vi.fn()

export const T0 = '2026-09-28T10:00:00+08:00'

export const backend = {
  rows: [] as BadcaseItem[],
  /** PATCH /admin/badcases/{messageId} 的 422 应答（null=成功） */
  patchError: null as string | null,
  patchCalls: [] as { messageId: number; badcaseStatus: string; note: string | null }[],
}

export function resetBackend(): void {
  backend.rows = []
  backend.patchError = null
  backend.patchCalls = []
}

export function badcaseItem(overrides: Partial<BadcaseItem> = {}): BadcaseItem {
  backend.rows.push({
    messageId: 800 + backend.rows.length,
    createdAt: T0,
    deptCode: 'service',
    questionPreview: '退货流程是什么？',
    category: '答案过时',
    comment: '2026 年已改线上退货',
    badcaseStatus: '待处理',
    ...overrides,
  })
  return backend.rows[backend.rows.length - 1]!
}

function listResponse(url: string): Response {
  const q = new URLSearchParams(url.split('?')[1] ?? '')
  const deptId = q.get('deptId')
  const status = q.get('badcaseStatus')
  const filtered = backend.rows.filter((row) => {
    if (deptId && row.deptCode !== { '1': 'service', '2': 'hr', '3': 'all' }[deptId]) return false
    if (status && (row.badcaseStatus ?? '待处理') !== status) return false
    return true
  })
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
  if (url.startsWith('/api/admin/badcases?') && method === 'GET') return listResponse(url)
  const patch = /^\/api\/admin\/badcases\/(\d+)$/.exec(url)
  if (patch !== null && method === 'PATCH') {
    const messageId = Number(patch[1])
    const body = JSON.parse(String(init.body)) as {
      badcaseStatus: string
      note: string | null
    }
    backend.patchCalls.push({ messageId, badcaseStatus: body.badcaseStatus, note: body.note })
    if (backend.patchError !== null) {
      return makeResponse(422, 'application/json', {
        code: 1003,
        message: backend.patchError,
        data: null,
      })
    }
    const row = backend.rows.find((item) => item.messageId === messageId)
    if (row) row.badcaseStatus = body.badcaseStatus
    return okEnvelope({ messageId, badcaseStatus: body.badcaseStatus })
  }
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

// ---- 响应构造（与 users-mock 同口径） ----

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
    .filter((url) => url.startsWith('/api/admin/badcases?'))
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

export async function mountBadcase(): Promise<VueWrapper> {
  const wrapper = mount(BadcaseView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mountedWrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

export function unmountAllBadcase(): void {
  while (mountedWrappers.length > 0) {
    const wrapper = mountedWrappers.pop()
    wrapper?.unmount()
  }
}
