/**
 * P6 用户管理 spec（users-view.spec.ts）共享的 mock 后端与工具。
 * 全链路走真 client.ts，mock 全局 fetch 按后端 app/api/admin_users.py +
 * services/users_admin.py 契约应答（docs/api.md 尚未收录 P6 节）；模块状态经
 * resetBackend 复位。
 */
import { DOMWrapper, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { expect, vi } from 'vitest'
import type { AdminUserItem } from '@/api/adminUsers'
import UsersView from '@/views/UsersView.vue'

export const fetchMock = vi.fn()

export const T0 = '2026-09-28T10:00:00+08:00'

export const backend = {
  users: [] as AdminUserItem[],
  /** POST /admin/users 的 201 应答（默认 null = 未配置） */
  createResult: null as { id: number; initialPassword: string } | null,
  /** POST /admin/users 的 409 应答（优先于 createResult） */
  createError: null as string | null,
  /** POST /admin/users/{id}/reset-password 的 201 应答（默认 null = 未配置） */
  resetResult: null as { id: number; initialPassword: string } | null,
  /** PATCH /{id}/status 的 422 应答（null=成功） */
  toggleError: null as string | null,
  createCalls: [] as { username: string; deptCode: string; role: string }[],
  toggleCalls: [] as { id: number; isActive: boolean }[],
  resetCalls: [] as number[],
  nextId: 100,
}

export function resetBackend(): void {
  backend.users = []
  backend.createResult = null
  backend.createError = null
  backend.resetResult = null
  backend.toggleError = null
  backend.createCalls = []
  backend.toggleCalls = []
  backend.resetCalls = []
  backend.nextId = 100
}

export function userItem(overrides: Partial<AdminUserItem> = {}): AdminUserItem {
  backend.nextId += 1
  return {
    id: backend.nextId,
    username: '客服小王',
    name: '客服小王',
    deptCode: 'service',
    role: 'member',
    status: '正常',
    locked: false,
    mustChangePassword: false,
    lastLoginAt: T0,
    createdAt: T0,
    ...overrides,
  }
}

function listResponse(url: string): Response {
  const q = new URLSearchParams(url.split('?')[1] ?? '')
  const keyword = q.get('keyword')
  const filtered = backend.users.filter((user) => !keyword || user.username.includes(keyword))
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
  if (url.startsWith('/api/admin/users?') && method === 'GET') return listResponse(url)
  if (url === '/api/admin/users' && method === 'POST') {
    const body = JSON.parse(String(init.body)) as {
      username: string
      deptCode: string
      role: string
    }
    backend.createCalls.push(body)
    if (backend.createError !== null) {
      return makeResponse(409, 'application/json', {
        code: 3005,
        message: backend.createError,
        data: null,
      })
    }
    if (backend.createResult === null) throw new Error('createResult not configured')
    return makeResponse(201, 'application/json', {
      code: 0,
      message: 'ok',
      data: backend.createResult,
    })
  }
  const toggle = /^\/api\/admin\/users\/(\d+)\/status$/.exec(url)
  if (toggle !== null && method === 'PATCH') {
    const id = Number(toggle[1])
    const body = JSON.parse(String(init.body)) as { isActive: boolean }
    backend.toggleCalls.push({ id, isActive: body.isActive })
    if (backend.toggleError !== null) {
      return makeResponse(422, 'application/json', {
        code: 1003,
        message: backend.toggleError,
        data: null,
      })
    }
    const row = backend.users.find((user) => user.id === id)
    if (row) row.status = body.isActive ? '正常' : '已禁用'
    return okEnvelope({ id, isActive: body.isActive })
  }
  const reset = /^\/api\/admin\/users\/(\d+)\/reset-password$/.exec(url)
  if (reset !== null && method === 'POST') {
    const id = Number(reset[1])
    backend.resetCalls.push(id)
    if (backend.resetResult === null) throw new Error('resetResult not configured')
    return makeResponse(201, 'application/json', {
      code: 0,
      message: 'ok',
      data: backend.resetResult,
    })
  }
  throw new Error(`unexpected fetch: ${method} ${url}`)
}

// ---- 响应构造（与 qa-pairs-mock 同口径） ----

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
    .filter((url) => url.startsWith('/api/admin/users?'))
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

export async function mountUsers(): Promise<VueWrapper> {
  const wrapper = mount(UsersView, {
    global: { plugins: [[ElementPlus, { locale: zhCn }]] },
  })
  mountedWrappers.push(wrapper)
  await flushPromises()
  return wrapper
}

export function unmountAllUsers(): void {
  while (mountedWrappers.length > 0) {
    const wrapper = mountedWrappers.pop()
    wrapper?.unmount()
  }
}

/** 在按钮集合中按文案模糊查找并点击（含 popconfirm teleport 后的 body 查询） */
export async function clickBtn(
  buttons: { text: () => string; trigger: (event: string) => Promise<void> }[],
  label: string,
): Promise<void> {
  const btn = buttons.find((b) => b.text().includes(label))
  await btn!.trigger('click')
}

/** 点击行内 link 按钮并确认 popconfirm（teleport 到 body：只点当前可见弹层的"确定"，
 * 防止上一轮遗留的隐藏 popconfirm 实例截胡） */
export async function confirmPopconfirm(
  row: {
    findAll: (s: string) => { text: () => string; trigger: (e: string) => Promise<void> }[]
  },
  label: string,
): Promise<void> {
  const before = document.querySelectorAll('.el-popconfirm__action').length
  const btn = row.findAll('button').find((b) => b.text().includes(label))
  await btn!.trigger('click')
  await waitFor(
    () => document.querySelectorAll('.el-popconfirm__action').length > before,
    'popconfirm 渲染',
  )
  const actions = [...document.querySelectorAll('.el-popconfirm__action')]
  // EP 2.14 action 内按钮无独立类名：确定=末位（primary）
  const btns = actions[actions.length - 1]!.querySelectorAll('button')
  await new DOMWrapper(btns[btns.length - 1]!).trigger('click')
}
