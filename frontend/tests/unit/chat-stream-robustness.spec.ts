/**
 * P2 问答工作台——SSE 流健壮性与发送中停止（T8 评审清单①②③，从 chat-view.spec.ts
 * 按 describe 域拆出，断言未改动；共享 mock 后端见 helpers/chat-mock.ts）。
 * 覆盖：中途网络断流可重试（①）、心跳/坏帧/未知事件名不中止流（②）、
 * AbortController 取消生成（③）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  Gate,
  ask,
  backend,
  doneFrame,
  fetchMock,
  frame,
  mountChat,
  resetBackend,
  routeFetch,
  sendButton,
  streamCalls,
  tokenFrame,
  waitFor,
} from './helpers/chat-mock'

// 流式用例每例整挂 ChatView（部分两连挂）+ 实时 waitFor，W6 验收期高负载机器上 5s
// 默认超时连续打满——T5"拆分文件维持现状"裁决的前提（当时 217 全绿）已不成立，
// 按 chat-view/chat-feedback 同口径补设（W6 Task 3 披露项，非静默改裁决）
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

describe('SSE 健壮性与停止（T8 评审清单①②③）', () => {
  it('中途网络断流（read 抛 TypeError）：失败态"网络异常"可重试，重试新流成功（清单①）', async () => {
    backend.streamQueue = [
      [{ text: tokenFrame('根据资料'), crash: 'network' }],
      [{ text: `${tokenFrame('根据资料[1]，48 小时内原路退款。')}${doneFrame()}` }],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '退货流程')
    await waitFor(() => wrapper.text().includes('网络异常，请检查网络后重试'), '断流失败文案内联')
    expect(wrapper.text()).toContain('生成失败')
    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')
    expect(retry, '断流失败可重试').toBeDefined()
    await retry!.trigger('click')
    await waitFor(() => wrapper.text().includes('48 小时内原路退款'), '重试新流完成')
    expect(streamCalls()).toHaveLength(2)
    expect(JSON.parse(String(streamCalls()[1]![1].body)).question).toBe('退货流程')
  })

  it('心跳注释块与坏 JSON 帧跳过不中止流，后续事件正常应用（清单②）', async () => {
    const heartbeat = ': keep-alive\n\n'
    const badFrame = 'event: token\ndata: {oops-not-json}\n\n'
    backend.streamQueue = [
      [
        {
          text: `${heartbeat}${tokenFrame('前面')}${badFrame}${heartbeat}${tokenFrame('后面')}${doneFrame()}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '退货流程')
    await waitFor(
      () => wrapper.find('.bubble.assistant').text().includes('前面后面'),
      '坏帧前后 token 均拼接',
    )
    expect(wrapper.text()).not.toContain('生成失败')
    expect(wrapper.text()).toContain('token: in 12 / out 34 · 56ms')
  })

  it('未知事件名忽略：不判 error、流继续到 done（清单②）', async () => {
    backend.streamQueue = [
      [
        {
          text: `${tokenFrame('根据资料回答')}${frame('metrics', { elapsedMs: 3 })}${doneFrame()}`,
        },
      ],
    ]
    const wrapper = await mountChat()
    await ask(wrapper, '退货流程')
    await waitFor(() => wrapper.text().includes('根据资料回答'), '未知事件后流继续完成')
    expect(wrapper.text()).not.toContain('生成失败')
  })

  it('发送中点"停止"：fetch signal 已中止，气泡标"已停止"可重试，重试同问题新发一条（清单③）', async () => {
    const gate = new Gate()
    // 首段 token 即到（流式中），done 段被 gate 扣住保持流式态
    backend.streamQueue = [[{ text: tokenFrame('部分回答') }, { text: doneFrame(), gate }]]
    const wrapper = await mountChat()
    await wrapper.find('textarea').setValue('差旅报销标准是多少')
    await sendButton(wrapper).trigger('click')
    await waitFor(() => wrapper.text().includes('部分回答'), '首段 token 到达')
    const stop = wrapper.findAll('button').find((b) => b.text() === '停止')
    expect(stop, '发送中显示停止按钮').toBeDefined()
    await stop!.trigger('click')
    await waitFor(() => wrapper.text().includes('已停止'), '气泡标已停止')
    expect(wrapper.text()).not.toContain('生成失败')
    const first = streamCalls()[0]!
    expect((first[1].signal as AbortSignal).aborted).toBe(true)
    backend.streamQueue = [[{ text: `${tokenFrame('完整回答内容')}${doneFrame()}` }]]
    const retry = wrapper.findAll('button').find((b) => b.text() === '重试')
    expect(retry, '停止后可重问').toBeDefined()
    await retry!.trigger('click')
    await waitFor(() => wrapper.text().includes('完整回答内容'), '重问流完成')
    expect(streamCalls()).toHaveLength(2)
    expect(JSON.parse(String(streamCalls()[1]![1].body)).question).toBe('差旅报销标准是多少')
  })
})
