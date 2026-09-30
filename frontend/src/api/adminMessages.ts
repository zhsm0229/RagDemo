/**
 * 管理端·提问记录接口（F-701，docs/api.md §4.3.1/§4.3.2）。
 * 恒滤 is_eval（后端 W3 约束：评测数据不污染运营查询）；以 user 消息行为单位，
 * 回答三字段（answerType/hasCitations/feedback）取自配对回答，无回答/中断记 null/false。
 */
import { get } from './client'
import type { CitationItem, FeedbackKind, PageData } from './types'

/** 列表项（api.md §4.3.1 全 9 键）：questionPreview 已由后端截 50 字 */
export interface AdminMessageItem {
  id: number
  conversationId: number
  createdAt: string
  username: string
  deptCode: string
  /** 截 50 字的问题摘要（完整提问原文在详情 user.content） */
  questionPreview: string
  /** 配对回答类型（cache/qa/rag/refuse）；无回答/中断为 null */
  answerType: string | null
  hasCitations: boolean
  /** 配对回答的反馈；无回答/无反馈为 null */
  feedback: FeedbackKind | null
}

export interface AdminMessageListQuery {
  deptId?: number
  /** YYYY-MM-DD，含首尾日（后端按 UTC 整日边界） */
  dateFrom?: string
  dateTo?: string
  /** true=回答被点踩；false=补集（含点赞/无反馈/无回答） */
  hasFeedback?: boolean
  page: number
  pageSize: number
}

/** 提问记录列表（F-701）：空参不发可选条件，created_at 降序分页 */
export function listAdminMessages(
  query: AdminMessageListQuery,
): Promise<PageData<AdminMessageItem>> {
  const params = new URLSearchParams()
  if (query.deptId) params.set('deptId', String(query.deptId))
  if (query.dateFrom) params.set('dateFrom', query.dateFrom)
  if (query.dateTo) params.set('dateTo', query.dateTo)
  if (typeof query.hasFeedback === 'boolean') {
    params.set('hasFeedback', String(query.hasFeedback))
  }
  params.set('page', String(query.page))
  params.set('pageSize', String(query.pageSize))
  return get<PageData<AdminMessageItem>>(`/admin/messages?${params.toString()}`)
}

/** 详情 user 段：提问者身份 + 完整问题原文（区别于列表 50 字摘要） */
export interface MessageDetailUser {
  id: number
  conversationId: number
  createdAt: string
  username: string
  deptCode: string
  content: string
}

/** 详情 assistant 段（api.md §4.3.2）：citations 为 JSONB 原样（不重组不脱敏） */
export interface MessageDetailAssistant {
  id: number
  content: string
  /** CitationItem[] 形态原样透传（含 id——badcase 处理按 assistant 消息 id 寻址） */
  citations: CitationItem[] | null
  answerType: string | null
  model: string | null
  promptTokens: number
  completionTokens: number
  latencyMs: number | null
  cacheHit: boolean
  feedback: FeedbackKind | null
  feedbackCategory: string | null
  feedbackComment: string | null
}

export interface MessageDetail {
  user: MessageDetailUser
  /** null = 该问无有效回答（生成中断——占位行被软删），抽屉渲染失败态占位 */
  assistant: MessageDetailAssistant | null
}

/** 提问详情：非 user 行 / 评测消息 / 不存在 id → 404/3003 同文案（不给探测空间） */
export function getAdminMessageDetail(id: number): Promise<MessageDetail> {
  return get<MessageDetail>(`/admin/messages/${id}`, { silent: true })
}
