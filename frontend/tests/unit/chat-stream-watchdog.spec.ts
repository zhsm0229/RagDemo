/**
 * 问答流看门狗（A8，从 useAgentRun 移植三件套）：45s 停滞判停 + 300s 总时长兜底
 * + 事件回调喂狗 + finally 清理。纯 composable 层单测：mock @/api/chat.streamChat
 * 持流不放（abort 即以 ABORTED_CODE 拒绝，同真 client 语义），fake timers 驱动时间。
 * 锁定行为：停滞/超时 → 可重试失败态（文案区分于用户停止）；正常流事件喂狗不误杀；
 * 终态后计时器拆除不再触发。
 */
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ABORTED_CODE, ApiClientError } from '@/api/client'
import {
  STALL_TIMEOUT_MS,
  TOTAL_TIMEOUT_MS,
  useChatStream,
  type ChatMessage,
} from '@/composables/useChatStream'
import type { ChatStreamEvent } from '@/api/types'

const { streamChatMock, openStreams } = vi.hoisted(() => {
  /** 已发起未收口的流（send 期间注册；收口即移除） */
  const openStreams: Array<{
    params: { conversationId: number | null; question: string }
    onEvent: (event: ChatStreamEvent) => void
    signal: AbortSignal
    resolve: () => void
  }> = []
  return { streamChatMock: vi.fn(), openStreams }
})

vi.mock('@/api/chat', () => ({
  NON_RETRYABLE_PRESTREAM_CODES: new Set([1003, 2003, 2004, 3002, 3003]),
  streamChat: streamChatMock,
}))

function lastAssistant(messages: ChatMessage[]): ChatMessage {
  const assistant = messages.filter((m) => m.role === 'assistant').at(-1)
  expect(assistant, '应有 assistant 占位气泡').toBeDefined()
  return assistant!
}

const token = (content: string): ChatStreamEvent =>
  ({ event: 'token', messageId: 1, content }) as ChatStreamEvent
const done = (): ChatStreamEvent =>
  ({
    event: 'done',
    messageId: 1,
    refused: false,
    cacheHit: false,
    promptTokens: 1,
    completionTokens: 2,
    durationMs: 3,
  }) as ChatStreamEvent

beforeEach(() => {
  vi.useFakeTimers()
  openStreams.length = 0
  // streamChat 持流不放：abort 即以 ABORTED_CODE 拒绝（同真 streamChat 把 AbortError
  // 转成 ApiClientError 的语义），正常收口由用例显式调 stream.resolve()
  streamChatMock.mockImplementation(
    (
      params: { conversationId: number | null; question: string },
      onEvent: (event: ChatStreamEvent) => void,
      signal: AbortSignal,
    ) =>
      new Promise<void>((resolve, reject) => {
        const onAbort = (): void => {
          reject(new ApiClientError(ABORTED_CODE, '请求已取消'))
        }
        if (signal.aborted) onAbort()
        else signal.addEventListener('abort', onAbort, { once: true })
        openStreams.push({ params, onEvent, signal, resolve })
      }),
  )
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('问答流看门狗（A8）', () => {
  it('45s 无任何事件：停滞判停 → abort 当前请求、可重试失败态"回答长时间无响应，已停止等待"', async () => {
    const settled: Array<number | null> = []
    const chat = useChatStream({ onSettled: (cid) => settled.push(cid) })
    void chat.send('退货流程', null)
    await flushPromises()
    expect(chat.streaming.value).toBe(true)
    expect(openStreams).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(STALL_TIMEOUT_MS - 1)
    expect(lastAssistant(chat.messagesOf(null)).status).toBe('streaming') // 差 1ms 不误杀

    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    const assistant = lastAssistant(chat.messagesOf(null))
    expect(assistant.status).toBe('failed')
    expect(assistant.failure).toBe('回答长时间无响应，已停止等待')
    expect(assistant.retryable).toBe(true)
    expect(assistant.stopped).toBe(false) // 看门狗判停 ≠ 用户停止（不标"已停止"）
    expect(openStreams[0]!.signal.aborted).toBe(true) // 已 abort 当前请求
    expect(chat.streaming.value).toBe(false)
    expect(settled).toEqual([null]) // finally 收口照常回调
  })

  it('事件喂狗：token 间隔 40s（<45s）持续到达不误杀，done 后正常完成且计时器拆除', async () => {
    const chat = useChatStream()
    void chat.send('差旅报销标准', null)
    await flushPromises()
    const stream = openStreams[0]!

    for (let i = 0; i < 3; i++) {
      stream.onEvent(token(`第${i}段`))
      await vi.advanceTimersByTimeAsync(40_000) // 每段间隔 <45s：喂狗生效
    }
    expect(lastAssistant(chat.messagesOf(null)).status).toBe('streaming')

    stream.onEvent(done())
    stream.resolve()
    await flushPromises()
    const assistant = lastAssistant(chat.messagesOf(null))
    expect(assistant.status).toBe('done')
    expect(assistant.content).toBe('第0段第1段第2段')
    expect(chat.streaming.value).toBe(false)

    // finally 清理：终态后推进远超停滞/总兜底阈值，状态不再变化
    await vi.advanceTimersByTimeAsync(TOTAL_TIMEOUT_MS + STALL_TIMEOUT_MS)
    expect(lastAssistant(chat.messagesOf(null)).status).toBe('done')
  })

  it('300s 总时长兜底：事件持续喂狗（每 40s 一段）仍会在 300s 被强判超时', async () => {
    const chat = useChatStream()
    void chat.send('长任务', null)
    await flushPromises()
    const stream = openStreams[0]!

    for (let i = 0; i < 7; i++) {
      stream.onEvent(token(`第${i}段`))
      await vi.advanceTimersByTimeAsync(40_000) // 累计 280s，停滞未触发
    }
    expect(lastAssistant(chat.messagesOf(null)).status).toBe('streaming')

    stream.onEvent(token('第7段')) // 280s 再喂一次：末次停滞线 325s，落在总兜底之后
    await vi.advanceTimersByTimeAsync(20_000) // 越过 300s 总兜底
    await flushPromises()
    const assistant = lastAssistant(chat.messagesOf(null))
    expect(assistant.status).toBe('failed')
    expect(assistant.failure).toBe('回答超时（超过 5 分钟），可重试')
    expect(assistant.retryable).toBe(true)
    expect(stream.signal.aborted).toBe(true)
  })

  it('用户主动停止不受看门狗影响：仍标"已停止"且无失败文案（回归 T8 清单③）', async () => {
    const chat = useChatStream()
    void chat.send('问题', null)
    await flushPromises()
    chat.stop()
    await flushPromises()
    const assistant = lastAssistant(chat.messagesOf(null))
    expect(assistant.stopped).toBe(true)
    expect(assistant.retryable).toBe(true)
    expect(assistant.failure).toBeNull()
  })

  it('流正常结束却没收到 done：按"连接中断，请重试"失败收口，不留假流式态', async () => {
    const chat = useChatStream()
    void chat.send('问题', null)
    await flushPromises()
    openStreams[0]!.onEvent(token('半截回答'))
    openStreams[0]!.resolve() // 服务端漏发 done / 代理截断
    await flushPromises()
    const assistant = lastAssistant(chat.messagesOf(null))
    expect(assistant.status).toBe('failed')
    expect(assistant.failure).toBe('连接中断，请重试')
    expect(assistant.retryable).toBe(true)
  })
})
