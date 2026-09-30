/**
 * P7 提问记录 spec（messages-view.spec.ts）共享的 mock 后端与工具。
 * 全链路走真 client.ts，mock 全局 fetch 按 docs/api.md §4.3.1/§4.3.2 契约应答；
 * 模块状态经 resetBackend 复位。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { expect, vi } from 'vitest'
import type { AdminMessageItem, MessageDetail } from '@/api/adminMessages'
import MessagesView from '@/views/MessagesView.vue'

export const fetchMock = vi.fn()

export const T0 = '2026-09-28T10:00:00+08:00'

export const backend = {
  messages: [] as AdminMessageItem[],
  /** GET /admin/messages/{id} 的应答（默认 null = 未配置） */
  detail: null as MessageDetail | null,
  detailError: null as string | null,
  detailCalls: [] as number[],
}

export function resetBackend(): void {
  backend.messages = []
  backend.detail = null
  backend.detailError = null
  backend.detailCalls = []
}

export function messageItem(overrides: Partial<AdminMessageItem> = {}): AdminMessageItem {
  backend.messages.push({
    id: 900 + backend.messages.length,
    conversationId: 1,
    createdAt: T0,
    username: '客服小王',
    deptCode: 'service',
    questionPreview: '退货流程是什么？',
    answerType: 'rag',
    hasCitations: true,
    feedback: null,
    ...overrides,
  })
  return backend.messages[backend.messages.length - 1]!
}

/** 详情应答工厂（api.md §4.3.2 user 段 6 键 / assistant 段 13 键） */
export function detailFixture(
  overrides: {
    assistant?: MessageDetail['assistant']
    question?: string
    feedback?: 'up' | 'down' | null
  } = {},
): MessageDetail {
  return {
    user: {
      id: 901,
      conversationId: 1,
      createdAt: T0,
      username: '客服小王',
      deptCode: 'service',
      content: overrides.question ?? '退货流程是什么？请给出完整步骤',
    },
    assistant:
      overrides.assistant === undefined
        ? {
            id: 902,
            content: '质量问题退货：联系客服登记 → 寄回商品 → 验货后退款。',
            citations: [
              {
                n: 1,
                refType: 'doc',
                refId: 33,
                title: '客服手册',
                page: 3,
                titlePath: '第3章 > 3.2 退货',
                score: 0.93,
              },
            ],
            answerType: 'rag',
            model: 'qwen-plus',
            promptTokens: 812,
            completionTokens: 96,
            latencyMs: 1840,
            cacheHit: false,
            feedback: overrides.feedback ?? null,
            feedbackCategory: overrides.feedback === 'down' ? '答案过时' : null,
            feedbackComment: overrides.feedback === 'down' ? '2026 年已改为线上退货' : null,
          }
        : overrides.assistant,
  }
}

function listResponse(url: string): Response {
  const q = new URLSearchParams(url.split('?')[1] ?? '')
  const deptId = q.get('deptId')
  const hasFeedback = q.get('hasFeedback')
  const filtered = backend.messages.filter((m) => {
    if (deptId && m.deptCode !== { '1': 'service', '2': 'hr', '3': 'all' }[deptId]) return false
    if (hasFeedback === 'true' && m.feedback !== 'down') return false
    if (hasFeedback === 'false' && m.feedback === 'down') return false
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
  if (url.startsWith('/api/admin/messages?') && method === 'GET') return listResponse(url)
  const detail = /^\/api\/admin\/messages\/(\d+)$/.exec(url)
  if (detail !== null && method === 'GET') {
    const id = Number(detail[1])
    backend.detailCalls.push(id)
    if (backend.detailError !== null) {
      return makeResponse(404, 'application/json', {
        code: 3003,
        message: backend.detailError,
        data: null,
      })
    }
    if (backend.detail === null) throw new Error('detail not configured')
    return okEnvelope(backend.detail)
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
    .filter((url) => url.startsWith('/api/admin/messages?'))
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

export async function mountMessages(): Promise<VueWrapper> {
  const wrapper = mount(MessagesView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mountedWrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

export function unmountAllMessages(): void {
  while (mountedWrappers.length > 0) {
    const wrapper = mountedWrappers.pop()
    wrapper?.unmount()
  }
}
