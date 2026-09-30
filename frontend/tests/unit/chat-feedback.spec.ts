/**
 * P2 问答工作台——引用溯源与消息反馈（F-202/F-208，从 chat-view.spec.ts 按
 * describe 域拆出，断言未改动；共享 mock 后端见 helpers/chat-mock.ts）。
 * 覆盖：角标↔面板联动、原文核对弹窗三态（doc/qa）、点赞轻交互、
 * 点踩弹窗（前端校验拦截 + 后端 422 兜底内联）、历史已反馈渲染。
 */
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  CITE,
  CITES,
  assistantItem,
  backend,
  conv,
  contentCalls,
  fetchMock,
  giveContent,
  mountChat,
  resetBackend,
  routeFetch,
  userItem,
  waitFor,
} from './helpers/chat-mock'

// SSE 流时序用例（等待多帧事件）在 CI/慢机上可能超过默认 5s 单测超时（T3 评审裁定
// 与其余 9 个重型 view spec 对齐补设；chat-stream-robustness 拆分文件维持现状）
vi.setConfig({ testTimeout: 30_000 })

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock)
  // jsdom 未实现 scrollIntoView（角标→面板滚动定位用）
  Element.prototype.scrollIntoView = vi.fn()
  resetBackend()
  fetchMock.mockImplementation(routeFetch)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

// ---- 弹窗操作（el-dialog 默认 appendToBody=false 渲染在组件树内，优先在 wrapper 内找） ----

function dialogEl(wrapper: VueWrapper): HTMLElement {
  const el =
    (wrapper.element.querySelector('.el-dialog') as HTMLElement | null) ??
    (document.body.querySelector('.el-dialog') as HTMLElement | null)
  expect(el, '弹窗应已打开').not.toBeNull()
  return el!
}

function dialogText(wrapper: VueWrapper): string {
  return dialogEl(wrapper).textContent ?? ''
}

async function clickDialogButton(wrapper: VueWrapper, text: string): Promise<void> {
  const btn = Array.from(dialogEl(wrapper).querySelectorAll('button')).find(
    (b) => b.textContent?.trim() === text,
  )
  expect(btn, `弹窗按钮 ${text} 应存在`).toBeDefined()
  ;(btn as HTMLElement).click()
  await flushPromises()
}

async function chooseCategory(wrapper: VueWrapper, label: string): Promise<void> {
  const radio = Array.from(dialogEl(wrapper).querySelectorAll('.el-radio')).find((r) =>
    r.textContent?.includes(label),
  )
  expect(radio, `分类项 ${label} 应存在`).toBeDefined()
  // jsdom 对 radio input.click() 不触发 EP 绑定的 change，手工置选并派发冒泡 change
  const input = (radio as HTMLElement).querySelector('input') as HTMLInputElement
  input.checked = true
  input.dispatchEvent(new Event('change', { bubbles: true }))
  await flushPromises()
}

function dialogTextarea(wrapper: VueWrapper): HTMLTextAreaElement {
  const ta = dialogEl(wrapper).querySelector('textarea')
  expect(ta, '弹窗正文输入框应存在').not.toBeNull()
  return ta as HTMLTextAreaElement
}

/** 轮询用谓词：正文 textarea 未渲染（loading/失败态）时返回 false 而非抛错 */
function textareaValueIncludes(wrapper: VueWrapper, text: string): boolean {
  const ta = dialogEl(wrapper).querySelector('textarea')
  return ta !== null && (ta as HTMLTextAreaElement).value.includes(text)
}

async function setComment(wrapper: VueWrapper, value: string): Promise<void> {
  const textarea = dialogEl(wrapper).querySelector('textarea')
  expect(textarea, '备注输入框应存在').not.toBeNull()
  const el = textarea as HTMLTextAreaElement
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flushPromises()
}

// ---- 用例 ----

describe('引用联动与原文核对弹窗（F-202，kotaemon 式溯源）', () => {
  it('点气泡角标 → 右侧面板对应卡片高亮（滚动定位）', async () => {
    backend.conversations = [conv(1, '退货流程问答')]
    backend.history.set(1, [
      userItem(11, '退货流程是什么？'),
      assistantItem(12, '根据资料[1]，48 小时内原路退款。', CITE),
    ])
    const wrapper = await mountChat()
    expect(wrapper.find('.cite-card').classes()).not.toContain('hl')
    await wrapper.find('.bubble.assistant .cite').trigger('click')
    await flushPromises()
    expect(wrapper.find('.cite-card').classes()).toContain('hl')
  })

  it('点引用卡 → "查看原文"弹窗：调 citations/content，展示标题/页码/位置 + 正文 readOnly 置灰（doc/qa 各按 refType 查询）', async () => {
    backend.conversations = [conv(1, '退货流程问答')]
    backend.history.set(1, [
      userItem(11, '两连问'),
      assistantItem(12, '根据资料[1][2]作答。', CITES),
    ])
    giveContent('doc', 11, '3.2 差旅报销标准：住宿费每晚 400 元封顶，须提供发票。')
    giveContent('qa', 7, 'Q：保修期多久？A：整机保修 24 个月。')

    // doc 卡（独立挂载：jsdom 下 EP 弹窗 X/ESC 不触发 close，为环境限制而非实现缺陷）
    const wrapper = await mountChat()
    await wrapper.findAll('.cite-card')[0]!.trigger('click')
    // 标题/页码/位置来自 citation 兜底即刻可见；正文在 textarea value（textContent 不含）
    expect(dialogText(wrapper)).toContain('客服手册')
    await waitFor(() => textareaValueIncludes(wrapper, '住宿费每晚 400 元封顶'), 'doc 正文加载展示')
    expect(dialogText(wrapper)).toContain('第3章>3.2')
    expect(dialogText(wrapper)).toContain('相似度：0.91')
    const ta = dialogTextarea(wrapper)
    expect(ta.readOnly).toBe(true)
    expect(ta.value).toContain('3.2 差旅报销标准')
    expect(dialogEl(wrapper).querySelector('.readonly-grey')).not.toBeNull()
    expect(contentCalls()[0]).toContain('refType=doc&refId=11')
    // 点卡片同时高亮气泡对应角标（task-9 brief：点卡片高亮对应角标）
    expect(wrapper.find('.bubble.assistant .cite.hl').exists()).toBe(true)
    wrapper.unmount()

    // qa 卡：refType=qa 查问答对，正文为答案文本
    const qaWrapper = await mountChat()
    await qaWrapper.findAll('.cite-card')[1]!.trigger('click')
    expect(dialogText(qaWrapper)).toContain('FAQ问答对')
    await waitFor(() => textareaValueIncludes(qaWrapper, '整机保修 24 个月'), 'qa 答案文本加载展示')
    expect(contentCalls()).toHaveLength(2)
    expect(contentCalls()[1]).toContain('refType=qa&refId=7')
  })

  it('原文弹窗 404/3003 失败态：错误内联弹窗不关，重试成功后展示正文', async () => {
    backend.conversations = [conv(1, '退货流程问答')]
    backend.history.set(1, [
      userItem(11, '退货流程是什么？'),
      assistantItem(12, '根据资料[1]作答。', CITE),
    ])
    const wrapper = await mountChat()
    await wrapper.find('.cite-card').trigger('click')
    await waitFor(() => dialogText(wrapper).includes('引用不存在或无权访问'), '失败错误内联')
    expect(dialogEl(wrapper).querySelector('.err-inline')).not.toBeNull()
    const retryBtn = Array.from(dialogEl(wrapper).querySelectorAll('button')).find((b) =>
      b.textContent?.includes('重试'),
    )
    expect(retryBtn, '失败态提供重试按钮').toBeDefined()
    giveContent('doc', 11, '3.2 差旅报销标准：住宿费每晚 400 元封顶。')
    await retryBtn!.click()
    await flushPromises()
    await waitFor(() => textareaValueIncludes(wrapper, '住宿费每晚 400 元封顶'), '重试后展示正文')
    expect(dialogText(wrapper)).not.toContain('引用不存在或无权访问')
  })
})

describe('消息反馈（F-208/CHAT-09）', () => {
  it('点赞轻交互：POST feedback up，meta 👍 高亮', async () => {
    backend.conversations = [conv(1, '退货流程问答')]
    backend.history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答内容', CITE)])
    const wrapper = await mountChat()
    const like = wrapper.findAll('button').find((b) => b.text() === '👍')
    expect(like, '点赞按钮应存在').toBeDefined()
    await like!.trigger('click')
    await flushPromises()
    expect(backend.feedbackCalls).toHaveLength(1)
    expect(backend.feedbackCalls[0]).toMatchObject({ id: 12, body: { feedback: 'up' } })
    const likeAfter = wrapper.findAll('button').find((b) => b.text() === '👍')
    expect(likeAfter!.classes()).toContain('el-button--primary')
  })

  it('点踩弹窗：前端校验拦截（无分类/无备注均不发请求），补齐后提交成功回填"已点踩：分类"', async () => {
    backend.conversations = [conv(1, '退货流程问答')]
    backend.history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答内容', CITE)])
    const wrapper = await mountChat()
    const dislike = wrapper.findAll('button').find((b) => b.text() === '👎')
    await dislike!.trigger('click')
    await flushPromises()
    expect(dialogText(wrapper)).toContain('反馈问题（点踩）')

    await clickDialogButton(wrapper, '提交反馈')
    expect(dialogText(wrapper)).toContain('请选择问题分类')
    expect(backend.feedbackCalls).toHaveLength(0)

    await chooseCategory(wrapper, '答案错误')
    await clickDialogButton(wrapper, '提交反馈')
    expect(dialogText(wrapper)).toContain('点踩必须填写备注（帮助定位 badcase）')
    expect(backend.feedbackCalls).toHaveLength(0)

    await setComment(wrapper, '引用与问题无关')
    await clickDialogButton(wrapper, '提交反馈')
    expect(backend.feedbackCalls).toHaveLength(1)
    expect(backend.feedbackCalls[0]).toMatchObject({
      id: 12,
      body: { feedback: 'down', category: '答案错误', comment: '引用与问题无关' },
    })
    await waitFor(() => wrapper.text().includes('已点踩：答案错误'), 'meta 回填已点踩分类')
  })

  it('点踩后端 422 兜底：错误内联弹窗不关闭，修正后可重提成功', async () => {
    backend.conversations = [conv(1, '退货流程问答')]
    backend.history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答内容', CITE)])
    backend.feedbackError = { code: 1003, message: '备注长度非法' }
    const wrapper = await mountChat()
    const dislike = wrapper.findAll('button').find((b) => b.text() === '👎')
    await dislike!.trigger('click')
    await flushPromises()
    await chooseCategory(wrapper, '答案错误')
    await setComment(wrapper, '引用与问题无关')
    await clickDialogButton(wrapper, '提交反馈')
    await waitFor(() => dialogText(wrapper).includes('备注长度非法'), '422 兜底错误内联')
    expect(backend.feedbackCalls).toHaveLength(1)
    expect(dialogEl(wrapper).querySelector('textarea')).not.toBeNull()
    backend.feedbackError = null
    await clickDialogButton(wrapper, '提交反馈')
    await waitFor(() => wrapper.text().includes('已点踩：答案错误'), '重提成功回填')
    expect(backend.feedbackCalls).toHaveLength(2)
  })

  it('历史已点踩消息：meta 展示"已点踩：答案错误"且 👎 高亮', async () => {
    backend.conversations = [conv(1, '退货流程问答')]
    backend.history.set(1, [
      userItem(11, '退货问题'),
      assistantItem(12, '退货回答内容', CITE, 'rag', false, 'down', '答案错误'),
    ])
    const wrapper = await mountChat()
    await waitFor(() => wrapper.text().includes('已点踩：答案错误'), '历史反馈状态渲染')
    const down = wrapper.findAll('button').find((b) => b.text() === '👎')
    expect(down!.classes()).toContain('el-button--danger')
  })
})
