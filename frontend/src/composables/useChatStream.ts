/**
 * 问答消息流状态机（P2 对话区数据源，standards §3.6 / CHAT-12）。
 *
 * - 消息按会话分桶存储（messagesByConv），流式中切换会话不丢增量；
 *   未落库会话（首问 conversationId=null）的消息暂存 UNASSIGNED 桶，
 *   done 后由页面把桶迁移到后端返回的真实会话 id 上。
 * - 事件应用顺序即后端时序：citations（先到，渲染引用占位）→ token（增量拼接）
 *   → done（refused/cacheHit/用量收尾）→ error（标失败态，retryable 才出重试）；
 *   未知事件名忽略。
 * - 停止（T8 评审清单③）：streamChat 传 AbortController.signal，stop() 中止后
 *   气泡标"已停止"且可重问。
 * - 重试语义（CHAT-12）：以相同问题新发一条（新 user 气泡 + 新流），
 *   失败消息保留标失败态，后端不重复落库。
 */
import { reactive, ref } from 'vue'
import { ABORTED_CODE, ApiClientError } from '@/api/client'
import { NON_RETRYABLE_PRESTREAM_CODES, streamChat, type MessageItem } from '@/api/chat'
import type { ChatStreamEvent, CitationItem, FeedbackKind } from '@/api/types'

export type MessageStatus = 'streaming' | 'done' | 'failed'

/**
 * 等待阶段提示（2026-10-06 用户体验缺陷修复：发送后到首 token 之间气泡全白无交互）：
 * search=检索知识库中（涵盖改写/召回/重排——prepare_turn 在后端开流前执行，此窗口
 * 只有 citations 事件可为界）；generate=引用已就绪等待生成。首个 token 到达即置 null，
 * 内容开始流式呈现后提示退场。
 */
export type StreamStage = 'search' | 'generate'

export interface ChatMessage {
  /** 本地渲染 key（流式占位在后端回 id 前为 null） */
  key: number
  id: number | null
  role: 'user' | 'assistant'
  content: string
  citations: CitationItem[]
  status: MessageStatus
  /** 等待阶段提示（仅流式占位期非空；历史/终态消息恒 null） */
  stage: StreamStage | null
  /** 占位创建时刻（等待提示的"已等待 N 秒"计时起点） */
  startedAt: number | null
  refused: boolean
  cacheHit: boolean
  /** done 事件下发的用量（对照原型 msg-meta 的 token·耗时；历史接口无此字段恒 null） */
  promptTokens: number | null
  completionTokens: number | null
  durationMs: number | null
  /** 反馈状态（F-208；历史加载与反馈提交回填，取消反馈后端无端点故只进不退） */
  feedback: FeedbackKind | null
  feedbackCategory: string | null
  /** 用户主动停止生成（气泡标"已停止"且可重问，T8 评审清单③） */
  stopped: boolean
  /** failed 态的脱敏失败原因（气泡内联展示） */
  failure: string | null
  retryable: boolean
}

/** 未落库会话的消息桶 key（首问 conversationId=null 时暂存） */
export const UNASSIGNED_CONV = 0

export interface ChatStreamHooks {
  /** 每个流事件应用后回调（页面用于滚动跟随） */
  onUpdate?: () => void
  /** 单轮流结束（done/error/异常）后回调，参数为该轮使用的会话 id（null=首问自建） */
  onSettled?: (conversationId: number | null) => void
}

function nextKey(): () => number {
  let seq = 0
  return () => ++seq
}

function userMessage(question: string, keyOf: () => number): ChatMessage {
  return {
    key: keyOf(),
    id: null,
    role: 'user',
    content: question,
    citations: [],
    status: 'done',
    stage: null,
    startedAt: null,
    refused: false,
    cacheHit: false,
    promptTokens: null,
    completionTokens: null,
    durationMs: null,
    feedback: null,
    feedbackCategory: null,
    stopped: false,
    failure: null,
    retryable: false,
  }
}

function assistantPlaceholder(keyOf: () => number): ChatMessage {
  return {
    key: keyOf(),
    id: null,
    role: 'assistant',
    content: '',
    citations: [],
    status: 'streaming',
    stage: 'search', // 发问即示"正在检索知识库"（改写/召回/重排窗口），citations 到达转"生成中"
    startedAt: Date.now(),
    refused: false,
    cacheHit: false,
    promptTokens: null,
    completionTokens: null,
    durationMs: null,
    feedback: null,
    feedbackCategory: null,
    stopped: false,
    failure: null,
    retryable: false,
  }
}

/** 历史消息还原（api.md §3.4）；answerType='refuse' 即拒答（后端 REFUSAL_TEXT 逐字在 content） */
export function mapHistory(items: MessageItem[], keyOf: () => number): ChatMessage[] {
  const out: ChatMessage[] = []
  for (const item of items) {
    if (item.role === 'user') {
      out.push(userMessage(item.content, keyOf))
      continue
    }
    out.push({
      key: keyOf(),
      id: item.id,
      role: 'assistant',
      content: item.content,
      citations: item.citations ?? [],
      status: 'done',
      stage: null,
      startedAt: null,
      refused: item.answerType === 'refuse',
      cacheHit: item.cacheHit,
      promptTokens: null,
      completionTokens: null,
      durationMs: null,
      feedback: item.feedback,
      feedbackCategory: item.feedbackCategory,
      stopped: false,
      failure: null,
      retryable: false,
    })
  }
  // 中断行被服务端过滤后，user 消息可能没有配对回答 → 补"生成失败可重试"占位（CHAT-12）
  const withPlaceholders: ChatMessage[] = []
  for (let i = 0; i < out.length; i++) {
    withPlaceholders.push(out[i])
    const next = out[i + 1]
    if (out[i].role === 'user' && (next === undefined || next.role !== 'assistant')) {
      withPlaceholders.push({
        key: keyOf(),
        id: null,
        role: 'assistant',
        content: '',
        citations: [],
        status: 'failed',
        stage: null,
        startedAt: null,
        refused: false,
        cacheHit: false,
        promptTokens: null,
        completionTokens: null,
        durationMs: null,
        feedback: null,
        feedbackCategory: null,
        stopped: false,
        failure: '生成失败，可重试',
        retryable: true,
      })
    }
  }
  return withPlaceholders
}

function applyEvent(message: ChatMessage, event: ChatStreamEvent): void {
  switch (event.event) {
    case 'citations':
      message.citations = event.items
      message.stage = 'generate' // 引用已就绪：等待提示切"正在生成回答"
      break
    case 'token':
      message.content += event.content
      message.stage = null // 内容开始流式呈现，提示退场（光标接管）
      break
    case 'done':
      message.status = 'done'
      message.stage = null
      message.id = event.messageId
      message.refused = event.refused
      message.cacheHit = event.cacheHit
      message.promptTokens = event.promptTokens
      message.completionTokens = event.completionTokens
      message.durationMs = event.durationMs
      break
    case 'error':
      message.status = 'failed'
      message.stage = null
      message.failure = event.message
      message.retryable = event.retryable
      break
    default:
      // 未知事件名（心跳/未来扩展）忽略，不判 error（T8 评审清单②）
      break
  }
}

/** 开流前错误的失败文案与可否重试：业务性拒绝（参数/鉴权/限额/会话归属）重试无意义，其余（网络/系统）可重试 */
function fromPreStreamError(error: unknown): { failure: string; retryable: boolean } {
  if (error instanceof ApiClientError) {
    return { failure: error.message, retryable: !NON_RETRYABLE_PRESTREAM_CODES.has(error.code) }
  }
  return { failure: '系统繁忙，请稍后重试', retryable: false }
}

export function useChatStream(hooks: ChatStreamHooks = {}) {
  const streaming = ref(false)
  const messagesByConv = reactive<Record<number, ChatMessage[]>>({})
  const keyOf = nextKey()

  function bucket(conversationId: number | null): ChatMessage[] {
    const key = conversationId ?? UNASSIGNED_CONV
    if (messagesByConv[key] === undefined) messagesByConv[key] = []
    return messagesByConv[key]
  }

  function messagesOf(conversationId: number | null): ChatMessage[] {
    return messagesByConv[conversationId ?? UNASSIGNED_CONV] ?? []
  }

  function hasMessages(conversationId: number): boolean {
    return messagesByConv[conversationId] !== undefined
  }

  /** 首问自建会话后，把未落库桶迁移到真实会话 id（api.md：标题取首问前 20 字，done 后刷新列表可知 id） */
  function adoptCreatedConversation(conversationId: number): void {
    if (messagesByConv[UNASSIGNED_CONV] !== undefined) {
      messagesByConv[conversationId] = messagesByConv[UNASSIGNED_CONV]
      delete messagesByConv[UNASSIGNED_CONV]
    }
  }

  function dropConversation(conversationId: number): void {
    delete messagesByConv[conversationId]
  }

  function loadHistory(conversationId: number, items: MessageItem[]): void {
    messagesByConv[conversationId] = mapHistory(items, keyOf)
  }

  /** 当前流的取消器（一机一流，全局串行） */
  let controller: AbortController | null = null

  /** 用户停止当前生成：气泡标"已停止"并可重问（T8 评审清单③；非流式态空操作） */
  function stop(): void {
    controller?.abort()
  }

  /** 发问：追加 user 气泡 + 流式占位，逐事件增量应用（一机一流，全局串行） */
  async function send(question: string, conversationId: number | null): Promise<void> {
    if (streaming.value) return
    streaming.value = true
    controller = new AbortController()
    const list = bucket(conversationId)
    list.push(userMessage(question, keyOf), assistantPlaceholder(keyOf))
    // 事件须作用在 reactive 代理上：直接改原始对象不触发依赖更新（模板经代理读取）
    const placeholder = list[list.length - 1]
    try {
      await streamChat(
        { conversationId, question },
        (event) => {
          applyEvent(placeholder, event)
          hooks.onUpdate?.()
        },
        controller.signal,
      )
    } catch (error) {
      placeholder.status = 'failed'
      placeholder.stage = null
      if (error instanceof ApiClientError && error.code === ABORTED_CODE) {
        // 用户停止：不打失败原因文案，可重问（重试=同问题新发一条）
        placeholder.stopped = true
        placeholder.retryable = true
      } else {
        const { failure, retryable } = fromPreStreamError(error)
        placeholder.failure = failure
        placeholder.retryable = retryable
      }
    } finally {
      controller = null
      streaming.value = false
      hooks.onSettled?.(conversationId)
    }
  }

  /** 重试（CHAT-12）：取失败消息前最近一条 user 提问，以相同问题新发一条 */
  async function retryFailed(failed: ChatMessage, conversationId: number | null): Promise<void> {
    if (streaming.value) return
    const list = bucket(conversationId)
    const index = list.indexOf(failed)
    if (index < 0) return
    for (let i = index - 1; i >= 0; i--) {
      if (list[i].role === 'user') {
        await send(list[i].content, conversationId)
        return
      }
    }
  }

  return {
    streaming,
    messagesOf,
    hasMessages,
    adoptCreatedConversation,
    dropConversation,
    loadHistory,
    send,
    stop,
    retryFailed,
  }
}
