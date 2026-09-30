/**
 * 审计日志接口（F-802，列表 + CSV 导出）。端点在主分支 68783f9，未合入
 * feat/w4-fe：按 T2 评审移交契约实现，合并即生效（deviations 同 #22 模式）。
 *
 * detail 为 JSON 字符串（T2 评审移交契约）：消费方解析后格式化展示，解析失败
 * 原样字符串。导出为 CSV 字节流（UTF-8 BOM + 中文列头），不走统一信封封装
 * （同文件下载口径，client.ts 头注），行数超导出上限截断并以 X-Truncated: true 标注。
 */
import { get, ApiClientError, BAD_RESPONSE_CODE, FALLBACK_MESSAGE } from './client'
import type { PageData } from './types'

export interface AuditLogItem {
  id: number
  actorUsername: string
  action: string
  objectType: string
  objectId: number | null
  /** JSON 字符串原样透传（后端 JSONB → json.dumps）；前端经 formatAuditDetail 消费 */
  detail: unknown
  ip: string | null
  createdAt: string
}

export interface AuditLogQuery {
  /** 操作人用户 id（后端精确匹配 actor_user_id） */
  actorId?: number
  /** 动作名（如 user.create，后端精确匹配） */
  action?: string
  /** 对象类型（如 qa_pair / eval_run，后端精确匹配） */
  objectType?: string
  /** YYYY-MM-DD，含首尾日（后端按 UTC 整日边界） */
  dateFrom?: string
  dateTo?: string
  page: number
  pageSize: number
}

/** 列表与导出共用的筛选拼参：空参不发（created_at DESC 服务端固定排序） */
function buildFilterParams(query: Omit<AuditLogQuery, 'page' | 'pageSize'>): URLSearchParams {
  const params = new URLSearchParams()
  if (query.actorId) params.set('actorId', String(query.actorId))
  const action = query.action?.trim() ?? ''
  if (action !== '') params.set('action', action)
  const objectType = query.objectType?.trim() ?? ''
  if (objectType !== '') params.set('objectType', objectType)
  if (query.dateFrom) params.set('dateFrom', query.dateFrom)
  if (query.dateTo) params.set('dateTo', query.dateTo)
  return params
}

/** 审计列表（F-802）：筛选 + 分页，created_at DESC（同刻 id 降序兜底） */
export function listAuditLogs(query: AuditLogQuery): Promise<PageData<AuditLogItem>> {
  const params = buildFilterParams(query)
  params.set('page', String(query.page))
  params.set('pageSize', String(query.pageSize))
  return get<PageData<AuditLogItem>>(`/admin/audit-logs?${params.toString()}`)
}

export interface AuditExportResult {
  blob: Blob
  /** 取自 Content-Disposition（audit-{yyyymmdd}.csv）；解析失败回退固定名 */
  filename: string
  /** 行数超导出上限被截断（X-Truncated: true，api 层以同 WHERE 计数判定） */
  truncated: boolean
}

/**
 * CSV 导出（F-802/ADM-09）：fetch 字节流 → Blob，a[download] 触发由调用方完成
 * （须读 X-Truncated 响应头，统一封装 request 不透出头，故独立 fetch）。
 * 失败与统一信封同口径抛 ApiClientError（message 取信封中文提示或系统兜底）。
 */
export async function exportAuditLogs(
  query: Omit<AuditLogQuery, 'page' | 'pageSize'>,
): Promise<AuditExportResult> {
  const qs = buildFilterParams(query).toString()
  const res = await fetch(`/api/admin/audit-logs/export${qs === '' ? '' : `?${qs}`}`, {
    credentials: 'include',
  })
  if (!res.ok) {
    let code = BAD_RESPONSE_CODE
    let message = FALLBACK_MESSAGE
    try {
      const payload = (await res.json()) as { code?: unknown; message?: unknown }
      if (typeof payload.code === 'number') code = payload.code
      if (typeof payload.message === 'string' && payload.message !== '') message = payload.message
    } catch {
      // 非 JSON 错误体（网关错误页等），走系统兜底文案
    }
    throw new ApiClientError(code, message)
  }
  const disposition = res.headers.get('Content-Disposition') ?? ''
  return {
    blob: await res.blob(),
    filename: parseDispositionFilename(disposition) ?? 'audit-export.csv',
    truncated: res.headers.get('X-Truncated') === 'true',
  }
}

function parseDispositionFilename(disposition: string): string | null {
  return /filename="([^"]+)"/.exec(disposition)?.[1] ?? null
}

/**
 * 详情格式化（T2 评审移交契约）：JSON 字符串解析后缩进展示；解析失败原样字符串；
 * 空值统一 "-"。对象形态（直接 JSONB 反序列化）同样缩进序列化兜底。
 */
export function formatAuditDetail(detail: unknown): string {
  if (detail === null || detail === undefined) return '-'
  if (typeof detail === 'string') {
    try {
      return JSON.stringify(JSON.parse(detail), null, 2)
    } catch {
      return detail
    }
  }
  return JSON.stringify(detail, null, 2)
}
