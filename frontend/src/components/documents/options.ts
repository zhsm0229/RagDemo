/**
 * 文档管理页共享枚举文案（对照原型 S.deptName 与状态下拉/状态列 tag 口径）。
 * 值与后端枚举同源：dept code（service/hr/all）、文档状态机（pending/parsing/ready/failed）、
 * zip 导入逐文件报告状态（accepted/duplicate/rejected/skipped_metadata，T3 契约）。
 */
import type { DocumentStatus, ZipEntryStatus } from '@/api/documents'

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

/** 状态列 tag 文案与语义色（对照原型 ● 就绪 / ◐ 解析中 / ✖ 失败 / ◔ 排队中） */
export const STATUS_TAGS: Record<
  DocumentStatus,
  { label: string; type: 'success' | 'warning' | 'danger' | 'info' }
> = {
  ready: { label: '● 就绪', type: 'success' },
  parsing: { label: '◐ 解析中', type: 'warning' },
  failed: { label: '✖ 失败(悬停看原因)', type: 'danger' },
  pending: { label: '◔ 排队中', type: 'info' },
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
