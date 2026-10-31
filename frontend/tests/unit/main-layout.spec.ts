/**
 * MainLayout 侧栏折叠组件测试（工单 18，设计 §6.5 + §8 R9）：
 * 纯前端状态逻辑零 mock 成本——默认展开、点击 toggle 折叠（el-menu collapse 类 +
 * aside 宽度类/宽度变量切换 + localStorage 写 '1'）、localStorage '1' 再挂载保持折叠、
 * 非法值容错回展开态、aria-label/aria-expanded 双向随态切换、菜单守账词双保险。
 * 挂载口径（R9）：createMemoryHistory 最小 stub 路由（不含守卫与业务页，零 fetch）+
 * ElementPlus 全量插件（el-menu-item-group 随全量注册）+ RouterView Harness
 * （MainLayout 是布局级路由组件，直接挂载会使其内部 router-view 递归渲染自身）；
 * afterEach 清 localStorage 防折叠态跨用例残留。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, describe, expect, it } from 'vitest'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import MainLayout from '@/layout/MainLayout.vue'
import { useAuthStore } from '@/stores/auth'

const SIDEBAR_KEY = 'rag.ui.sidebarCollapsed'

async function mountLayout(): Promise<VueWrapper> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = {
    userId: 1,
    username: 'tester',
    role: 'admin',
    mustChangePassword: false,
    memoryEnabled: true,
  }
  // 最小 stub 路由（R9 允许：既有路由表或最小 stub 二选一，stub 避免业务页噪音）
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/',
        component: MainLayout,
        children: [
          { path: 'chat', name: 'chat', component: defineComponent({ render: () => null }) },
        ],
      },
    ],
  })
  await router.push('/chat')
  await router.isReady()
  const wrapper = mount(defineComponent({ name: 'Harness', setup: () => () => h(RouterView) }), {
    global: { plugins: [pinia, ElementPlus, router] },
  })
  await flushPromises()
  return wrapper
}

describe('MainLayout 侧栏折叠（工单 18）', () => {
  let wrapper: VueWrapper | null = null

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    localStorage.removeItem(SIDEBAR_KEY)
  })

  it('默认展开：el-menu 无 collapse 类、aside 无折叠类且宽度 220px、toggle 播报折叠侧栏', async () => {
    wrapper = await mountLayout()
    const menu = wrapper.find('.el-menu')
    expect(menu.exists()).toBe(true)
    expect(menu.classes()).not.toContain('el-menu--collapse')
    const aside = wrapper.find('aside.el-aside')
    expect(aside.classes()).not.toContain('is-collapsed')
    expect(aside.attributes('style')).toContain('220px')
    const toggle = wrapper.find('[data-test="sidebar-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.attributes('aria-label')).toBe('折叠侧栏')
  })

  it('点击 toggle：el-menu 含 collapse 类、aside 切折叠类与 64px 宽度、brand 切单字、localStorage 写 1', async () => {
    wrapper = await mountLayout()
    const toggle = wrapper.find('[data-test="sidebar-toggle"]')
    await toggle.trigger('click')
    await flushPromises()
    expect(wrapper.find('.el-menu').classes()).toContain('el-menu--collapse')
    const aside = wrapper.find('aside.el-aside')
    expect(aside.classes()).toContain('is-collapsed')
    expect(aside.attributes('style')).toContain('64px')
    expect(wrapper.text()).toContain('企')
    expect(wrapper.text()).not.toContain('企业智能工作台')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-label')).toBe('展开侧栏')
    expect(localStorage.getItem(SIDEBAR_KEY)).toBe('1')
  })

  it('localStorage 预置 1：再挂载保持折叠态', async () => {
    localStorage.setItem(SIDEBAR_KEY, '1')
    wrapper = await mountLayout()
    expect(wrapper.find('.el-menu').classes()).toContain('el-menu--collapse')
    expect(wrapper.find('aside.el-aside').classes()).toContain('is-collapsed')
    expect(wrapper.find('[data-test="sidebar-toggle"]').attributes('aria-expanded')).toBe('false')
  })

  it('localStorage 非法值 garbage：容错回展开态', async () => {
    localStorage.setItem(SIDEBAR_KEY, 'garbage')
    wrapper = await mountLayout()
    expect(wrapper.find('.el-menu').classes()).not.toContain('el-menu--collapse')
    expect(wrapper.find('aside.el-aside').classes()).not.toContain('is-collapsed')
    expect(wrapper.find('[data-test="sidebar-toggle"]').attributes('aria-expanded')).toBe('true')
  })

  it('aria 随态切换：展开→折叠→再展开全程 label 与 expanded 反转，恢复后 localStorage 回 0', async () => {
    wrapper = await mountLayout()
    const toggle = wrapper.find('[data-test="sidebar-toggle"]')
    expect(toggle.attributes('aria-label')).toBe('折叠侧栏')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    await toggle.trigger('click')
    await flushPromises()
    expect(toggle.attributes('aria-label')).toBe('展开侧栏')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')
    await flushPromises()
    expect(toggle.attributes('aria-label')).toBe('折叠侧栏')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('.el-menu').classes()).not.toContain('el-menu--collapse')
    expect(localStorage.getItem(SIDEBAR_KEY)).toBe('0')
  })

  it('菜单原文守账词：问题反馈管理/评测管理/运营概览 仍在（与 help-sync 双保险）', async () => {
    wrapper = await mountLayout()
    const text = wrapper.text()
    expect(text).toContain('问题反馈管理')
    expect(text).toContain('评测管理')
    expect(text).toContain('运营概览')
  })
})
