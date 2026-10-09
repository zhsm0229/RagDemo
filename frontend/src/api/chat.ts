/**
 * 问答与会话接口（docs/api.md §3.1-§3.5）。
 *
 * SSE 消费（§3.6）：POST /api/chat/stream 须用 fetch + ReadableStream（无
 * EventSource 自动重连），不走 client.ts 统一信封封装（text/event-stream 无信封）。
 * 开流前错误（422/429/404/500）仍是普通 JSON 信封，解析后抛 ApiClientError；
 * 流中错误经 `error` 事件下发（5001 脱敏、retryable=true），由调用方按事件处理。
 */
import {
  ABORTED_CODE,
  ABORTED_MESSAGE,
  ApiClientError,
  BAD_RESPONSE_CODE,
  FALLBACK_MESSAGE,
  NETWORK_ERROR_CODE,
  NETWORK_ERROR_MESSAGE,
  UNAUTHORIZED_CODE,
  get,
  notifyUnauthorized,
  post,
  request,
} from './client'
import { parseSseFrames } from './sse'
import type {
  ApiResponse,
  ChatStreamEvent,
  ChatStreamRequest,
  CitationContent,
  CitationItem,
  FeedbackKind,
  FeedbackResult,
  PageData,
  RefType,
} from './types'

// ---- 会话 / 消息响应字段（docs/api.md §3.3 / §3.4） ----

export interface ConversationItem {
  id: number
  title: string
  updatedAt: string
}

/** 会话历史消息项；citations 为 JSONB 原样（user 消息恒 null） */
export interface MessageItem {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations: CitationItem[] | null
  feedback: 'up' | 'down' | null
  feedbackCategory: string | null
  feedbackComment: string | null
  answerType: string | null
  cacheHit: boolean
  /** 检索上游降级轮（retrieval-policy §2 迁移 0012）；旧数据缺省，读取侧 `?? false` 兜底 */
  degraded?: boolean
  createdAt: string
}

export function listConversations(page = 1, pageSize = 50): Promise<PageData<ConversationItem>> {
  return get<PageData<ConversationItem>>(`/conversations?page=${page}&pageSize=${pageSize}`)
}

/** 新建会话（title 走库默认"新会话"；问答首问命题见 api.md §3.2） */
export function createConversation(): Promise<{ id: number }> {
  return post<{ id: number }>('/conversations')
}

/** 删除会话（软删，幂等） */
export function deleteConversation(id: number): Promise<{ id: number; isActive: boolean }> {
  return request<{ id: number; isActive: boolean }>(`/conversations/${id}`, { method: 'DELETE' })
}

/** 会话历史消息（created_at 升序；中断遗留 assistant 占位行已被服务端过滤） */
export function getConversationMessages(conversationId: number): Promise<{ items: MessageItem[] }> {
  return get<{ items: MessageItem[] }>(`/conversations/${conversationId}/messages`)
}

// ---- 问答流（SSE） ----

export type ChatEventHandler = (event: ChatStreamEvent) => void

/** 开流前错误里由前端决定可否重试的码：参数/鉴权/限额/会话归属，重试无意义 */
const NON_RETRYABLE_PRESTREAM_CODES = new Set([1003, 2003, 2004, 3002, 3003])

/** 点踩分类枚举（docs/api.md §3.6，与后端 FeedbackRequest 同源） */
export const FEEDBACK_CATEGORIES = ['答案错误', '引用无关', '拒答不当', '其他'] as const

async function toEnvelopeError(res: Response): Promise<ApiClientError> {
  try {
    const payload = (await res.json()) as ApiResponse<unknown>
    if (payload !== null && typeof payload === 'object' && typeof payload.code === 'number') {
      return new ApiClientError(payload.code, payload.message || FALLBACK_MESSAGE, payload.data)
    }
  } catch {
    // 非 JSON 错误体（网关错误页等），按系统兜底码呈现
  }
  return new ApiClientError(BAD_RESPONSE_CODE, FALLBACK_MESSAGE)
}

/**
 * 消费问答流：逐事件回调（citations 恒先于 token，api.md §3.1）。
 * 网络层失败 / 开流前业务错误抛 ApiClientError（带 code），调用方据此标失败态。
 */
export async function streamChat(
  payload: ChatStreamRequest,
  onEvent: ChatEventHandler,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response
  try {
    res = await fetch('/api/chat/stream', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiClientError(ABORTED_CODE, ABORTED_MESSAGE)
    }
    throw new ApiClientError(NETWORK_ERROR_CODE, NETWORK_ERROR_MESSAGE)
  }

  const contentType = res.headers.get('content-type') ?? ''
  if (!res.ok || !contentType.includes('text/event-stream')) {
    const error = await toEnvelopeError(res)
    // 开流前会话失效（401/2003）走与 request 相同的全局通道（W4 终审 I ③）；
    // 流中 error 事件（5001 等）经 dispatch 下发，不经此路径、不触发登出
    if (res.status === 401 || error.code === UNAUTHORIZED_CODE) notifyUnauthorized()
    throw error
  }

  const reader = res.body?.getReader()
  if (!reader) throw new ApiClientError(BAD_RESPONSE_CODE, FALLBACK_MESSAGE)

  // 坏帧容错（T8 评审清单②）：代理注入的心跳/脏 data 非 JSON 时跳过该帧，不中止整条流
  const dispatch = (frame: { event: string; data: string }): void => {
    try {
      onEvent({ ...JSON.parse(frame.data), event: frame.event } as ChatStreamEvent)
    } catch {
      // 非 JSON data 帧丢弃
    }
  }

  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (value !== undefined) buffer += decoder.decode(value, { stream: true })
      const { frames, rest } = parseSseFrames(buffer)
      buffer = rest
      frames.forEach(dispatch)
      if (done) break
    }
  } catch (error) {
    // 中途断流（T8 评审清单①）：read 抛错（断网/代理重置）映射网络错误码呈现，
    // 非 NON_RETRYABLE 集合成员 → 气泡失败态可重试；用户 abort 单独成码
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiClientError(ABORTED_CODE, ABORTED_MESSAGE)
    }
    throw new ApiClientError(NETWORK_ERROR_CODE, NETWORK_ERROR_MESSAGE)
  }
  // 收尾残尾：服务端若未以空行收尾也尽力解析最后一个事件
  buffer += decoder.decode()
  if (buffer.trim() !== '') parseSseFrames(`${buffer}\n\n`).frames.forEach(dispatch)
}

export { NON_RETRYABLE_PRESTREAM_CODES }

// ---- 引用原文核对 / 消息反馈 ----

/** 引用正文（doc → chunk 全文 / qa → 答案文本）；404/3003 由弹窗内联展示故 silent */
export function getCitationContent(refType: RefType, refId: number): Promise<CitationContent> {
  return get<CitationContent>(`/citations/content?refType=${refType}&refId=${refId}`, {
    silent: true,
  })
}

export interface FeedbackPayload {
  feedback: FeedbackKind
  /** down 必填枚举；up 时后端忽略置 null */
  category?: string
  /** down 必填 strip 后 1-500 字；up 时后端忽略置 null */
  comment?: string
}

/**
 * 消息反馈（F-208）。down 弹窗内联展示 422/1003 兜底错误需传 silent=true；
 * up 轻交互走默认 ElMessage 兜底。
 */
export function leaveFeedback(
  messageId: number,
  payload: FeedbackPayload,
  silent = false,
): Promise<FeedbackResult> {
  return post<FeedbackResult>(`/messages/${messageId}/feedback`, payload, { silent })
}
