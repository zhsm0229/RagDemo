/**
 * 后端 OpenAPI 生成类型包装 + 手写业务类型。
 *
 * 分工约定（task-6 brief）：
 * - 请求体：直接取 `src/api/schema.d.ts`（openapi-typescript 自 /openapi.json 生成，唯一事实源）；
 * - 响应体：后端未声明 response_model（生成物里是 unknown），按 docs/api.md 手写；
 * - SSE（POST /api/chat/stream）与文件下载不走 OpenAPI DTO，四事件手写（§3.6 逐字口径）。
 */
import type { components } from './schema'

// ---- 请求体：OpenAPI 生成物 ----
export type LoginRequest = components['schemas']['LoginRequest']
export type ChangePasswordRequest = components['schemas']['ChangePasswordRequest']
export type ChatStreamRequest = components['schemas']['ChatStreamRequest']

// ---- 统一响应体（docs/api.md §1.2） ----
export interface ApiResponse<T> {
  code: number
  message: string
  data: T
}

/** 列表响应统一信封（standards §3-5） */
export interface PageData<T> {
  items: T[]
  total: number
  page: number
  pageSize: number
}

// ---- 登录态 ----
export type UserRole = 'admin' | 'member'

/** 登录 / /auth/me / 改密响应的用户信息（app/api/auth.py `_user_payload`） */
export interface UserInfo {
  userId: number
  username: string
  role: UserRole
  mustChangePassword: boolean
  /** 用户级记忆开关（F-210）：MemoryView 开关初值来源（app/api/memories.py PUT settings 可改） */
  memoryEnabled: boolean
}

// ---- 问答流 SSE 事件（standards §3 第 6 条，docs/api.md §3.1） ----
export type RefType = 'doc' | 'qa'

/**
 * 引用项：refId 语义随 refType——qa → 问答对 id；doc → chunk id（仅取切片正文用）。
 * docId 仅 doc 引用有值：文档主键，取原文件/图片/在线预览按文档寻址（≠refId）。
 */
export interface CitationItem {
  n: number
  refType: RefType
  refId: number
  docId?: number
  title: string
  page: number | null
  titlePath: string | null
  score: number
}

/** citations 恒先于 token 事件下发 */
export interface ChatCitationsEvent {
  event: 'citations'
  messageId: number
  items: CitationItem[]
}

export interface ChatTokenEvent {
  event: 'token'
  messageId: number
  content: string
}

export interface ChatDoneEvent {
  event: 'done'
  messageId: number
  refused: boolean
  cacheHit: boolean
  promptTokens: number
  completionTokens: number
  durationMs: number
}

/** 仅经 SSE 下发的流中错误；retryable 仅 code=5001 时为 true */
export interface ChatErrorEvent {
  event: 'error'
  code: number
  message: string
  retryable: boolean
}

export type ChatStreamEvent = ChatCitationsEvent | ChatTokenEvent | ChatDoneEvent | ChatErrorEvent

// ---- 引用原文核对（docs/api.md §3.7 契约，GET /api/citations/content） ----

/** refType=doc → chunk 正文；refType=qa → 问答对答案文本 */
export interface CitationContent {
  refType: RefType
  refId: number
  content: string
  title: string
  page: number | null
  titlePath: string | null
}

// ---- 消息反馈（docs/api.md §3.6，POST /api/messages/{id}/feedback） ----

export type FeedbackKind = 'up' | 'down'

export interface FeedbackResult {
  id: number
  feedback: FeedbackKind
  feedbackCategory: string | null
  feedbackComment: string | null
  badcaseStatus: string | null
}
