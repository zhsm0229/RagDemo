/**
 * 全局 401 统一链路集成测试（W4 终审 I 项③）：真 pinia + memory router + 真
 * createUnauthorizedHandler 装配（同 router/index.ts 装配方式），mock fetch 不发真请求。
 * 覆盖：业务页 401/2003 → 清态 + 回 /login；/login 与 /change-password 自身 401 不回跳
 * （防循环，仅清态）；SSE 开流前 401 同通道；流中 error 5001 不触发登出；探活静默请求同样触发。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('element-plus', () => ({
  ElMessage: { error: vi.fn(), success: vi.fn() },
}))

import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { ApiClientError, request, setUnauthorizedHandler, UNAUTHORIZED_CODE } from '@/api/client'
import { streamChat } from '@/api/chat'
import { createUnauthorizedHandler, shouldRedirectToLogin } from '@/router/unauthorized'
import { useAuthStore } from '@/stores/auth'
import type { UserInfo } from '@/api/types'

const fetchMock = vi.fn()

/** 捕获 promise 拒绝值（断言为 Error 实例，避免 catch(e) 宽松断言） */
async function requestError(run: () => Promise<unknown>): Promise<ApiClientError> {
  try {
    await run()
  } catch (error: unknown) {
    expect(error).toBeInstanceOf(Error)
    return error as ApiClientError
  }
  throw new Error('expected request to reject')
}

/** handler 内 router.push 未被 await，轮询等待异步导航落定（vitest waitFor 自带重试） */
async function waitForRoute(router: Router, name: string): Promise<void> {
  await vi.waitFor(
    () => {
      expect(router.currentRoute.value.name).toBe(name)
    },
    { timeout: 2000 },
  )
}

const MEMBER: UserInfo = {
  userId: 2,
  username: '客服小王',
  role: 'member',
  mustChangePassword: false,
  memoryEnabled: true,
}
const FRESH_MEMBER: UserInfo = { ...MEMBER, mustChangePassword: true }

function jsonResponse(status: number, payload: unknown): Response {
  return {
    ok: status < 400,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? 'application/json' : null),
    },
    json: async () => payload,
  } as unknown as Response
}

/** SSE 响应：frames 整段入队后关闭（error 事件后接 done 正常收尾） */
function sseResponse(frames: string): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(frames))
      controller.close()
    },
  })
  return {
    ok: true,
    status: 200,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-type' ? 'text/event-stream' : null),
    },
    body,
  } as unknown as Response
}

const errorThenDone = (data: Record<string, unknown>): string =>
  `event: error\ndata: ${JSON.stringify(data)}\n\n` +
  `event: done\ndata: ${JSON.stringify({ messageId: 101, refused: false })}\n\n`

/** "不跳转"负向断言前先让潜在的在途 push 落定（若有 bug 触发跳转，50ms 内可见） */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 50))

let pinia: Pinia

/** 与 router/index.ts 相同的装配：真 handler 注册到 client 单点 */
async function mountChain(path: string): Promise<Router> {
  pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'login', component: { render: (): null => null } },
      {
        path: '/change-password',
        name: 'change-password',
        component: { render: (): null => null },
      },
      { path: '/chat', name: 'chat', component: { render: (): null => null } },
    ],
  })
  setUnauthorizedHandler(createUnauthorizedHandler(router))
  await router.push(path)
  await router.isReady()
  return router
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  setUnauthorizedHandler(null)
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('业务页 401/2003 → 清态回登录页', () => {
  it('登录态下业务接口 HTTP 401/2003：store 清空 + 跳转 /login + 抛 ApiClientError(2003)', async () => {
    const router = await mountChain('/chat')
    const auth = useAuthStore()
    auth.user = MEMBER
    fetchMock.mockResolvedValue(jsonResponse(401, { code: 2003, message: '未登录', data: null }))
    const err = await requestError(() => request('/conversations'))
    expect(err).toBeInstanceOf(ApiClientError)
    expect(err.code).toBe(UNAUTHORIZED_CODE)
    expect(auth.user).toBeNull()
    await waitForRoute(router, 'login')
  })

  it('业务码 2003（HTTP 200 信封）同通道：清态 + 回 /login', async () => {
    const router = await mountChain('/chat')
    const auth = useAuthStore()
    auth.user = MEMBER
    fetchMock.mockResolvedValue(jsonResponse(200, { code: 2003, message: '未登录', data: null }))
    await expect(request('/conversations')).rejects.toMatchObject({ code: 2003 })
    expect(auth.user).toBeNull()
    await waitForRoute(router, 'login')
  })
})

describe('登录态页自身 401 不回跳（防循环，仅清态）', () => {
  it('/login 页自身 401（登录失败 401/2001）：停留 /login，不触发跳转', async () => {
    const router = await mountChain('/login')
    const auth = useAuthStore()
    fetchMock.mockResolvedValue(
      jsonResponse(401, { code: 2001, message: '用户名或密码错误', data: null }),
    )
    await expect(request('/auth/login', { method: 'POST', silent: true })).rejects.toMatchObject({
      code: 2001,
    })
    await settle()
    expect(router.currentRoute.value.name).toBe('login')
    expect(auth.user).toBeNull()
  })

  it('/change-password 页自身 401（会话过期）：停留改密页，陈旧 mustChangePassword 态清空', async () => {
    const router = await mountChain('/change-password')
    const auth = useAuthStore()
    auth.user = FRESH_MEMBER
    fetchMock.mockResolvedValue(jsonResponse(401, { code: 2003, message: '未登录', data: null }))
    await expect(
      request('/auth/change-password', { method: 'POST', silent: true }),
    ).rejects.toMatchObject({ code: 2003 })
    await settle()
    expect(router.currentRoute.value.name).toBe('change-password')
    expect(auth.user).toBeNull()
  })
})

describe('SSE 流路径（streamChat）', () => {
  it('开流前 401/2003：同一通道清态 + 回 /login，并抛 ApiClientError(2003)', async () => {
    const router = await mountChain('/chat')
    const auth = useAuthStore()
    auth.user = MEMBER
    fetchMock.mockResolvedValue(jsonResponse(401, { code: 2003, message: '未登录', data: null }))
    const err = await requestError(() => streamChat({ question: '退货流程' }, () => undefined))
    expect(err).toBeInstanceOf(ApiClientError)
    expect(err.code).toBe(UNAUTHORIZED_CODE)
    expect(auth.user).toBeNull()
    await waitForRoute(router, 'login')
  })

  it('流中 error 事件 5001：仅作为业务事件下发，不触发登出与跳转', async () => {
    const router = await mountChain('/chat')
    const auth = useAuthStore()
    auth.user = MEMBER
    fetchMock.mockResolvedValue(
      sseResponse(errorThenDone({ code: 5001, message: '生成服务暂时不可用', retryable: true })),
    )
    const events: Array<Record<string, unknown>> = []
    await streamChat({ question: '退货流程' }, (e) =>
      events.push(e as unknown as Record<string, unknown>),
    )
    const errorEvent = events.find((e) => e.code === 5001)
    expect(errorEvent).toBeDefined()
    expect(auth.user).toEqual(MEMBER)
    expect(router.currentRoute.value.name).toBe('chat')
  })
})

describe('守卫探活（silent）同样触发', () => {
  it('ensureLoaded 探活 /auth/me 401：静默请求仍走全局通道，清态 + 回 /login', async () => {
    const router = await mountChain('/chat')
    const auth = useAuthStore()
    auth.user = MEMBER
    fetchMock.mockResolvedValue(jsonResponse(401, { code: 2003, message: '未登录', data: null }))
    await auth.ensureLoaded()
    expect(auth.user).toBeNull()
    await waitForRoute(router, 'login')
  })
})

describe('shouldRedirectToLogin 纯函数决策', () => {
  it('业务路由回登录页；login/change-password 不回跳；非字符串路由名（symbol/null）回登录页', () => {
    expect(shouldRedirectToLogin('chat')).toBe(true)
    expect(shouldRedirectToLogin('login')).toBe(false)
    expect(shouldRedirectToLogin('change-password')).toBe(false)
    expect(shouldRedirectToLogin(Symbol('chat'))).toBe(true)
    expect(shouldRedirectToLogin(null)).toBe(true)
  })
})
