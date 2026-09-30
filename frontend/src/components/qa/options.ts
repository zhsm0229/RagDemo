/**
 * 问答对管理页共享枚举文案（对照原型 QaView 与 api.md §4.1 枚举口径）。
 * status 与后端枚举同源：enabled=启用（参与检索）/ disabled=停用（不参与）。
 */
import type { QaPairStatus } from '@/api/qaPairs'

export interface QaOption<T> {
  value: T
  label: string
}

export const QA_STATUS_OPTIONS: QaOption<QaPairStatus>[] = [
  { value: 'enabled', label: '启用' },
  { value: 'disabled', label: '停用' },
]

/**
 * qa-pairs 列表 DTO 只有 deptId（api.md §4.1.3，无 deptCode），部门列与筛选用
 * deptId → 中文映射。取值 = 种子序（app/db/seed.py：service → hr → all 自增 id），
 * 无部门列表端点可查；后端若补 deptCode 字段则此处一行切换（deviations #30）。
 */
export const DEPT_ID_LABELS: Record<number, string> = { 1: '客服', 2: '人力', 3: '全员' }

export const DEPT_ID_OPTIONS: QaOption<number>[] = [
  { value: 1, label: '客服' },
  { value: 2, label: '人力' },
  { value: 3, label: '全员' },
]

/** 未知 deptId 兜底展示（不编造部门名） */
export function deptIdLabel(deptId: number): string {
  return DEPT_ID_LABELS[deptId] ?? `部门#${deptId}`
}
