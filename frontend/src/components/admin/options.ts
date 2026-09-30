/**
 * 管理三页（P6 用户 / P7 提问记录 / P8 badcase）共享枚举文案，对照原型与后端口径。
 * deptId 筛选参数映射与 qa/options 同一依据：列表端点无部门列表可查，按种子序
 * （app/db/seed.py：service → hr → all 自增 id，deviations #30）；item 的 deptCode
 * 直接走 code → 中文映射（后端返回编码原值）。
 */

export interface Option<T = string> {
  value: T
  label: string
}

/** 筛选下拉：deptId（GET /admin/messages、/admin/badcases 的查询参数） */
export const ADMIN_DEPT_ID_OPTIONS: Option<number>[] = [
  { value: 1, label: '客服' },
  { value: 2, label: '人力' },
  { value: 3, label: '全员' },
]

/** item deptCode（service/hr/all）→ 中文；未知编码兜底展示原值（不编造部门名） */
export const DEPT_CODE_LABELS: Record<string, string> = {
  service: '客服',
  hr: '人力',
  all: '全员',
}

export function deptCodeLabel(code: string): string {
  return DEPT_CODE_LABELS[code] ?? code
}

/** 发放账号的部门下拉（后端 _resolve_dept 枚举，api.md §4.4.1 同源） */
export const ADMIN_DEPT_CODE_OPTIONS: Option[] = [
  { value: 'service', label: '客服' },
  { value: 'hr', label: '人力' },
  { value: 'all', label: '全员' },
]

/** 回答类型（后端 TurnPlan.kind：cache/qa/rag/refuse）→ 中文标注 */
export const ANSWER_TYPE_LABELS: Record<string, string> = {
  cache: '缓存',
  qa: '问答对',
  rag: '文档检索',
  refuse: '拒答',
}

export function answerTypeLabel(answerType: string | null): string {
  if (answerType === null) return '-'
  return ANSWER_TYPE_LABELS[answerType] ?? answerType
}

/** badcase 处置状态筛选枚举（'待处理' 由 W2 点踩入口产生，队列初始态） */
export const BADCASE_STATUS_OPTIONS: Option[] = [
  { value: '待处理', label: '待处理' },
  { value: '已修语料', label: '已修语料' },
  { value: '已忽略', label: '已忽略' },
]

/** 状态列 tag 文案与语义色（对照原型：待处理 warning、终态 success） */
export function badcaseStatusTag(status: string | null): {
  text: string
  type: 'warning' | 'success'
} {
  const text = status ?? '待处理'
  return { text, type: text === '待处理' ? 'warning' : 'success' }
}
