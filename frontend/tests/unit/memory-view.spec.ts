/**
 * 我的记忆视图测试（agent-ui-design §2）：开关置顶行（初值来自 /auth/me、加载前
 * disabled、切换即保存失败回滚、关闭后冻结 alert 与添加入口禁用）、手动添加
 * （1-200 校验禁发、IME 守卫、成功清空回第 1 页）、删除（ElMessageBox 确认/
 * 取消不发请求、删空回退一页 §22.3）、空态双分支（未积累 vs 筛选无结果 §2.5）。
 * 数据逻辑在后端（tests/api/test_memory.py），此处只锁展示与交互（照
 * services-health-panel.spec.ts 惯例 mock api 模块）。
 */
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import type { MessageBoxData } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  addMemory,
  deleteMemory,
  listMemories,
  updateMemorySettings,
  type MemoryItem,
} from '@/api/memory'
import { useAuthStore } from '@/stores/auth'
import MemoryView from '@/views/MemoryView.vue'

vi.mock('@/api/memory', () => ({
  listMemories: vi.fn(),
  addMemory: vi.fn(),
  deleteMemory: vi.fn(),
  updateMemorySettings: vi.fn(),
}))

const mocked = {
  list: vi.mocked(listMemories),
  add: vi.mocked(addMemory),
  del: vi.mocked(deleteMemory),
  settings: vi.mocked(updateMemorySettings),
}

const T0 = '2026-10-06T10:24:00+08:00'

function memoryItem(overrides: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id: 1,
    content: '我负责华东区客服，回复时请用中文',
    source: 'manual',
    sourceMessageId: null,
    createdAt: T0,
    ...overrides,
  }
}

function emptyPage() {
  return { items: [], total: 0, page: 1, pageSize: 20 }
}

/** 预置已登录用户（路由守卫 ensureLoaded 之后的等价态），再挂载视图 */
async function mountView(memoryEnabled = true): Promise<VueWrapper> {
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.user = {
    userId: 7,
    username: 'alice',
    role: 'member',
    mustChangePassword: false,
    memoryEnabled,
  }
  auth.loaded = true
  const wrapper = mount(MemoryView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

// el-table 列宽监听在 jsdom 依赖 ResizeObserver
vi.setConfig({ testTimeout: 30_000 })

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  vi.clearAllMocks()
  mocked.list.mockResolvedValue(emptyPage())
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('我的记忆', () => {
  it('挂载即拉列表；开关初值取 /auth/me 的 memoryEnabled 且已探明时不禁用', async () => {
    mocked.list.mockResolvedValue({
      items: [
        memoryItem(),
        memoryItem({ id: 2, content: '倾向先给结论再给依据', source: 'auto', sourceMessageId: 99 }),
      ],
      total: 2,
      page: 1,
      pageSize: 20,
    })
    const wrapper = await mountView(true)
    expect(mocked.list).toHaveBeenCalledWith({ keyword: '', page: 1, pageSize: 20 })
    const switchEl = wrapper.find('[data-test="memory-switch"]')
    expect(switchEl.classes()).not.toContain('is-disabled')
    expect(switchEl.classes()).toContain('is-checked')
    const text = wrapper.text()
    expect(text).toContain('我负责华东区客服，回复时请用中文')
    expect(text).toContain('手动')
    expect(text).toContain('自动')
    expect(text).toContain('开启后，回答会参考你的记忆') // 总闸说明常驻首屏（§2.1）
  })

  it('登录态未探明时开关禁用（避免先渲染默认值再跳变）', async () => {
    setActivePinia(createPinia())
    const wrapper = mount(MemoryView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.find('[data-test="memory-switch"]').classes()).toContain('is-disabled')
  })

  it('开关切换即保存：调 settings 接口，关闭后出现冻结 alert 且添加入口禁用', async () => {
    mocked.settings.mockResolvedValue({ memoryEnabled: false })
    mocked.list.mockResolvedValue({
      items: [memoryItem()],
      total: 1,
      page: 1,
      pageSize: 20,
    })
    const wrapper = await mountView(true)
    await wrapper.find('[data-test="memory-switch"]').trigger('click')
    await flushPromises()
    expect(mocked.settings).toHaveBeenCalledWith(false)
    expect(wrapper.find('[data-test="memory-switch"]').classes()).not.toContain('is-checked')
    expect(wrapper.find('[data-test="memory-frozen-alert"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('记忆功能已关闭')
    // 添加入口联动禁用（§2.7）：输入框与按钮同步 disabled；删除保持可用（清理动作不受限）
    expect(wrapper.find('input[data-test="memory-draft"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="memory-add"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="memory-delete"]').attributes('disabled')).toBeUndefined()
  })

  it('开关保存失败回滚开关状态且不出现冻结 alert', async () => {
    mocked.settings.mockRejectedValueOnce(new Error('500'))
    const wrapper = await mountView(true)
    await wrapper.find('[data-test="memory-switch"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="memory-switch"]').classes()).toContain('is-checked')
    expect(wrapper.find('[data-test="memory-frozen-alert"]').exists()).toBe(false)
  })

  it('手动添加：Enter 提交 strip 后内容，成功清空并回第 1 页刷新', async () => {
    mocked.add.mockResolvedValue(memoryItem({ id: 3, content: '新画像' }))
    const wrapper = await mountView(true)
    mocked.list.mockClear()
    const input = wrapper.find('input[data-test="memory-draft"]')
    await input.setValue('  新画像  ')
    await input.trigger('keydown.enter')
    await flushPromises()
    expect(mocked.add).toHaveBeenCalledTimes(1)
    expect(mocked.add).toHaveBeenCalledWith('新画像')
    expect((input.element as HTMLInputElement).value).toBe('') // 成功清空
    expect(mocked.list).toHaveBeenCalledWith({ keyword: '', page: 1, pageSize: 20 }) // 回第 1 页
  })

  it('中文输入法选词回车不发送（isComposing / keyCode 229 守卫）', async () => {
    const wrapper = await mountView(true)
    const native = wrapper.find('input[data-test="memory-draft"]').element as HTMLInputElement
    native.value = '选词中'
    native.dispatchEvent(new Event('input', { bubbles: true }))
    const composing = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    Object.defineProperty(composing, 'isComposing', { value: true })
    native.dispatchEvent(composing)
    await flushPromises()
    expect(mocked.add).not.toHaveBeenCalled()
    const keyCode229 = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
    Object.defineProperty(keyCode229, 'keyCode', { value: 229 })
    native.dispatchEvent(keyCode229)
    await flushPromises()
    expect(mocked.add).not.toHaveBeenCalled()
  })

  it('超限（>200 字）与空内容禁发：添加按钮 disabled，不发请求', async () => {
    const wrapper = await mountView(true)
    const input = wrapper.find('input[data-test="memory-draft"]')
    await input.setValue('字'.repeat(201))
    expect(wrapper.find('[data-test="memory-add"]').attributes('disabled')).toBeDefined()
    await input.setValue('   ')
    expect(wrapper.find('[data-test="memory-add"]').attributes('disabled')).toBeDefined()
    expect(mocked.add).not.toHaveBeenCalled()
  })

  it('删除：取消确认不发请求；确认后软删并刷新', async () => {
    const confirmSpy = vi
      .spyOn(ElMessageBox, 'confirm')
      .mockRejectedValueOnce('cancel')
      .mockResolvedValue({ action: 'confirm', value: '' } as never)
    mocked.list.mockResolvedValue({
      items: [memoryItem(), memoryItem({ id: 2 })],
      total: 2,
      page: 1,
      pageSize: 20,
    })
    const wrapper = await mountView(true)
    mocked.del.mockResolvedValue({ id: 1, isActive: false })
    await wrapper.find('[data-test="memory-delete"]').trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(mocked.del).not.toHaveBeenCalled() // 取消归取消
    await wrapper.find('[data-test="memory-delete"]').trigger('click')
    await flushPromises()
    expect(mocked.del).toHaveBeenCalledWith(1)
    expect(mocked.list).toHaveBeenCalledTimes(2) // 挂载 + 确认后刷新（取消那次不刷）
  })

  it('末页删空回退一页（§22.3）', async () => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({
      action: 'confirm',
      value: '',
    } as never)
    // total=21 使 el-pagination（20/页）出两页：第 2 页仅 1 条，删空后应回退第 1 页
    mocked.list.mockImplementation(({ page }) =>
      Promise.resolve(
        page === 1
          ? { items: [memoryItem(), memoryItem({ id: 2 })], total: 21, page: 1, pageSize: 20 }
          : {
              items: [memoryItem({ id: 3, content: '末页仅一条' })],
              total: 21,
              page: 2,
              pageSize: 20,
            },
      ),
    )
    const wrapper = await mountView(true)
    await wrapper.find('.btn-next').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('末页仅一条')
    mocked.del.mockResolvedValue({ id: 3, isActive: false })
    await wrapper.find('[data-test="memory-delete"]').trigger('click')
    await flushPromises()
    expect(mocked.list).toHaveBeenLastCalledWith({ keyword: '', page: 1, pageSize: 20 })
  })

  it('空态双分支：未积累显示引导（可聚焦输入框）；筛选无结果显示清除筛选', async () => {
    mocked.list.mockResolvedValue(emptyPage())
    const wrapper = await mountView(true)
    expect(wrapper.find('[data-test="memory-empty-blank"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('还没有记忆')
    const guide = wrapper.find('[data-test="memory-empty-blank"] button')
    expect(guide.text()).toBe('手动添加一条') // 开关开启时渲染引导
    // 引导动作 = 聚焦添加输入框（§2.5）：jsdom 点击的默认聚焦行为在 handler 之后
    // 生效会盖过 activeElement，故 spy 原型 focus 断言（本页唯一被聚焦的输入框即草稿框）
    const focusSpy = vi.spyOn(HTMLInputElement.prototype, 'focus')
    await guide.trigger('click')
    expect(focusSpy).toHaveBeenCalledTimes(1)

    await wrapper.find('input[data-test="memory-keyword"]').setValue('不存在的关键字')
    await wrapper.find('[data-test="memory-search"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="memory-empty-filtered"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('没有匹配筛选条件的记忆')
    await wrapper.find('[data-test="memory-empty-filtered"] button').trigger('click') // 清除筛选
    await flushPromises()
    expect(wrapper.find('[data-test="memory-empty-blank"]').exists()).toBe(true)
    expect(mocked.list).toHaveBeenLastCalledWith({ keyword: '', page: 1, pageSize: 20 })
  })

  it('开关关闭时空态不渲染"手动添加一条"引导（避免引导进死胡同 §2.3）', async () => {
    mocked.list.mockResolvedValue(emptyPage())
    const wrapper = await mountView(false)
    expect(wrapper.find('[data-test="memory-empty-blank"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="memory-empty-blank"] button').exists()).toBe(false)
  })

  it('超限（>200 字）回车提交给出明确警示而非静默吞掉（B7）', async () => {
    const warningSpy = vi.spyOn(ElMessage, 'warning')
    const wrapper = await mountView(true)
    const input = wrapper.find('input[data-test="memory-draft"]')
    await input.setValue('字'.repeat(201))
    await input.trigger('keydown.enter')
    await flushPromises()
    expect(warningSpy).toHaveBeenCalledWith('记忆内容不能超过 200 字')
    expect(mocked.add).not.toHaveBeenCalled()
    // 回到限内后回车正常提交（警示只针对超限路径）
    warningSpy.mockRestore()
    await input.setValue('限内内容')
    await input.trigger('keydown.enter')
    await flushPromises()
    expect(mocked.add).toHaveBeenCalledTimes(1)
  })

  it('开关切换在飞锁定：请求挂起期间开关禁用且重复触发不叠加请求（B7）', async () => {
    let resolveSettings: (value: { memoryEnabled: boolean }) => void = () => undefined
    mocked.settings.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSettings = resolve
        }),
    )
    const wrapper = await mountView(true)
    const switchEl = () => wrapper.find('[data-test="memory-switch"]')
    const firstClick = switchEl().trigger('click')
    await flushPromises()
    expect(switchEl().classes()).toContain('is-disabled') // 在飞禁用（防并发保存）
    await switchEl().trigger('click') // 在飞重复触发被吞
    resolveSettings({ memoryEnabled: false })
    await firstClick
    await flushPromises()
    expect(mocked.settings).toHaveBeenCalledTimes(1)
    expect(switchEl().classes()).not.toContain('is-disabled')
    expect(switchEl().classes()).not.toContain('is-checked') // 以后端落库值校准
    expect(wrapper.find('[data-test="memory-frozen-alert"]').exists()).toBe(true)
  })

  it('手动添加在飞锁定：按钮 loading 且重复提交不叠加请求（B7）', async () => {
    let resolveAdd: (value: MemoryItem) => void = () => undefined
    mocked.add.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveAdd = resolve
        }),
    )
    const wrapper = await mountView(true)
    const input = wrapper.find('input[data-test="memory-draft"]')
    await input.setValue('新画像')
    const addBtn = () => wrapper.find('[data-test="memory-add"]')
    const firstClick = addBtn().trigger('click')
    await flushPromises()
    expect(addBtn().classes()).toContain('is-loading')
    await addBtn().trigger('click') // 在飞重复提交被吞
    resolveAdd(memoryItem({ id: 3, content: '新画像' }))
    await firstClick
    await flushPromises()
    expect(mocked.add).toHaveBeenCalledTimes(1)
    expect((input.element as HTMLInputElement).value).toBe('') // 成功清空不受在飞锁影响
  })

  it('删除请求在飞防连点：确认框挂起期间再次点击不重复弹确认（B8）', async () => {
    let resolveConfirm: (value: MessageBoxData) => void = () => undefined
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm').mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveConfirm = resolve
        }),
    )
    mocked.list.mockResolvedValue({ items: [memoryItem()], total: 1, page: 1, pageSize: 20 })
    const wrapper = await mountView(true)
    mocked.del.mockResolvedValue({ id: 1, isActive: false })
    const delBtn = () => wrapper.find('[data-test="memory-delete"]')
    const firstClick = delBtn().trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(delBtn().attributes('disabled')).toBeDefined() // 在飞禁用（B8 行级防重）
    await delBtn().trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledTimes(1) // 第二次点击被吞，不重复弹确认
    resolveConfirm({ action: 'confirm', value: '' } as unknown as MessageBoxData)
    await firstClick
    await flushPromises()
    expect(mocked.del).toHaveBeenCalledTimes(1)
    expect(delBtn().attributes('disabled')).toBeUndefined() // 收口后解锁
  })

  it('加载失败空态显示"加载失败，点击重试"而非"还没有"，重试成功恢复列表（B11）', async () => {
    mocked.list.mockRejectedValueOnce(new Error('network down'))
    const wrapper = await mountView(true)
    expect(wrapper.find('[data-test="memory-load-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载失败，点击重试')
    expect(wrapper.text()).not.toContain('还没有记忆') // 失败 ≠ 没有数据
    expect(wrapper.text()).not.toContain('没有匹配筛选条件的记忆')
    mocked.list.mockResolvedValue({ items: [memoryItem()], total: 1, page: 1, pageSize: 20 })
    await wrapper.find('[data-test="memory-load-error"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="memory-load-error"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('我负责华东区客服，回复时请用中文')
  })
})
