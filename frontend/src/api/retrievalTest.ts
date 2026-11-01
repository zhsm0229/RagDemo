/**
 * 检索测试接口（docs/api.md §4.4.1，W3 管理端点，只读调参工具：不调 LLM、不落审计）。
 *
 * 分数口径红线（W3 交接，standards §4.4）：qa.similarity 为 embedding 余弦（qa 口径，
 * 对照问答对阈值）；docs[].rerankScore 为 rerank sigmoid 分、recallCosine 为召回阶段
 * 余弦——两套分数分属不同空间，页面必须分列展示、不得互比。
 */
import { get, post } from './client'
import type { PageData } from './types'
import type { components } from './schema'

/** 请求体（schema RetrievalTestRequest）：question strip 后 1-2000 字 + deptCode 检索视角 */
export type RetrievalTestRequest = components['schemas']['RetrievalTestRequest']

/** 问答对命中：低于问答对阈值时后端直接返回 null，命中时 thresholdPassed 恒 true */
export interface RetrievalTestQa {
  id: number
  question: string
  similarity: number
  thresholdPassed: boolean
}

export interface RetrievalTestDoc {
  chunkId: number
  documentTitle: string
  page: number | null
  titlePath: string | null
  /** rerank sigmoid 分（与 recallCosine 不同空间，分列展示不互比） */
  rerankScore: number
  /** 召回阶段余弦 */
  recallCosine: number
  /** 对照当前 Settings 阈值逐块标记 */
  thresholdPassed: boolean
  /** true=进 top_n 但被重排折叠丢弃（调参需可见） */
  folded: boolean
  /** 历史召回次数（W5 F-1011 读现列：chat 链路 HitCounter 沉淀，检索测试自身不写计数） */
  hitCount: number
  /** 最近一次被召回时刻 ISO8601；null=从未被真实用户流量召回 */
  lastHitAt: string | null
}

export interface RetrievalTestResult {
  qa: RetrievalTestQa | null
  docs: RetrievalTestDoc[]
  /** 当前生效阈值：qa=问答对阈值 / refuse=拒答阈值（config 缺省 0.85 / 0.55） */
  thresholds: { qa: number; refuse: number }
  /** 本次实际生效参数（工单 30）：装配值或请求覆盖值；chunkDedupSim 只回显不开放 */
  params: { topK: number; topN: number; efSearch: number; chunkDedupSim: number }
  /** rerank 熔断 open/half_open=true——rerankScore 为余弦口径，对比结论不可信 */
  degraded: boolean
}

export function runRetrievalTest(body: RetrievalTestRequest): Promise<RetrievalTestResult> {
  return post<RetrievalTestResult>('/admin/retrieval-test', body)
}

// ---- 零召回块清单（W5 F-1011，GET /admin/zero-recall-chunks）----

/** 零召回块清单项：F-1009 零召回文档榜的块级下钻（同一口径的块级一面，服务端判定） */
export interface ZeroRecallChunkItem {
  chunkId: number
  documentTitle: string
  /** 部门编码原值（service/hr/all），展示经共享 deptCodeLabel 映射 */
  deptCode: string
  titlePath: string | null
  /** 服务端已截 50 字的内容预览 */
  contentPreview: string
  /** 块建档时刻 ISO8601（清单按其降序，同刻 id 降序兜底） */
  createdAt: string
}

export type ZeroRecallChunkPage = PageData<ZeroRecallChunkItem>

/** 查询参：days 观察窗口（服务端缺省 30，1-365），分页缺省 page=1/pageSize=20 */
export interface ZeroRecallChunkQuery {
  days?: number
  page?: number
  pageSize?: number
}

/** 零召回块清单：从未召回（last_hit_at IS NULL）或最近召回早于窗口起点的活跃块 */
export function listZeroRecallChunks(
  query: ZeroRecallChunkQuery = {},
): Promise<ZeroRecallChunkPage> {
  const params = new URLSearchParams()
  if (query.days !== undefined) params.set('days', String(query.days))
  if (query.page !== undefined) params.set('page', String(query.page))
  if (query.pageSize !== undefined) params.set('pageSize', String(query.pageSize))
  const qs = params.toString()
  return get<ZeroRecallChunkPage>(`/admin/zero-recall-chunks${qs === '' ? '' : `?${qs}`}`)
}
