/**
 * 知识库·文档接口（docs/api.md §2.5-§2.8）。
 *
 * 上传 / zip 导入 / 元数据建议走 multipart（文件本体请求），由 client.request 按
 * FormData 识别后不设 Content-Type（boundary 由浏览器生成）。响应体按 api.md 手写
 * （后端未声明 response_model）。
 *
 * metadata-suggest（F-1008，T4）与 import-zip（F-1007，T3）端点在车道 A 分支
 * （6a88cd8 / 36da916），尚未合入 feat/w4-fe：调用侧 silent 并对失败静默降级
 * （建议仅本地预填、导入入口置灰），合并后自动生效（deviations #21/#22）。
 */
import { get, post, request } from './client'
import type { PageData } from './types'

export type DocumentStatus = 'pending' | 'parsing' | 'ready' | 'failed'

/** 列表项（docs/api.md §2.6 全字段）。degraded 后端暂未下发：可选字段保留，下发即渲染降级标注（deviations #24） */
export interface DocumentItem {
  id: number
  title: string
  deptId: number
  deptCode: 'service' | 'hr' | 'all'
  originalFilename: string
  mime: string
  sizeBytes: number
  version: number
  effectiveDate: string
  expiryDate: string | null
  ownerName: string
  status: DocumentStatus
  parseEngine: string | null
  parseError: string | null
  isActive: boolean
  uploadedBy: number
  createdAt: string
  updatedAt: string
  degraded?: boolean
}

export interface DocumentListQuery {
  dept?: string
  status?: DocumentStatus | ''
  keyword?: string
  page: number
  pageSize: number
}

/** 文档列表（§22.1）：dept/status/keyword 可选，created_at 降序，分页 */
export function listDocuments(query: DocumentListQuery): Promise<PageData<DocumentItem>> {
  const params = new URLSearchParams()
  if (query.dept) params.set('dept', query.dept)
  if (query.status) params.set('status', query.status)
  const keyword = query.keyword?.trim() ?? ''
  if (keyword !== '') params.set('keyword', keyword)
  params.set('page', String(query.page))
  params.set('pageSize', String(query.pageSize))
  return get<PageData<DocumentItem>>(`/documents?${params.toString()}`)
}

export interface UploadDocumentPayload {
  title: string
  dept: string
  owner: string
  effectiveDate: string
  expiryDate: string | null
}

/**
 * 上传文档（F-1001~F-1005）：202 {id, status=parsing}。表单字段 camelCase 与
 * 后端 Form 参数同名（docs/api.md §2.5）；合规确认必为 true（后端 422/1003 卡口）。
 * silent：422/409 错误（格式不支持 / 查重命中）在上传弹窗内联展示（§22.2）。
 */
export function uploadDocument(
  file: File,
  payload: UploadDocumentPayload,
): Promise<{ id: number; status: DocumentStatus }> {
  const form = new FormData()
  form.append('file', file)
  form.append('title', payload.title)
  form.append('dept', payload.dept)
  form.append('owner', payload.owner)
  form.append('effectiveDate', payload.effectiveDate)
  if (payload.expiryDate !== null) form.append('expiryDate', payload.expiryDate)
  form.append('complianceConfirmed', 'true')
  return request<{ id: number; status: DocumentStatus }>('/documents', {
    method: 'POST',
    body: form,
    silent: true,
  })
}

/** 失败重试（DOC-11）：仅 failed 可重试（非 failed 422 由全局 toast 兜底） */
export function retryDocument(id: number): Promise<{ id: number; status: DocumentStatus }> {
  return post<{ id: number; status: DocumentStatus }>(`/documents/${id}/retry`)
}

/** 待确认激活（F-1002 黄色警示的确认动作）：isActive=false → true */
export function confirmDocument(id: number): Promise<{ id: number; isActive: boolean }> {
  return post<{ id: number; isActive: boolean }>(`/documents/${id}/confirm`)
}

// ---- 车道 A T3/T4 端点（尚未合入本分支，契约按合入提交逐字段对齐） ----

export type ZipEntryStatus = 'accepted' | 'duplicate' | 'rejected' | 'skipped_metadata'

/** 逐文件报告条目：documentId 仅 accepted/duplicate 有值，reason 仅非 accepted 有值 */
export interface ZipImportResultItem {
  filename: string
  status: ZipEntryStatus
  documentId?: number
  reason?: string
}

export interface ZipImportSummary {
  results: ZipImportResultItem[]
  accepted: number
  rejected: number
  skipped: number
}

/** zip 批量导入（F-1007）：202 逐文件报告 + 汇总；422（非 zip / 超限）弹窗内联故 silent */
export function importDocumentsZip(file: File): Promise<ZipImportSummary> {
  const form = new FormData()
  form.append('file', file)
  return request<ZipImportSummary>('/documents/import-zip', {
    method: 'POST',
    body: form,
    silent: true,
  })
}

/** 元数据建议（F-1008）：只解析文件名，命名不符返回 {}；失败（端点未合入 404）由调用方静默跳过 */
export function suggestDocumentMetadata(file: File): Promise<{
  suggestedDeptCode?: string
  suggestedTitle?: string
  suggestedEffectiveDate?: string
}> {
  const form = new FormData()
  form.append('file', file)
  return request<{
    suggestedDeptCode?: string
    suggestedTitle?: string
    suggestedEffectiveDate?: string
  }>('/documents/metadata-suggest', { method: 'POST', body: form, silent: true })
}
