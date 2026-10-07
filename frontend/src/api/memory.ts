/**
 * 我的记忆接口（F-210 管理面；docs/specs/agent-ui-design.md §2）。
 *
 * 记忆是个人数据：全员可管自己的记忆（后端 ReadyUser 门 + 服务层归属裁决）。
 * 来源由后端按 sourceMessageId 推导（auto=系统在问答中提炼 / manual=手动添加）；
 * 响应体按契约手写（后端未声明 response_model，schema 生成物为 unknown）。
 */
import { del, get, post, request } from './client'
import type { PageData } from './types'

/** 来源口径：manual=你手动添加（sourceMessageId 为 null）/ auto=系统在问答中提炼 */
export type MemorySource = 'manual' | 'auto'

/** 列表项（后端按 updated_at 降序分页；DTO 无 updatedAt，时间列只呈现 createdAt） */
export interface MemoryItem {
  id: number
  content: string
  source: MemorySource
  sourceMessageId: number | null
  createdAt: string
}

export interface MemoryListQuery {
  keyword?: string
  page: number
  pageSize: number
}

/** 我的记忆列表：keyword 对内容过滤（通配符后端已转义，按字面匹配），updated_at 降序 */
export function listMemories(query: MemoryListQuery): Promise<PageData<MemoryItem>> {
  const params = new URLSearchParams()
  const keyword = query.keyword?.trim() ?? ''
  if (keyword !== '') params.set('keyword', keyword)
  params.set('page', String(query.page))
  params.set('pageSize', String(query.pageSize))
  return get<PageData<MemoryItem>>(`/memories?${params.toString()}`)
}

/** 手动添加（1-200 字，strip 口径与后端校验一致）：201 返回新条目 */
export function addMemory(content: string): Promise<MemoryItem> {
  return post<MemoryItem>('/memories', { content })
}

/** 删除（软删）：回答不再参考该条，行保留可追溯 */
export function deleteMemory(id: number): Promise<{ id: number; isActive: boolean }> {
  return del<{ id: number; isActive: boolean }>(`/memories/${id}`)
}

/** 记忆功能开关（用户级）：false=冻结（不注入不提炼），已有记忆保留；返回落库值 */
export function updateMemorySettings(enabled: boolean): Promise<{ memoryEnabled: boolean }> {
  return request<{ memoryEnabled: boolean }>('/memories/settings', {
    method: 'PUT',
    body: { enabled },
  })
}
