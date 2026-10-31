/**
 * 运营概览与语料健康度接口（docs/api.md §4.5，W3 管理端点，纯只读聚合不留审计）。
 *
 * 比率均为 0-1 四位小数，分母恒为 questions（命中率/拒答率/点踩率同口径可横向
 * 对比）；评测消息（is_eval=true）不入统计（W3 约束）。
 */
import { get } from './client'

/** days 仅 1|7 两档（窗口外后端 422/1003，api.md §4.5.1） */
export type OverviewDays = 1 | 7

/** 聚合指标（api.md §4.5.1）：tokens = prompt + completion 之和 */
export interface UsageStats {
  questions: number
  hitRate: number
  refuseRate: number
  downRate: number
  tokens: number
}

export interface OverviewData {
  totals: UsageStats
  /** 按 deptCode 升序且只含有数据的部门 */
  byDept: (UsageStats & { deptCode: string })[]
}

export function getOverview(days: OverviewDays): Promise<OverviewData> {
  return get<OverviewData>(`/admin/overview?days=${days}`)
}

/** 零召回文档（ready 且建档早于 30 天、近 30 天 0 命中），按建档升序 */
export interface ZeroRecallDoc {
  id: number
  title: string
  deptCode: string
  createdAt: string
}

/** 待过期文档（expiry_date ≤ 今天+7 天且未删除，含已到期未下线） */
export interface ExpiringDoc {
  id: number
  title: string
  /** YYYY-MM-DD */
  expiryDate: string
}

/** 评测质量项（评测集自动更新 08 票）：最近自动跑分带退化标记时返回，否则键省略 */
export interface EvalQualityWarn {
  status: 'warn'
  runId: number
  name: string
  previousAccuracy: number
  delta: number
}

export interface HealthData {
  totalDocs: number
  readyCount: number
  readyRate: number
  pendingConfirm: number
  zeroRecall: ZeroRecallDoc[]
  expiringSoon: ExpiringDoc[]
  /** 键省略 = 无自动跑分历史（前端整卡不显）；status 恒 'warn' 才下发 */
  evalQuality?: EvalQualityWarn
}

export function getHealth(): Promise<HealthData> {
  return get<HealthData>('/admin/health')
}

/** 服务健康卡（api.md §4.5.4）：依赖探活；key 六键、status up/down/not_configured 三态 */
export interface ServiceHealthItem {
  /** key/status 全集以 api.md §4.5.4 为准；gotenberg=老格式转换探活（convert_base_url 空=not_configured） */
  key: 'pg' | 'embed' | 'rerank' | 'llm' | 'mineru' | 'gotenberg'
  label: string
  status: 'up' | 'down' | 'not_configured'
  latencyMs: number
  detail: string
}

export interface ServicesHealthData {
  /** up=问答链路全绿；degraded=仅 mineru 不可用；down=问答链路任一不可用 */
  overall: 'up' | 'degraded' | 'down'
  services: ServiceHealthItem[]
}

export function getServicesHealth(): Promise<ServicesHealthData> {
  return get<ServicesHealthData>('/admin/services/health')
}
