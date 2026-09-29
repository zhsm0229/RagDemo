/**
 * 改密页（首登强制）集成测试：≥8 位与确认一致拦截、原密码必填、
 * 后端 2001 原密码不正确内联且停留本页（§22.2 弹窗不关语义的路由版）、
 * 成功后调 /auth/change-password（silent）→ 跳 /chat，store 的 mustChangePassword 清零。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'

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
import ChangePasswordView from '@/views/ChangePasswordView.vue'
import LoginView from '@/views/LoginView.vue'
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

/** 首登态预置：user.mustChangePassword=true（守卫外的页面内文案与按钮依赖此态） */
async function mountChange(): Promise<{
  wrapper: VueWrapper
  router: Router
  auth: ReturnType<typeof useAuthStore>
}> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = { userId: 3, username: '新员工小李', role: 'member', mustChangePassword: true }
  auth.loaded = true
  const router = makeRouter()
  await router.push('/change-password')
  await router.isReady()
  const wrapper = mount(ChangePasswordView, {
    global: { plugins: [pinia, router, ElementPlus] },
  })
  return { wrapper, router, auth }
}

/** el-form-item 的错误态经 refDebounced(100ms) 才渲染，断言错误文案前先等去抖落定 */
async function flushErrorRender(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 150))
}

async function fillAndSubmit(
  wrapper: VueWrapper,
  oldPwd: string,
  newPwd: string,
  confirm: string,
): Promise<void> {
  const inputs = wrapper.findAll('input')
  await inputs[0]?.setValue(oldPwd)
  await inputs[1]?.setValue(newPwd)
  await inputs[2]?.setValue(confirm)
  const button = wrapper.findAll('button').find((b) => b.text().includes('确认修改'))
  expect(button).toBeDefined()
  await button?.trigger('click')
  await flushPromises()
}

beforeEach(() => {
  postMock.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('改密页对照原型（首登强制态）', () => {
  it('标题与按钮文案与原型一致（首次登录请修改密码 / 确认修改并进入）', async () => {
    const { wrapper } = await mountChange()
    expect(wrapper.find('h2').text()).toBe('首次登录请修改密码')
    const button = wrapper.findAll('button').find((b) => b.text().includes('确认修改'))
    expect(button?.text()).toContain('确认修改并进入')
  })
})

describe('改密表单前端校验拦截', () => {
  it('新密码不足 8 位：拦截提交不发请求，内联"密码至少 8 位"', async () => {
    const { wrapper } = await mountChange()
    await fillAndSubmit(wrapper, 'OldPass123', 'Pass123', 'Pass123')
    await flushErrorRender()
    expect(postMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('密码至少 8 位')
  })

  it('两次输入不一致：拦截提交不发请求，内联"两次输入不一致"', async () => {
    const { wrapper } = await mountChange()
    await fillAndSubmit(wrapper, 'OldPass123', 'NewPass1234', 'NewPass1235')
    await flushErrorRender()
    expect(postMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('两次输入不一致')
  })

  it('原密码失焦为空：内联必填提示（blur 触发）', async () => {
    const { wrapper } = await mountChange()
    const inputs = wrapper.findAll('input')
    await inputs[0]?.setValue('')
    await inputs[0]?.trigger('blur')
    await flushErrorRender()
    expect(wrapper.text()).toContain('请输入原密码')
    expect(postMock).not.toHaveBeenCalled()
  })
})

describe('改密提交与后端交互', () => {
  it('改密成功：以 silent 调 /auth/change-password（old+new），跳转 /chat，mustChangePassword 清零', async () => {
    const { wrapper, router, auth } = await mountChange()
    postMock.mockResolvedValueOnce({
      userId: 3,
      username: '新员工小李',
      role: 'member',
      mustChangePassword: false,
    })
    await fillAndSubmit(wrapper, 'OldPass123', 'NewPass1234', 'NewPass1234')
    expect(postMock).toHaveBeenCalledWith(
      '/auth/change-password',
      { old: 'OldPass123', new: 'NewPass1234' },
      { silent: true },
    )
    expect(router.currentRoute.value.path).toBe('/chat')
    expect(auth.user?.mustChangePassword).toBe(false)
  })

  it('后端 400/2001 原密码不正确：错误内联展示，停留改密页（不关闭语义）', async () => {
    const { wrapper, router, auth } = await mountChange()
    postMock.mockRejectedValueOnce(new ApiClientError(2001, '原密码不正确'))
    await fillAndSubmit(wrapper, 'WrongOld123', 'NewPass1234', 'NewPass1234')
    expect(wrapper.find('.err-inline').text()).toBe('原密码不正确')
    expect(router.currentRoute.value.name).toBe('change-password')
    expect(auth.user?.mustChangePassword).toBe(true)
  })
})
