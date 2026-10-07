/**
 * 知识库·文档接口（docs/api.md §2.5-§2.8）。
 *
 * 上传 / zip 导入 / 元数据建议走 multipart（文件本体请求），由 client.request 按
 * FormData 识别后不设 Content-Type（boundary 由浏览器生成）。响应体按 api.md 手写
 * （后端未声明 response_model）。
 *
 * metadata-suggest（F-1008，T4）与 import-zip（F-1007，T3）端点已上线（历史注记：
 * 曾在车道 A 分支先行，2026-10-06 对账时合入口径以 docs/api.md 为准），
 * 调用侧 silent 并对失败静默降级（建议仅本地预填、导入入口置灰）。
 */
import { ApiClientError, del, get, post, request } from './client'
import type { PageData } from './types'

export type DocumentStatus = 'pending' | 'parsing' | 'ready' | 'failed'
/** 来源（agent-evolution #05）：upload=人工上传语料，draft=Agent 生成草稿 */
export type DocumentSourceType = 'upload' | 'draft'
/** 草稿审核状态（与后端 0007 迁移 CHECK 同源）；仅 sourceType='draft' 非空 */
export type ReviewStatus = '待审核' | '已通过' | '已驳回'

/** 列表项（docs/api.md §2.6 全字段）。degraded：ready 且带降级标注（MinerU 不可用走 pypdf） */
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
  /** 技术错误摘要（管理端定位用），与 parseError 双轨；仅 failed 可能非空 */
  parseErrorDetail: string | null
  /** 解析当前阶段文案（排队中/解析中/分块中/向量化中），仅 parsing 期非空 */
  parseStage: string | null
  /** 真实进度 0-100（向量化按嵌入批次推进），仅 parsing 期非空；null 时 UI 回退不确定动画 */
  parseProgress: number | null
  /** 来源：upload/draft；草稿未经审核不进检索（agent-evolution §五 无审核不落地） */
  sourceType: DocumentSourceType
  /** 草稿审核状态，仅 sourceType='draft' 非空 */
  reviewStatus: ReviewStatus | null
  /** 驳回原因，仅 reviewStatus='已驳回' 非空 */
  rejectReason: string | null
  isActive: boolean
  uploadedBy: number
  createdAt: string
  updatedAt: string
  degraded?: boolean
}

/** F-1017/F-1018：解析图片回显与原文件下载 URL（Cookie 同源鉴权，后端部门隔离闸） */
export function docImageUrl(docId: number, imageIndex: number): string {
  return `/api/documents/${docId}/images/${imageIndex}`
}

export function docFileUrl(docId: number): string {
  return `/api/documents/${docId}/file`
}

/** 在线预览目标（DocumentItem 的结构子集；引用侧无 filename/mime 时留空走端点判定） */
export interface PreviewTarget {
  id: number
  title: string
  originalFilename: string
  mime: string
}

/** 在线预览 URL（统一 PDF inline）：pdf 直通/office 服务端转 PDF 缓存，iframe 内嵌渲染 */
export function docPreviewUrl(docId: number): string {
  return `/api/documents/${docId}/preview`
}

/**
 * 取原文件字节（文本类预览渲染用）：fetch 字节流 → Blob，独立 fetch 不走 client.request
 * 信封解包（模式同 exportAuditLogs）；失败抛 ApiClientError（信封中文提示或系统兜底）。
 */
export async function fetchDocumentFile(docId: number): Promise<Blob> {
  const res = await fetch(`/api/documents/${docId}/file`, { credentials: 'include' })
  if (!res.ok) {
    let code = 5000
    let message = '原文件获取失败，请稍后重试'
    try {
      const payload = (await res.json()) as { code?: unknown; message?: unknown }
      if (typeof payload.code === 'number') code = payload.code
      if (typeof payload.message === 'string' && payload.message !== '') message = payload.message
    } catch {
      // 非 JSON 错误体（网关错误页等），走系统兜底文案
    }
    throw new ApiClientError(code, message)
  }
  return res.blob()
}

export interface DocumentListQuery {
  dept?: string
  status?: DocumentStatus | ''
  keyword?: string
  /** 来源筛选：草稿审核入口取 'draft'；不传=不过滤（全部来源可见） */
  sourceType?: DocumentSourceType | ''
  /** 审核状态筛选：与 sourceType=draft 组合取待审核清单 */
  reviewStatus?: ReviewStatus | ''
  page: number
  pageSize: number
}

/** 文档列表（§22.1）：dept/status/keyword/sourceType/reviewStatus 可选，created_at 降序，分页 */
export function listDocuments(query: DocumentListQuery): Promise<PageData<DocumentItem>> {
  const params = new URLSearchParams()
  if (query.dept) params.set('dept', query.dept)
  if (query.status) params.set('status', query.status)
  if (query.sourceType) params.set('sourceType', query.sourceType)
  if (query.reviewStatus) params.set('reviewStatus', query.reviewStatus)
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

/** 文档删除（F-305，2026-10-06）：软删+级联停用 chunks；仅 admin（后端 AdminUser 闸） */
export function deleteDocument(id: number): Promise<{ id: number; deleted: boolean }> {
  return del<{ id: number; deleted: boolean }>(`/documents/${id}`)
}

// ---- 草稿审核（agent-evolution #05）：仅 admin；通过后走既有解析入库管道 ----

/** 草稿审核通过（无审核不落地红线的放行闸）：已通过后后端立即触发解析入库 */
export function approveDraft(
  id: number,
): Promise<{ id: number; reviewStatus: ReviewStatus; status: DocumentStatus }> {
  return post<{ id: number; reviewStatus: ReviewStatus; status: DocumentStatus }>(
    `/documents/${id}/draft/approve`,
  )
}

/** 草稿驳回：原因必填留档（后端 422 卡空原因），驳回后永不入库 */
export function rejectDraft(
  id: number,
  reason: string,
): Promise<{ id: number; reviewStatus: ReviewStatus; rejectReason: string }> {
  return post<{ id: number; reviewStatus: ReviewStatus; rejectReason: string }>(
    `/documents/${id}/draft/reject`,
    { reason },
  )
}

// ---- 车道 A T3/T4 端点（已上线，契约见 docs/api.md §2.8a-c） ----

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
