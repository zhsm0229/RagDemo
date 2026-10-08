/**
 * 评测管理接口（docs/api.md §4.2，W3 管理端点 + W4 阈值扫描契约）。
 *
 * 跑分为异步任务（202 创建即 running，后台逐题 commit）：轮询 GET /{id} 时
 * running 期 detail 仅含 items 无 pendingCount 键、failed 态无 pendingCount 键
 * （W3 交接契约），前端必须容忍键缺失。阈值扫描端点（主分支 e42c4ce）未合入
 * feat/w4-fe：调用与曲线渲染按契约实现，入口置灰待翻转开关（deviations 同 #21）。
 */
import { get, post, request } from './client'
import type { CitationItem, PageData } from './types'

export type EvalRunStatus = 'running' | 'done' | 'failed'
/** 人工录分三档（判定协议第 1 条，API 锁枚举；折算 correct=1 / partial=0.5 / wrong=0） */
export type EvalJudgment = 'correct' | 'partial' | 'wrong'
/** 自动判分：拒答题逐字比对固定拒答话术；其余题型恒 pending 留人工（api.md §4.2.5） */
export type AutoJudgment = 'correct' | 'wrong' | 'pending'

/** 跑分列表项（api.md §4.2.4 全 7 键）：correct 为加权总分（可半分），accuracy 4 位小数 */
export interface EvalRunItem {
  id: number
  name: string
  status: EvalRunStatus
  total: number
  correct: number
  accuracy: number
  createdAt: string
}

/** 收口时 10 字段快照（锁定阈值/模型/提示词与 judge 提示词版本，防跨跑对比被污染）；running 期为 null */
export interface EvalRunConfig {
  model: string
  promptVersion: string
  qaThreshold: number
  refuseThreshold: number
  efSearch: number
  topK: number
  topN: number
  temperature: number
  manifestVersion: string
  /** judge 提示词版本（metrics-balance 工单 07，eval_judge.JUDGE_PROMPT_VERSION） */
  judgePromptVersion: string
}

/** 逐题明细（api.md §4.2.5）：题面文本不在 DTO（题库无列表端点），以 questionId+expected 呈现 */
export interface EvalRunDetailItem {
  questionId: number
  deptId: number
  qtype: string
  expected: string
  /** 单题失败（error 非 null）时可能为空串（引擎初始占位） */
  answerType: string
  answer: string
  citations: CitationItem[]
  autoJudgment: AutoJudgment
  /** 仅单题失败时非 null（user 消息留痕、不落 assistant 行） */
  error: string | null
  /** 人工录分后逐题追加；最终判定 = judgment 优先否则 autoJudgment */
  judgment?: EvalJudgment
}

/** detail 结构：pendingCount 为可选键（running/failed 期缺失，轮询必须容忍） */
export interface EvalRunDetail {
  items: EvalRunDetailItem[]
  pendingCount?: number
}

export interface EvalRunDetailData extends EvalRunItem {
  config: EvalRunConfig | null
  detail: EvalRunDetail | null
}

export function listEvalRuns(query: {
  page: number
  pageSize: number
}): Promise<PageData<EvalRunItem>> {
  const params = new URLSearchParams()
  params.set('page', String(query.page))
  params.set('pageSize', String(query.pageSize))
  return get<PageData<EvalRunItem>>(`/admin/eval-runs?${params.toString()}`)
}

export function getEvalRun(runId: number): Promise<EvalRunDetailData> {
  return get<EvalRunDetailData>(`/admin/eval-runs/${runId}`)
}

/** 发起跑分（202）：检索范围恒为 [视角部门, 全员]，题面部门只进报告口径不参与检索 */
export function createEvalRun(body: {
  name: string
  perspectiveDeptCode: string
}): Promise<{ id: number }> {
  return post<{ id: number }>('/admin/eval-runs', body)
}

/** 人工评判录分：仅 status=done 可评（running/failed 422/3004）；返回即时重算 summary + pendingCount */
export function submitJudgments(
  runId: number,
  items: { questionId: number; judgment: EvalJudgment }[],
): Promise<EvalRunItem & { pendingCount: number }> {
  return post<EvalRunItem & { pendingCount: number }>(`/admin/eval-runs/${runId}/judgments`, {
    items,
  })
}

// ---- 分组报告（api.md §4.2.7）：running/failed 也可出报告（部分明细 + 真实状态，无状态闸） ----

export interface EvalReportTotals {
  total: number
  correct: number
  accuracy: number
  pendingCount: number
}

export interface EvalReportGroup {
  total: number
  accuracy: number
}

/** 四指标单项（metrics-balance §4.4）：value=null 表示无参算题（拒答类等记 null 排除） */
export interface FourMetricStat {
  value: number | null
  evaluatedCount: number
}

/** 四指标（contextRecall/contextPrecision/answerRelevancy/answerCorrectness），顺序冻结 */
export interface EvalReportFourMetrics {
  contextRecall: FourMetricStat
  contextPrecision: FourMetricStat
  answerRelevancy: FourMetricStat
  answerCorrectness: FourMetricStat
}

/** 分型分组项（按题目 sourceDocId 指向文档的 docType 分组；unassigned 四指标恒 null） */
export interface EvalReportDocTypeGroup {
  docType: string
  questionCount: number
  contextRecall: number | null
  contextPrecision: number | null
  answerRelevancy: number | null
  answerCorrectness: number | null
}

export interface EvalReport {
  config: EvalRunConfig | null
  status: EvalRunStatus
  totals: EvalReportTotals
  byDept: (EvalReportGroup & { deptCode: string })[]
  byQtype: (EvalReportGroup & { qtype: string })[]
  fourMetrics: EvalReportFourMetrics
  byDocType: EvalReportDocTypeGroup[]
}

export function getEvalReport(runId: number): Promise<EvalReport> {
  return get<EvalReport>(`/admin/eval-runs/${runId}/report`)
}

// ---- 阈值扫描（F-1012 简化版，主分支 e42c4ce 未合入：入口置灰，deviations 同 #21） ----

/** 拒答曲线档位（0.40-0.70 步长 0.05 共 7 档）：accuracy=重放权重均值，refuseRate=判拒比例 */
export interface RefuseCurveItem {
  threshold: number
  accuracy: number
  refuseRate: number
}

/** qa 曲线档位（0.75-0.95 步长 0.05 共 5 档）：hitRate=各档仍命中比例（口径局限见 scope.note） */
export interface QaCurveItem {
  threshold: number
  hitRate: number
}

export interface ThresholdScanResult {
  refuseCurve: RefuseCurveItem[]
  qaCurve: QaCurveItem[]
  recommended: { refuseThreshold: number | null; qaThreshold: number | null }
  scannedAt: string
  /** scope.note（qa 曲线口径局限）页面必须原样展示（T5 评审裁决） */
  scope: {
    refuseTotal: number
    qaTotal: number
    pendingSkipped: number
    errorSkipped: number
    note: string
  }
}

export function runThresholdScan(runId: number): Promise<ThresholdScanResult> {
  return post<ThresholdScanResult>(`/admin/eval-runs/${runId}/threshold-scan`)
}

// ---- 题库（api.md §4.2.1/§4.2.2）：Excel 整批导入 + 模板下载 ----

export interface EvalImportResult {
  imported: number
}

/** 错误行（422/1003 的 data 全量）：同问答对导入 {row, field, reason}；row=0 为文件级错误 */
export interface EvalImportErrorRow {
  row: number
  field: string
  reason: string
}

/** 部门在文件行内（中文名 客服/人力/全员），与问答对导入的表单 deptCode 口径不同（api.md §4.2.1） */
export function importEvalQuestions(file: File): Promise<EvalImportResult> {
  const form = new FormData()
  form.append('file', file)
  return request<EvalImportResult>('/admin/eval-questions/import', {
    method: 'POST',
    body: form,
    silent: true,
  })
}

/** 模板下载直链（§4.2.2，xlsx 附件）；同源直链 httpOnly Cookie 随请求携带 */
export const EVAL_QUESTIONS_TEMPLATE_URL = '/api/admin/eval-questions/template'
/** Content-Disposition 文件名（api.md §4.2.2） */
export const EVAL_QUESTIONS_TEMPLATE_FILENAME = 'eval_questions_template.xlsx'
