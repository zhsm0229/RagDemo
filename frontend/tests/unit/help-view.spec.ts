/**
 * 帮助中心页测试：纯静态内容页，无 API 依赖——冒烟覆盖三条主线：
 * 1) 员工视角（isAdmin=false）：可见页签不含"管理员指南"，且全文不出现管理页签标题；
 * 2) 管理员视角（isAdmin=true）：五个页签齐全，含"管理员指南"；
 * 3) 内容块渲染分发：步骤（有序列表）、要点（无序列表）、对照表（原生 table 表头）、
 *    提示（el-alert）各至少渲染一处，演示模式警示文案可见（2026-10 试点反馈新增）。
 * 内容数据与 docs/user-guide.md 同源，断言锚定界面原文（如"知识库中未找到相关内容"）。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { defineComponent, h } from 'vue'
import { useAuthStore } from '@/stores/auth'
import HelpView from '@/views/HelpView.vue'

async function mountHelp(role: 'admin' | 'member'): Promise<VueWrapper> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = {
    userId: 1,
    username: 'tester',
    role,
    mustChangePassword: false,
    memoryEnabled: true,
  }
  const wrapper = mount(HelpView, {
    global: { plugins: [pinia, ElementPlus] },
  })
  await flushPromises()
  return wrapper
}

/** M40：带 router 上下文挂载（query.tab 同步依赖 useRoute/useRouter） */
async function mountHelpAtQuery(
  query: Record<string, string>,
  role: 'admin' | 'member' = 'admin',
): Promise<{ wrapper: VueWrapper; router: ReturnType<typeof createRouter> }> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.user = {
    userId: 1,
    username: 'tester',
    role,
    mustChangePassword: false,
    memoryEnabled: true,
  }
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/help', name: 'help', component: HelpView },
      { path: '/', name: 'chat', component: defineComponent({ render: () => null }) },
    ],
  })
  await router.push({ path: '/help', query })
  await router.isReady()
  const wrapper = mount(defineComponent({ name: 'Harness', setup: () => () => h(RouterView) }), {
    global: { plugins: [pinia, ElementPlus, router] },
  })
  await flushPromises()
  return { wrapper, router }
}

function activeTabText(wrapper: VueWrapper): string {
  return wrapper.find('.el-tabs__item.is-active').text()
}

describe('帮助中心页', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('员工视角：不出现"管理员指南"页签，员工内容可见', async () => {
    const wrapper = await mountHelp('member')
    const text = wrapper.text()
    expect(text).not.toContain('管理员指南')
    expect(text).toContain('快速上手')
    expect(text).toContain('提问技巧')
    expect(text).toContain('常见问题')
  })

  it('管理员视角：五个页签齐全，含"管理员指南"', async () => {
    const wrapper = await mountHelp('admin')
    const text = wrapper.text()
    expect(text).toContain('快速上手')
    expect(text).toContain('提问技巧')
    expect(text).toContain('员工功能')
    expect(text).toContain('管理员指南')
    expect(text).toContain('常见问题')
  })

  it('内容块渲染：步骤/要点/对照表/提示齐备，拒答话术与演示模式警示可见', async () => {
    const wrapper = await mountHelp('admin')
    expect(wrapper.find('.steps').exists()).toBe(true)
    expect(wrapper.find('.tips').exists()).toBe(true)
    expect(wrapper.find('.table').exists()).toBe(true)
    expect(wrapper.find('.note').exists()).toBe(true)
    const text = wrapper.text()
    // 与后端拒答红线文案逐字一致（rag/prompt.py REFUSAL_TEXT）
    expect(text).toContain('知识库中未找到相关内容')
    // 演示模式辨别（TEST_MODE stub 模板文案特征 + 处置指引）
    expect(text).toContain('根据知识库检索结果')
    expect(text).toContain('TEST_MODE')
  })

  it('挂载读 query.tab 选中对应页签；切换页签 router.replace 写回 query（M40）', async () => {
    const { wrapper, router } = await mountHelpAtQuery({ tab: 'faq' })
    expect(activeTabText(wrapper)).toBe('常见问题')

    // 切换页签 → query.tab 写回（replace 不留历史，其余键保留）
    await router.push({ query: { tab: 'faq', keepMe: '1' } })
    await flushPromises()
    const skillTab = wrapper.findAll('.el-tabs__item').find((t) => t.text() === '提问技巧')!
    await skillTab.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query.tab).toBe('skills')
    expect(router.currentRoute.value.query.keepMe).toBe('1')
    expect(activeTabText(wrapper)).toBe('提问技巧')
  })

  it('query.tab 非法值回落首个页签；成员带 adminOnly 页签名同样回落（不越权）', async () => {
    const { wrapper } = await mountHelpAtQuery({ tab: 'not-a-tab' })
    expect(activeTabText(wrapper)).toBe('快速上手')
    const member = await mountHelpAtQuery({ tab: 'admin-guide' }, 'member')
    try {
      expect(activeTabText(member.wrapper)).toBe('快速上手')
    } finally {
      member.wrapper.unmount()
    }
  })
})
