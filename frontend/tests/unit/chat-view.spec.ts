/**
 * P2 问答工作台集成测试——主文件：会话列表/历史渲染、SSE 事件时序与 done 分支、
 * 输入校验、首问自动建会话、历史中断占位（CHAT-12）。
 * T8 评审清单①②③健壮性用例见 chat-stream-robustness.spec.ts；
 * 引用弹窗与反馈用例见 chat-feedback.spec.ts（共享 mock 后端见 helpers/chat-mock.ts）。
 */
import { flushPromises } from '@vue/test-utils'
import { ElMessageBox } from 'element-plus'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MessageBoxData } from 'element-plus'
import {
  CITE,
  Gate,
  assistantItem,
  backend,
  citationsFrame,
  conv,
  deleteCalls,
  doneFrame,
  errorFrame,
  fetchMock,
  makeResponse,
  mountChat,
  resetBackend,
  routeFetch,
  sendButton,
  ask,
  streamCalls,
  tokenFrame,
  userItem,
  waitFor,
} from './helpers/chat-mock'

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

// ---- 用例 ----

describe('会话列表与历史渲染', () => {
  it('进入页面拉取会话列表并自动选中最近会话，历史按序渲染（引用角标与面板联动）', async () => {
    backend.conversations = [conv(1, '退货流程问答'), conv(2, '年假咨询')]
    backend.history.set(1, [
      userItem(11, '退货流程是什么？'),
      assistantItem(12, '根据资料[1]，48 小时内原路退款。', CITE),
    ])
    const wrapper = await mountChat()
    expect(wrapper.findAll('.conv-item')).toHaveLength(2)
    expect(wrapper.find('.conv-item.active').text()).toContain('退货流程问答')
    expect(wrapper.text()).toContain('退货流程是什么？')
    expect(wrapper.text()).toContain('根据资料')
    expect(wrapper.findAll('.bubble.assistant .cite')).toHaveLength(1)
    expect(wrapper.find('.cite-card').text()).toContain('客服手册')
    expect(wrapper.text()).toContain('相似度 0.91')
  })

  it('点击其他会话切换并拉取该会话历史渲染', async () => {
    backend.conversations = [conv(1, '退货流程问答'), conv(2, '年假咨询')]
    backend.history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答')])
    backend.history.set(2, [userItem(21, '年假问题'), assistantItem(22, '年假回答')])
    const wrapper = await mountChat()
    expect(wrapper.text()).toContain('退货回答')
    await wrapper.findAll('.conv-item')[1]!.trigger('click')
    await waitFor(() => wrapper.text().includes('年假回答'), '切换后渲染目标会话历史')
    expect(wrapper.find('.conv-item.active').text()).toContain('年假咨询')
  })

  it('新会话按钮 POST /conversations 建会话并选中空会话（空态引导可见）', async () => {
    const wrapper = await mountChat()
    const newBtn = wrapper.find('button.new-btn')
    expect(newBtn.exists()).toBe(true)
    await newBtn.trigger('click')
    await flushPromises()
    const postConv = fetchMock.mock.calls.filter(
      (call) => call[0] === '/api/conversations' && (call[1] as RequestInit).method === 'POST',
    )
    expect(postConv).toHaveLength(1)
    expect(wrapper.findAll('.conv-item')).toHaveLength(1)
    expect(wrapper.find('.conv-item.active').text()).toContain('新会话')
    expect(wrapper.text()).toContain('问点什么吧')
  })

  it('会话删除需二次确认：取消不调 DELETE，确认后软删并从列表消失、自动切换下一会话', async () => {
    backend.conversations = [conv(1, '退货流程问答'), conv(2, '年假咨询')]
    backend.history.set(1, [userItem(11, '退货问题'), assistantItem(12, '退货回答')])
    backend.history.set(2, [userItem(21, '年假问题'), assistantItem(22, '年假回答')])
    const wrapper = await mountChat()
    const confirmSpy = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    await wrapper.findAll('.conv-item')[0]!.find('button.del').trigger('click')
    await flushPromises()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(deleteCalls()).toHaveLength(0)

    // MessageBoxData 为 MessageBoxInputData & Action 交叉类型，字面量须经断言绕过其类型瑕疵
    confirmSpy.mockResolvedValue({ action: 'confirm', value: '' } as unknown as MessageBoxData)
    await wrapper.findAll('.conv-item')[0]!.find('button.del').trigger('click')
    await waitFor(() => wrapper.findAll('.conv-item').length === 1, '删除后列表移除')
    expect(deleteCalls()).toHaveLength(1)
    expect(wrapper.text()).not.toContain('退货流程问答')
    await waitFor(() => wrapper.text().includes('年假回答'), '自动切换到下一会话')
    expect(wrapper.find('.conv-item.active').text()).toContain('年假咨询')
  })
})

describe('SSE 流式渲染（mock fetch 流）', () => {
  it('citations 恒先于 token：引用面板先上板，气泡文本随后增量拼接', async () => {
    const gate = new Gate()
    backend.streamQueue = [
      [
        { text: citationsFrame(CITE) },
        {
          text: `${tokenFrame('根据资料')}${tokenFrame('[1]，48 小时内原路退款。')}${doneFrame()}`,
          gate,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准是多少')
    await waitFor(() => wrapper.find('.cite-card').exists(), 'citations 先上板')
    expect(wrapper.find('.bubble.assistant').text()).not.toContain('根据资料')
    gate.open()
    await waitFor(() => wrapper.text().includes('48 小时内原路退款'), 'token 拼接完成')
    expect(wrapper.find('.bubble.assistant').text()).toContain('根据资料')
    expect(wrapper.findAll('.bubble.assistant .cite')).toHaveLength(1)
  })

  it('done cacheHit=true 气泡显示"来自缓存"标注', async () => {
    backend.streamQueue = [
      [{ text: `${tokenFrame('报销标准为每天 200 元。')}${doneFrame({ cacheHit: true })}` }],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准')
    await waitFor(() => wrapper.text().includes('来自缓存'), '缓存标注')
    expect(wrapper.text()).toContain('报销标准为每天 200 元。')
  })

  it('done 事件 promptTokens/completionTokens/durationMs 展示在消息 meta（对照原型 msg-meta）', async () => {
    backend.streamQueue = [
      [
        {
          text: `${tokenFrame('报销标准为每天 200 元。')}${doneFrame({
            promptTokens: 912,
            completionTokens: 36,
            durationMs: 2400,
          })}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准')
    await waitFor(() => wrapper.text().includes('200 元'), '回答完成')
    expect(wrapper.text()).toContain('token: in 912 / out 36 · 2400ms')
  })

  it('done refused=true 展示精确拒答话术，无引用角标、无缓存标注、面板空态', async () => {
    backend.streamQueue = [
      [
        {
          text: `${citationsFrame([])}${tokenFrame('知识库中未找到相关内容')}${doneFrame({ refused: true })}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '公司年会抽奖规则')
    await waitFor(
      () => wrapper.find('.bubble.assistant').text().includes('知识库中未找到相关内容'),
      '拒答话术',
    )
    expect(wrapper.find('.bubble.assistant').text()).toContain('知识库中未找到相关内容')
    expect(wrapper.findAll('.bubble.assistant .cite')).toHaveLength(0)
    expect(wrapper.text()).toContain('已拒答')
    expect(wrapper.text()).not.toContain('来自缓存')
    expect(wrapper.text()).toContain('无引用')
  })

  it('error 事件 retryable：气泡标生成失败+重试按钮；重试以相同问题新发一条（CHAT-12）', async () => {
    backend.streamQueue = [
      [{ text: `${tokenFrame('根据资料[1]')}${errorFrame()}` }],
      [
        {
          text: `${citationsFrame(CITE)}${tokenFrame('根据资料[1]，48 小时内原路退款。')}${doneFrame()}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '退货流程')
    await waitFor(() => wrapper.text().includes('生成失败'), '失败标注')
    expect(wrapper.text()).toContain('生成服务暂时不可用')
    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await waitFor(() => wrapper.findAll('.bubble.user').length === 2, '重试新发 user 气泡')
    await waitFor(() => wrapper.text().includes('48 小时内原路退款'), '重试流完成')
    const calls = streamCalls()
    expect(calls).toHaveLength(2)
    expect(JSON.parse(String(calls[0]![1].body)).question).toBe('退货流程')
    expect(JSON.parse(String(calls[1]![1].body)).question).toBe('退货流程')
    expect(wrapper.text()).toContain('生成失败')
  })

  it('开流前 429/3002 限额错误：气泡内联失败原因且不提供重试按钮', async () => {
    backend.streamQueue = [
      makeResponse(429, 'application/json', null, {
        code: 3002,
        message: '今日提问次数已达上限，请明天再试',
        data: null,
      }),
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准')
    await waitFor(() => wrapper.text().includes('今日提问次数已达上限'), '限额错误内联')
    expect(wrapper.text()).toContain('生成失败')
    expect(wrapper.findAll('button').find((b) => b.text() === '重试')).toBeUndefined()
  })
})

describe('输入校验与键盘行为', () => {
  it('空输入与超 2000 字禁发送：超长计数红色提示且不发起请求', async () => {
    const wrapper = await mountChat()
    expect(sendButton(wrapper).attributes('disabled')).toBeDefined()
    await wrapper.find('textarea').setValue('字'.repeat(2001))
    expect(sendButton(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('2001/2000')
    await sendButton(wrapper).trigger('click')
    await flushPromises()
    expect(streamCalls()).toHaveLength(0)
    await wrapper.find('textarea').setValue('正常问题')
    expect(sendButton(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('Enter 发送问题，Shift+Enter 换行不发送', async () => {
    backend.streamQueue = [[{ text: `${tokenFrame('好的')}${doneFrame()}` }]]
    const wrapper = await mountChat()
    const textarea = wrapper.find('textarea')
    await textarea.setValue('差旅报销标准')
    await textarea.trigger('keydown', { key: 'Enter' })
    await waitFor(() => streamCalls().length === 1, 'Enter 触发发送')
    await textarea.setValue('第二问')
    await textarea.trigger('keydown', { key: 'Enter', shiftKey: true })
    await flushPromises()
    expect(streamCalls()).toHaveLength(1)
  })
})

describe('首问自动建会话', () => {
  it('conversationId 传 null 直接提问，done 后刷新列表并选中后端自建会话（消息桶迁移）', async () => {
    backend.onStream = () => {
      // 后端在首问时自建会话（首问前 20 字命题），此处模拟其落库结果
      backend.conversations = [conv(9, '报销标准是多少'), ...backend.conversations]
    }
    backend.streamQueue = [[{ text: `${tokenFrame('报销标准为每天 200 元。')}${doneFrame()}` }]]
    const wrapper = await mountChat()
    await ask(wrapper, '报销标准是多少')
    await waitFor(() => wrapper.findAll('.conv-item').length === 1, '列表出现自建会话')
    const body = JSON.parse(String(streamCalls()[0]![1].body))
    expect(body.conversationId).toBeNull()
    expect(body.question).toBe('报销标准是多少')
    expect(wrapper.find('.conv-item.active').text()).toContain('报销标准是多少')
    // 消息桶已迁移到真实会话，问答气泡仍在视图上
    expect(wrapper.findAll('.bubble.user')).toHaveLength(1)
    expect(wrapper.text()).toContain('报销标准为每天 200 元。')
  })
})

describe('历史中断占位（CHAT-12）', () => {
  it('user 消息后无 assistant（中断行被过滤）→ 渲染"生成失败可重试"占位', async () => {
    backend.conversations = [conv(1, '退货流程问答')]
    backend.history.set(1, [userItem(11, '退货流程是什么？')])
    const wrapper = await mountChat()
    await waitFor(() => wrapper.text().includes('生成失败'), '中断占位渲染')
    expect(wrapper.text()).toContain('退货流程是什么？')
    expect(wrapper.findAll('.bubble.assistant')).toHaveLength(1)
    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')
    expect(retry).toBeDefined()
    // 占位重试同样走新发一条
    backend.streamQueue = [[{ text: `${tokenFrame('48 小时内原路退款。')}${doneFrame()}` }]]
    await retry!.trigger('click')
    await waitFor(() => streamCalls().length === 1, '占位重试发起新流')
    expect(JSON.parse(String(streamCalls()[0]![1].body)).question).toBe('退货流程是什么？')
  })
})
