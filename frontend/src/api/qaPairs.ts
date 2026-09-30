/**
 * 知识库·问答对接口（docs/api.md §4.1，W3 管理端点）。
 *
 * 响应体按 api.md 手写（后端未声明 response_model，schema 生成物为 unknown）；
 * 请求体类型取 schema.d.ts（ToggleStatusBody / EditQaPairBody，唯一事实源）。
 * 模板下载为 xlsx 字节流，不走统一信封封装（同 SSE / 文件下载口径，client.ts 头注）。
 */
import { get, request } from './client'
import type { components } from './schema'
import type { PageData } from './types'

/** enabled=启用（参与检索）/ disabled=停用（不参与），与后端枚举同源 */
export type QaPairStatus = 'enabled' | 'disabled'

/** 编辑请求体（schema EditQaPairBody：全文提交，校验同导入规则 问题 1-2000 / 答案 1-10000） */
export type EditQaPairPayload = components['schemas']['EditQaPairBody']

/** 列表项（docs/api.md §4.1.3 全 9 键；无 updatedAt，见 components/qa/options.ts 注） */
export interface QaPairItem {
  id: number
  deptId: number
  deptCode: string
  question: string
  answer: string
  status: QaPairStatus
  batchNo: string
  createdAt: string
  isActive: boolean
}

export interface QaPairListQuery {
  deptId?: number
  status?: QaPairStatus | ''
  keyword?: string
  page: number
  pageSize: number
}

/** 管理列表（§22.1）：deptId/status/keyword 可选（空参不发），created_at 降序，分页 */
export function listQaPairs(query: QaPairListQuery): Promise<PageData<QaPairItem>> {
  const params = new URLSearchParams()
  if (query.deptId) params.set('deptId', String(query.deptId))
  if (query.status) params.set('status', query.status)
  const keyword = query.keyword?.trim() ?? ''
  if (keyword !== '') params.set('keyword', keyword)
  params.set('page', String(query.page))
  params.set('pageSize', String(query.pageSize))
  return get<PageData<QaPairItem>>(`/admin/qa-pairs?${params.toString()}`)
}

export interface QaPairImportResult {
  imported: number
  batchNo: string
}

/** 错误行（422/1003 的 data 全量数组）：row 为 Excel 物理行号（表头=1，数据从 2 起） */
export interface QaPairImportErrorRow {
  row: number
  field: string
  reason: string
}

/**
 * Excel 整批导入（F-401）：全合格 201 {imported, batchNo}；任一行不合格 422/1003
 * 整批退回零入库，data 为错误行全量数组——silent 由弹窗内联展示（§22.2）。
 * deptCode 固定 'all'（后端缺省同值）：原型导入向导无部门选择控件（deviations #31）。
 */
export function importQaPairs(file: File): Promise<QaPairImportResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('deptCode', 'all')
  return request<QaPairImportResult>('/admin/qa-pairs/import', {
    method: 'POST',
    body: form,
    silent: true,
  })
}

/** 模板下载直链（F-403，xlsx 附件）；同源直链 httpOnly Cookie 随请求携带 */
export const QA_PAIRS_TEMPLATE_URL = '/api/admin/qa-pairs/template'
/** Content-Disposition 文件名（api.md §4.1.2） */
export const QA_PAIRS_TEMPLATE_FILENAME = 'qa_pairs_template.xlsx'

/** 停用 / 启用（QA-05）：返回落库后的状态 */
export function toggleQaPairStatus(
  id: number,
  status: QaPairStatus,
): Promise<{ id: number; status: QaPairStatus }> {
  return request<{ id: number; status: QaPairStatus }>(`/admin/qa-pairs/${id}/status`, {
    method: 'PATCH',
    body: { status },
  })
}

/** 编辑（QA-06）：问题单条重嵌、新答案立即生效，返回更新后全文 */
export function updateQaPair(
  id: number,
  payload: EditQaPairPayload,
): Promise<{ id: number; question: string; answer: string }> {
  return request<{ id: number; question: string; answer: string }>(`/admin/qa-pairs/${id}`, {
    method: 'PUT',
    body: payload,
  })
}

/** 删除（软删 QA-07）：置 isActive=false 退出检索与列表 */
export function deleteQaPair(id: number): Promise<{ id: number; isActive: boolean }> {
  return request<{ id: number; isActive: boolean }>(`/admin/qa-pairs/${id}`, {
    method: 'DELETE',
  })
}
