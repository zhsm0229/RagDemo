/**
 * P1 登录页集成测试（mock '@/api/client'，真 pinia + memory router 走完整链路）：
 * 必填校验拦截（不发请求）、2001/2002 错误内联且 silent 不弹全局、
 * 成功分流：普通用户 → /chat，首登 mustChangePassword → /change-password。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => {
  class MockApiClientError extends Error {
    readonly code: number
    readonly data: unknown
    constructor(code: number, message: string, data: unknown = null) {
      super(message)
      this.name = 'ApiClientError'
      this.code = code
      this.data = data
    }
  }
  return {
    NETWORK_ERROR_CODE: -1,
    ApiClientError: MockApiClientError,
    get: vi.fn(),
    post: vi.fn(),
  }
})

import { ApiClientError, post } from '@/api/client'
import LoginView from '@/views/LoginView.vue'
import ChangePasswordView from '@/views/ChangePasswordView.vue'
import { useAuthStore } from '@/stores/auth'

const postMock = vi.mocked(post)

function makeRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'login', component: LoginView },
      { path: '/change-password', name: 'change-password', component: ChangePasswordView },
      { path: '/chat', name: 'chat', component: { render: (): null => null } },
    ],
  })
}

async function mountLogin(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = makeRouter()
  await router.push('/login')
  await router.isReady()
  const wrapper = mount(LoginView, {
    global: { plugins: [pinia, router, ElementPlus] },
  })
  return { wrapper, router }
}

/** el-form-item 的错误态经 refDebounced(100ms) 才渲染，断言错误文案前先等去抖落定 */
async function flushErrorRender(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 150))
}

async function clickLogin(wrapper: VueWrapper): Promise<void> {
  const button = wrapper.findAll('button').find((b) => b.text() === '登 录')
  expect(button).toBeDefined()
  await button?.trigger('click')
  await flushPromises()
}

async function fillAndSubmit(
  wrapper: VueWrapper,
  username: string,
  password: string,
): Promise<void> {
  const inputs = wrapper.findAll('input')
  await inputs[0]?.setValue(username)
  await inputs[1]?.setValue(password)
  await clickLogin(wrapper)
}

beforeEach(() => {
  postMock.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('登录表单必填校验（失焦/提交拦截）', () => {
  it('空表单点击登录被拦截，不发起请求，字段错误内联展示', async () => {
    const { wrapper } = await mountLogin()
    await clickLogin(wrapper)
    await flushErrorRender()
    expect(postMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请输入用户名')
    expect(wrapper.text()).toContain('请输入密码')
  })

  it('账号输入失焦为空即内联提示（blur 触发，§21.3）', async () => {
    const { wrapper } = await mountLogin()
    const inputs = wrapper.findAll('input')
    await inputs[0]?.setValue('')
    await inputs[0]?.trigger('blur')
    await flushErrorRender()
    expect(wrapper.text()).toContain('请输入用户名')
    expect(postMock).not.toHaveBeenCalled()
  })

  it('仅填密码提交仍被用户名必填拦截', async () => {
    const { wrapper } = await mountLogin()
    const inputs = wrapper.findAll('input')
    await inputs[1]?.setValue('secret123')
    await clickLogin(wrapper)
    await flushErrorRender()
    expect(postMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请输入用户名')
  })
})

describe('登录错误响应内联展示（§22.2，silent 不弹全局）', () => {
  it('2001 用户名或密码错误：服务端文案（含剩余次数）原样内联，停留登录页', async () => {
    const { wrapper, router } = await mountLogin()
    postMock.mockRejectedValueOnce(new ApiClientError(2001, '用户名或密码错误，剩余尝试次数 4'))
    await fillAndSubmit(wrapper, 'admin', 'wrong-pass')
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith(
      '/auth/login',
      { username: 'admin', password: 'wrong-pass' },
      { silent: true },
    )
    expect(wrapper.find('.err-inline').text()).toBe('用户名或密码错误，剩余尝试次数 4')
    expect(router.currentRoute.value.name).toBe('login')
  })

  it('2002 账号锁定：锁定文案原样内联，停留登录页', async () => {
    const { wrapper, router } = await mountLogin()
    postMock.mockRejectedValueOnce(
      new ApiClientError(2002, '密码连续错误 5 次，账号已锁定 15 分钟'),
    )
    await fillAndSubmit(wrapper, 'admin', 'wrong-pass')
    expect(wrapper.find('.err-inline').text()).toBe('密码连续错误 5 次，账号已锁定 15 分钟')
    expect(router.currentRoute.value.name).toBe('login')
  })
})

describe('登录成功分流跳转', () => {
  it('普通用户登录成功 → 跳转问答页 /chat，登录态写入 store', async () => {
    const { wrapper, router } = await mountLogin()
    postMock.mockResolvedValueOnce({
      userId: 2,
      username: '客服小王',
      role: 'member',
      mustChangePassword: false,
    })
    await fillAndSubmit(wrapper, '客服小王', 'secret123')
    expect(router.currentRoute.value.path).toBe('/chat')
    const auth = useAuthStore()
    expect(auth.user?.username).toBe('客服小王')
    expect(auth.isAuthenticated).toBe(true)
  })

  it('首登 mustChangePassword=true → 登录成功跳转改密页（不可绕过）', async () => {
    const { wrapper, router } = await mountLogin()
    postMock.mockResolvedValueOnce({
      userId: 3,
      username: '新员工小李',
      role: 'member',
      mustChangePassword: true,
    })
    await fillAndSubmit(wrapper, '新员工小李', 'secret123')
    expect(router.currentRoute.value.name).toBe('change-password')
  })

  it('admin 登录成功同样进问答页（统一 /chat，管理页手动进入）', async () => {
    const { wrapper, router } = await mountLogin()
    postMock.mockResolvedValueOnce({
      userId: 1,
      username: 'admin',
      role: 'admin',
      mustChangePassword: false,
    })
    await fillAndSubmit(wrapper, 'admin', 'secret123')
    expect(router.currentRoute.value.path).toBe('/chat')
  })
})
