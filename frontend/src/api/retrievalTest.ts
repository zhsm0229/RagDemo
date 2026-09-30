/**
 * 检索测试接口（docs/api.md §4.4.1，W3 管理端点，只读调参工具：不调 LLM、不落审计）。
 *
 * 分数口径红线（W3 交接，standards §4.4）：qa.similarity 为 embedding 余弦（qa 口径，
 * 对照问答对阈值）；docs[].rerankScore 为 rerank sigmoid 分、recallCosine 为召回阶段
 * 余弦——两套分数分属不同空间，页面必须分列展示、不得互比。
 */
import { post } from './client'
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
}

export interface RetrievalTestResult {
  qa: RetrievalTestQa | null
  docs: RetrievalTestDoc[]
  /** 当前生效阈值：qa=问答对阈值 / refuse=拒答阈值（config 缺省 0.85 / 0.55） */
  thresholds: { qa: number; refuse: number }
}

export function runRetrievalTest(body: RetrievalTestRequest): Promise<RetrievalTestResult> {
  return post<RetrievalTestResult>('/admin/retrieval-test', body)
}
