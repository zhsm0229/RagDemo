# 检索动态适配（RetrievalPolicy）· 设计文档 v1.1

状态：已实施（2026-10-09 三方评审裁决并入 v1.1 后按此编码，工单台账 .scratch/retrieval-policy/progress.md；08/09/10 挂起票待基线跑分与 Mac Mini 硬件）。编码必须严格遵循本文档；冲突先评审指出，不许静默择一。
配套：docs/specs/metrics-balance.md（四指标评测裁判 + doc_type 分型，直接前置）；standards §17.3（检索/prompt 变更须附评测对比）。

## 1. 目标与门槛三分类

**目标**：检索手段按「问题信号 × 文档分型 × 任务 × 算力档」动态路由到程序分支，重手段全部预埋、按证据开闸；检索上游故障不再整轮失败。检索范围基线（工单 22，2026-11-01 生效）：检索语句前置生效日期闸——仅 `effective_date ≤` 当前日（DB 时钟 UTC 口径）的文档可召回，未生效文档恒不可见（standards §11.4）；到期下线（expiry_date）语义不变。

**门槛三分类**：
- **A 资源门槛**（本机 CPU TEI 不能测，Mac Mini gpu 档解锁）：agent 精排恢复、multi_query 激活、HyDE 实验、基线提速。
- **B 证据门槛**（本机可测，§17.3 流程门）：L1 意图分类激活、动态 top_k/ef_search 激活、字符预算开闸——激活=四指标跑分对比+评审。
- **C 纪律永久门**（唯二不做）：语义缓存（禁清单）；动态 top_p（无收益证据，known-issues 备查）。

## 2. RetrievalPolicy 设计（app/rag/retrieval_policy.py，纯函数域，禁 import services/db）

`RetrievalPolicy` dataclass——**每个字段缺省值=现状行为**：

| 字段 | 类型 | 缺省（=现状） | 取值 |
|---|---|---|---|
| top_k | int | 20 | P1 联动档 30 |
| ef_search | int | 40 | P1 联动 64 |
| rerank_mode | str | "full" | "vector_only"（降级/熔断态） |
| multi_query | bool | False | True（双门控：L0 multi_hop 信号 且 compute_tier=gpu） |
| char_budget | int \| None | None | 机制就位默认不限 |
| intent | str | "single_hop" | L0: "multi_hop"；L1: LLM 标签 |
| degraded_hint | bool | False | 上游降级时置 True（SSE 透出） |

`resolve_policy(question_signals, doc_type, task, compute_tier, circuit_state) -> RetrievalPolicy`：纯规则零 LLM 零延迟。
- **L0 启发式词表**（常量）：比较类（相比/对比/区别/哪个更/差异）、多实体（以及/并且/分别/各/还）、因果（为什么/如何影响/会导致）——**词表避免单字歧义词**（"和""与"不入表，防"海南"误报）；命中 ≥2 类或累计 ≥3 处 → intent=multi_hop。
- **doc_type 入参语义（裁决）**：主链路查询期检索前无 doc_type 可得，**恒传 "general"**；该参数为 agent TASK_PROFILES 目标型别覆盖预留（工单 07）。
- `compute_tier`：config 增项（cpu_constrained 默认 / gpu）。熔断 open → rerank_mode=vector_only。
- 缺省输入产出与现状参数**逐字相等**（等价证明断言基础）。

**全链契约（裁决）**：TurnPlan **追加** `degraded: bool = False`（retrieval_trace 先例，计划内演进）；迁移 0012 `messages.degraded` BOOLEAN NOT NULL DEFAULT FALSE（中文注释 + EXPECTED_COLUMNS 同步 + finalize_turn 契约测试计划内演进）；GET messages 回传该字段（cache_hit 先例）——重开会话标注不消失。

## 3. 弹性保护（P0 弹性票）

- **rerank 失败兜底**：`rerank_and_fold` 捕 `UpstreamError` → 向量序兜底；**降级标记走收集缝** `degraded_out: list[bool] | None = None`（ranked_out 同款，主路径不传零改动）；降级态 top_n callable 在向量序上求值（docstring 注明 top1 为向量序口径）；降级态 ranked_out 写 (hit, 召回余弦) 且 trace 增 `scoreScale: "cosine"`（禁止冒充 rerank 分）。
- **降级拒答阈值**：config `fallback_refuse_threshold=0.63`（开放-2 对照命中带 0.63-0.71 下沿，漏拒带从 0.6509 起——取对照带下沿为保守下限，实测后按降级态负例校准；与 refuse_threshold=0.55 两套口径不互比 §4.4）。降级轮 citation score 记余弦并 scoreScale 标注。
- **熔断器（分层裁决）**：状态机在 **core**——`app/core/circuit_breaker.py` 纯类，注入 Clock（clock.py 先例）+ 访问器 `get_rerank_breaker()`；**装配层** `CircuitBreakerReranker` 装饰器实现 Reranker 协议，在 main._build_chat_deps 包装 build_reranker 产物。参数：连续 3 次 → 开路 300s → 半开单发探测（探测期间其余请求直接 vector_only，不并发探测）→ 成功回 closed 清零计数。config 总闸手动关。**失败口径**：一次 rerank_and_fold 调用抛 UpstreamError（tei 内部重试后）计 1 次；成功重置连续计数；multi_query 激活后每子查询各计一次（明示接受）。状态接 services_health（rerank_circuit: closed/open/half_open）。
- **embed 失败分支（捕获层裁决）**：捕获点在 **prepare_turn（services 层）**——主链路/评测/检索测试三调用方同享；产出拒答话术计划 + degraded=True，answer_type 恒 'refuse'（refuseRate 现口径不动，降级率单列不合并）。
- **缓存裁决**：**降级态 rag 计划 cache_key=None 不写缓存**（refuse 同款）——避免混尺分数入缓存与标注丢失，代价仅故障窗口重算。

## 4. LLM 调用时机与 token 硬预算

- 每题 LLM ≤2 次、≤6k in + 4k out。时序固定：`[多轮?改写] → embed → [qa短路 | 检索+rerank] → 生成`。
- **L1 三产物合并调用（P1 预埋）**：multi_query 门控命中时改写升级为一次调用三产物（改写问题+意图标签+子查询 ≤3，子查询 ≤150 token）——不加往返；走云 LLM 与本机算力无关，激活=B 类（四指标对比）。
- **multi_query 激活（A 类）**：embed 一次多向量、召回合并按余弦去重、rerank ≤3 次（仅 gpu 档）。
- HyDE：不建——触发线登记（multi_query 实测不足再评估）。

## 5. SSE degraded 与 UX

- **仅 done 事件**携带 `degraded: bool`（缺省 False，旧帧容错；citations/token 事件不带）。前端回答旁"简化检索"轻标注：复用"来自缓存"先例（el-tag size=small type=info，msg-meta 行内，顺序=已拒答→拒答说明→简化检索→来自缓存→耗时），拒答与简化检索**双标签同显**（诚实信号）；流式/失败/停止态不显示。文案同步 user-guide/help（红线 7）、api.md §1.3。
- **看门狗竞态修复（裁决）**：rerank 最坏 240s（120×2）与前端 STALL_TIMEOUT_MS=240_000 相等属先到先判竞态（上波提 rerank_timeout 时引入）——前端 STALL 调整为 300_000（后端 120s 实测口径不动），watchdog 测试同步。
- 开路前最坏延迟预算：最多 3 个请求各吃满 240s 后熔断生效（PM 知情项）。

## 6. agent 对齐（收敛一把尺）

agent 检索工具补 match_qa 直答通道（≥0.85，跨部门问答对隔离不变）+ 折叠去重——**抽出 rag 域纯函数 `fold_top_n(hits, top_n, dedup_sim)`**，主链路 rerank_and_fold 内部与 agent 共用（"不另造"落在 fold 纯函数）；参数读 policy；精排仍关（A 类）；excerpt 500 字截断保留。熔断 open 态 agent 同样 vector_only 可用。行数红线预案：chat.py/retrieval.py 触 300 行按 conversations_store 先例转出。

## 7. 字符预算（P0.7 机制就位默认关）

预算常量+截断标注接线，缺省 None 不截；0 与负数按缺省处理；激活值等基线数据（B 类）。

## 8. 测试 seams（冻结版）

- **unit**：resolve_policy 缺省=现状七字段逐字；L0 词表边界（恰 2 类/累计恰 3 处）；熔断状态机（FakeClock 注入：3 败→open→299s 拒绝→300s half_open→成败两向→总闸关闭恒 closed→成功清零）；兜底降级（stub 抛错→向量序+fallback 阈值分轨：降级态 0.58 拒/0.61 过）；embed 分支；三产物解析（P1）；fold_top_n 纯函数；模块结构断言（纯函数域禁 import services）。
- **integration**（真 pg+Stub 三件套，全 Stub 禁真容器注入）：故障注入全链无 500 漏出；降级态部门隔离同尺；agent 与主链路对照（对照命中集与折叠，不对照 rerank 分）；degraded 落 messages 列读回（迁移 0012）；SSE 事件序不变 + done.degraded 断言（正常/qa/cache 轮恒 false 且字段不缺席）。
- **api**：services_health 暴露 rerank_circuit 三态；embed 故障 SSE 返回 200 拒答话术而非 500。
- **frontend**：done degraded=true 气泡"简化检索"标注；与 cacheHit 并存双标注；degraded=false 不渲染。
- **等价证明由既有用例承担，不新增测试文件**。
- 纪律：TDD 中文用例标题；读回断言；测试全 Stub 不抢 TEI（基线 run#2 在跑）。

## 9. 评测口径稳定性（§17.3 保护）

trace 与 eval detail item 增 `degraded` 布尔；run config 快照与报告增 `degradedCount`；**工单 08 门禁：degradedCount=0 方可对比**。降级导致的拒答在评测中**记 error 不计分**（非知识缺口，判 wrong 会污染指标）。config 快照记录 compute_tier。

## 10. Mac Mini 工作包（独立票 09/10，独立排期）

TEI arm64 核实或 ONNX-Metal（reranker ONNX 导出有先例）｜MinerU 解析解耦（Parser 接口）｜compose gpu 档 profile｜四指标基线+NFR #7 复测｜pg 卷迁移。

## 11. 评审裁决记录（2026-10-09，PM 裁决）

1. degraded 全链契约：TurnPlan 追加字段 + 迁移 0012 messages.degraded + GET messages 回传（架构师 F1、设计师①⑤）。
2. 降级答案**不写精确缓存**（cache_key=None，refuse 同款）——拒绝"条目带 degraded 字段"备选（架构师 F2）。
3. 熔断器：core 纯类 + 装配层装饰器 + get_rerank_breaker() 访问器；失败=一次调用抛 UpstreamError；成功清零；half_open 并发=其余直接 vector_only（架构师 F3、测试 1/2）。
4. 评测口径：detail/快照/报告增 degraded(degradedCount)；08 门禁 degradedCount=0；降级拒答记 error 不计分（架构师 F4）。
5. 兜底缝：degraded_out 收集缝 + 降级态 ranked_out 写余弦 + trace scoreScale="cosine"；top_n callable 向量序上求值（架构师 F5）。
6. embed 分支捕获在 prepare_turn；answer_type 恒 refuse；refuseRate 不动、降级率单列（架构师 F6、测试 8）。
7. fallback_refuse_threshold **0.63**（对照带下沿；v1 的 0.60 与"宁拒勿编"方向矛盾已修正）（架构师 F7）。
8. 前端 STALL_TIMEOUT_MS 240s→300s（修复 240s 竞态，后端 120s 不动；设计师"不动 watchdog"约定据此豁免）（架构师 F8）。
9. doc_type 主链路恒 "general"，为 07 票 TASK_PROFILES 覆盖预留（架构师 F9）。
10. 折叠抽纯函数 fold_top_n 主/agent 共用（架构师 F10）。
11. L1 载体=改写升级三产物调用（合并无额外往返）；L0 词表避单字歧义词（测试 8）。
12. 概览/健康面板**前端渲染延后**（后端字段本波就绪，展示登记后续小票）——防波面扩大（设计师④）。
