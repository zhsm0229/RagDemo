/**
 * 管理端·badcase 队列接口（F-702，docs/api.md §4.3.3/§4.3.4）。
 * 队列口径：feedback='down' 且非评测的 assistant 消息，一条回答一条记录不重复。
 * badcaseStatus 缺省不过滤——"待处理"默认视图由前端显式传参（W3 交接）。
 */
import { get, request } from './client'
import type { components } from './schema'
import type { PageData } from './types'

/** admin 终态枚举（schema BadcasePatchRequest 唯一事实源）：'待处理' 仅由 W2 点踩入口产生，无重开回退 */
export type BadcaseStatus = components['schemas']['BadcasePatchRequest']['badcaseStatus']

/** 队列项（api.md §4.3.3 全 7 键）：questionPreview 已由后端截 50 字 */
export interface BadcaseItem {
  messageId: number
  createdAt: string
  deptCode: string
  questionPreview: string
  /** 用户点踩时选择的分类/备注（W2 反馈入口落列） */
  category: string | null
  comment: string | null
  badcaseStatus: string | null
}

export interface BadcaseListQuery {
  deptId?: number
  badcaseStatus?: string
  page: number
  pageSize: number
}

/** badcase 队列（F-702）：badcaseStatus 空串不发（不过滤），created_at 降序分页 */
export function listBadcases(query: BadcaseListQuery): Promise<PageData<BadcaseItem>> {
  const params = new URLSearchParams()
  if (query.deptId) params.set('deptId', String(query.deptId))
  if (query.badcaseStatus) params.set('badcaseStatus', query.badcaseStatus)
  params.set('page', String(query.page))
  params.set('pageSize', String(query.pageSize))
  return get<PageData<BadcaseItem>>(`/admin/badcases?${params.toString()}`)
}

/** 处置请求体（schema BadcasePatchRequest）：note 只入审计不入 messages（ADM-03） */
export type BadcasePatchPayload = components['schemas']['BadcasePatchRequest']

/** 处置（F-702）：迁移至 admin 终态；'已忽略' 必填理由（前端先行校验 + 后端 422 兜底内联） */
export function patchBadcase(
  messageId: number,
  payload: BadcasePatchPayload,
): Promise<{ messageId: number; badcaseStatus: BadcaseStatus }> {
  return request<{ messageId: number; badcaseStatus: BadcaseStatus }>(
    `/admin/badcases/${messageId}`,
    { method: 'PATCH', body: payload, silent: true },
  )
}
