/**
 * 文档管理页共享枚举文案（对照原型 S.deptName 与部门/状态下拉口径）。
 * 值与后端枚举同源：dept code（service/hr/all）、文档状态机（pending/parsing/ready/failed）、
 * zip 导入逐文件报告状态（accepted/duplicate/rejected/skipped_metadata，T3 契约）、
 * 草稿审核状态（待审核/已通过/已驳回，agent-evolution #05 迁移 0007 CHECK 同源）、
 * 文档分型五档（policy_clause/table_report/notice/operation_manual/general，
 * metrics-balance 工单 01 迁移 0010 CHECK 同源；文档域常量，不放 eval 域）。
 * 状态列 tag 文案在 DocumentsView 模板内逐分支对照原型书写（单一真相源：原型即基线）。
 */
import type { ReviewStatus, ZipEntryStatus } from '@/api/documents'

export interface Option {
  value: string
  label: string
}

export const DEPT_OPTIONS: Option[] = [
  { value: 'service', label: '客服' },
  { value: 'hr', label: '人力' },
  { value: 'all', label: '全员' },
]

export const DEPT_LABELS: Record<string, string> = {
  service: '客服',
  hr: '人力',
  all: '全员',
}

export const STATUS_OPTIONS: Option[] = [
  { value: 'pending', label: '排队中' },
  { value: 'parsing', label: '解析中' },
  { value: 'ready', label: '就绪' },
  { value: 'failed', label: '失败' },
]

/** 文档分型五档下拉选项与中文映射（metrics-balance 工单 01，与后端 0010 CHECK 同源） */
export const DOC_TYPE_OPTIONS: Option[] = [
  { value: 'policy_clause', label: '制度条款' },
  { value: 'table_report', label: '表格报表' },
  { value: 'notice', label: '通知公告' },
  { value: 'operation_manual', label: '操作手册' },
  { value: 'general', label: '通用' },
]

export const DOC_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  DOC_TYPE_OPTIONS.map((option) => [option.value, option.label]),
)

/** 分型文案映射 + 未知兜底：五档外的新 code 原样回显（自描述），空值显"未知" */
export function docTypeLabel(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '未知'
  return DOC_TYPE_LABELS[value] ?? value
}

/** zip 逐文件结果四态中文映射与语义色 */
export const ZIP_STATUS_TAGS: Record<
  ZipEntryStatus,
  { label: string; type: 'success' | 'warning' | 'danger' | 'info' }
> = {
  accepted: { label: '已入库', type: 'success' },
  duplicate: { label: '重复跳过', type: 'warning' },
  rejected: { label: '已拒绝', type: 'danger' },
  skipped_metadata: { label: '元数据待补', type: 'info' },
}

/** 草稿审核状态三态中文映射与语义色（agent-evolution #05） */
export const REVIEW_STATUS_TAGS: Record<
  ReviewStatus,
  { label: string; type: 'success' | 'warning' | 'danger' | 'info' }
> = {
  待审核: { label: '待审核', type: 'warning' },
  已通过: { label: '已通过', type: 'success' },
  已驳回: { label: '已驳回', type: 'danger' },
}
