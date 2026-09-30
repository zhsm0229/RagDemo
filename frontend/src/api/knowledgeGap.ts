/**
 * 知识缺口报告接口（F-1010，docs/api.md §4.9，W5 管理端点，只读聚合不留审计）。
 *
 * 口径：拒答（配对回答 answer_type=refuse）的用户问题按周聚合，count 降序，列表
 * 取前 50（total 为窗口内去重问题数，可大于 items 长度）；weekOffset 0=本周、
 * -1=上周、-2=前两周（UTC 周一为界）。导出为 CSV 字节流（UTF-8 BOM + 中文列头
 * 问题/出现次数），不走统一信封封装（同文件下载口径，client.ts 头注），行数超
 * 导出上限截断并以 X-Truncated: true 标注。
 */
import { get, ApiClientError, BAD_RESPONSE_CODE, FALLBACK_MESSAGE } from './client'

/** 周偏移：0=本周、-1=上周、-2=前两周（前端仅三档；后端允许 -11..0，越界 422） */
export type WeekOffset = 0 | -1 | -2

export interface KnowledgeGapItem {
  /** 用户问题原文（后端按 content 分组的键） */
  question: string
  /** 窗口内该问题的拒答次数 */
  count: number
}

export interface KnowledgeGapData {
  items: KnowledgeGapItem[]
  /** 窗口内去重问题总数（榜单只出前 50，total 可能更大） */
  total: number
  /** 聚合窗口起点（UTC 周一 00:00，ISO 字符串，含） */
  weekStart: string
  /** 聚合窗口终点（ISO 字符串，不含） */
  weekEnd: string
}

/** 知识缺口周榜（F-1010）：count 降序前 50 */
export function getKnowledgeGap(weekOffset: WeekOffset): Promise<KnowledgeGapData> {
  return get<KnowledgeGapData>(`/admin/knowledge-gap?weekOffset=${weekOffset}`)
}

export interface KnowledgeGapExportResult {
  blob: Blob
  /** 取自 Content-Disposition（knowledge-gap-{yyyymmdd}.csv）；解析失败回退固定名 */
  filename: string
  /** 行数超导出上限被截断（X-Truncated: true，api 层以同窗口计数判定） */
  truncated: boolean
}

/**
 * CSV 导出（F-1010）：fetch 字节流 → Blob，a[download] 触发由调用方完成
 * （须读 X-Truncated 响应头，统一封装 request 不透出头，故独立 fetch，同 auditLogs）。
 * 失败与统一信封同口径抛 ApiClientError（message 取信封中文提示或系统兜底）。
 */
export async function exportKnowledgeGap(
  weekOffset: WeekOffset,
): Promise<KnowledgeGapExportResult> {
  const res = await fetch(`/api/admin/knowledge-gap/export?weekOffset=${weekOffset}`, {
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
    filename: /filename="([^"]+)"/.exec(disposition)?.[1] ?? 'knowledge-gap-export.csv',
    truncated: res.headers.get('X-Truncated') === 'true',
  }
}
