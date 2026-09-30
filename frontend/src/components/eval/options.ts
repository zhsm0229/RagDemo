/**
 * P9 评测管理共享枚举文案（对照原型与 api.md §4.2 口径），供页面与抽屉复用。
 */
import type { EvalRunStatus } from '@/api/evalAdmin'
import { ADMIN_DEPT_ID_OPTIONS } from '@/components/admin/options'

/**
 * W4 Task 5 阈值扫描端点（主分支 e42c4ce/15dfe55）未合入 feat/w4-fe：入口置灰
 * 不发无效调用，扫描调用与曲线/推荐值渲染按契约实现，合并置 true 即启用
 * （deviations #44，同 ZIP_IMPORT_ENABLED #21 模式）。对象形态便于 spec 翻转。
 */
export const FEATURE_FLAGS = {
  /** POST /admin/eval-runs/{id}/threshold-scan（W4 Task 5 主分支） */
  thresholdScan: false,
}

/** 跑分状态 tag：running（轮询中）/ done / failed（api.md §4.2.4 枚举） */
export function runStatusTag(status: EvalRunStatus): {
  text: string
  type: 'warning' | 'success' | 'danger'
} {
  if (status === 'running') return { text: '进行中', type: 'warning' }
  if (status === 'failed') return { text: '失败', type: 'danger' }
  return { text: '已完成', type: 'success' }
}

/** 题型中文（api.md §4.2.1 枚举 factual/multi_hop/cross_reject）；未知编码兜底原值 */
export const QTYPE_LABELS: Record<string, string> = {
  factual: '事实型',
  multi_hop: '多跳',
  cross_reject: '串库拒答',
}

export function qtypeLabel(qtype: string): string {
  return QTYPE_LABELS[qtype] ?? qtype
}

/**
 * 判定中文（含人工三档与自动 pending）。原型第三档"拒答正确"非 API 枚举——
 * 判定协议第 1 条锁 correct/partial/wrong，"拒答正确"语义由自动判分承载
 * （拒答题逐字比对话术判 correct，deviations #42）。
 */
export const JUDGMENT_LABELS: Record<string, string> = {
  correct: '正确',
  partial: '部分正确',
  wrong: '错误',
  pending: '待判定',
}

export function judgmentLabel(judgment: string): string {
  return JUDGMENT_LABELS[judgment] ?? judgment
}

/** 明细项 deptId → 中文（题库无部门列表端点，按种子序 id 映射，deviations #30 同口径） */
export function evalDeptLabel(deptId: number): string {
  return ADMIN_DEPT_ID_OPTIONS.find((option) => option.value === deptId)?.label ?? `#${deptId}`
}

/** 准确率/比率：0-1 比率 → 百分比 1 位小数（对照原型 (acc*100).toFixed(1)%） */
export function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}
