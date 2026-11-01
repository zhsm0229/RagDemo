# 工单 30 设计：检索质量调优工具（收窄版：检索测试参数对比）

> 版本 v1.1（QC 评审 R1-R7 修订合入，2026-11-01）｜来源：路线图阶段二 item 4；
> 2026-11-01 设计期盘点发现 threshold-scan 报告化已在位（eval_scan 离线重放曲线+
> 推荐+复制 JSON 导出，RunReportPanel 已展示）——本设计范围收窄为**检索测试参数对比**，
> 避免重复建设（防提前建设红线）。

## 1. 现状锚点（已核实）
- 检索测试端点 `POST /api/admin/retrieval-test`（admin_retrieval_test.py）请求体仅
  `question`+`deptCode` 两参数——阈值/topK 用线上缺省值，改参需改配置重启，无法对比。
- 前端 RetrievalTestView.vue：单次查询+单结果视图，无对比。
- threshold-scan（eval_scan.py）：done run 的 detail 快照离线重放拒答/qa 曲线+推荐值，
  已存 detail.thresholdScan 可追溯、前端曲线展示+复制 JSON——报告化闭环已存在。

## 2. 目标（一句话）
管理员在检索测试页对**同一问题并排对比 2-3 组检索参数档位**（qaThreshold/refuseThreshold/topK/topN）
的命中差异，支撑 §5.5 真实阈值复测与 §17.3 调整附对比的取证效率——不改线上行为。

## 3. 后端（小改，R1/R3/R4 修订后）
- `RetrievalTestRequest` 增可选参数：`qaThreshold`（0-1）、`refuseThreshold`（0-1）、
  `topK`（1-50）、`topN`（1-20）、`efSearch`（1-200，R3：与 topK 联动口径——retrieval-policy
  §2 冻结档 20/40→30/64，自由参数下不强制联动但响应回显，topK>efSearch 时召回被 ef
  截断、对比结论失真须文档注明）；全部缺省 None=读线上值，行为与现状逐字节一致（回归红线）。
  **model_copy 跳过校验（R2）——范围约束全部落在请求层 field_validator。**
- **机制（R1 更正）**：`run_retrieval_test` 本就显式收 `settings` 参数、检索链路全部读该
  对象（retrieval_test_views.py:86-109 实证，不经 get_settings()）——端点对
  `deps.settings.model_copy(update={…})`（None 字段不进 update）后直传即可，
  **app.state.chat_deps 零接触**；弃 v1"deps 副本"表述（eval_engine 先例引用失实，
  其为 dataclasses.replace 且不覆盖阈值）。
- 响应回显（R4 合一）：既有 `thresholds{qa, refuse}` 键保持（旧契约零改动，值随参数
  生效值）；新增 `params{topK, topN, efSearch, chunkDedupSim}`——chunk_dedup_sim **列入
  回显不开放参数**（折叠去重口径与阈值对比弱相关，参数面只增噪音）。回显值即本次
  实际生效值（= 装配快照值或请求覆盖值）。
- 响应增 `degraded: bool`（R6）：rerank 熔断 open/half_open（get_rerank_breaker()）时
  true——向量序兜底下 rerankScore 为余弦口径，thresholdPassed 对照 refuse 阈值的对比
  **结论不可信**，文档注明。
- 错误分支补 `422/1003`（参数越界，字段级）。

## 4. 前端（对比视图，R5 修订后）
- RetrievalTestView 增"对比模式"开关：开时 2-3 组参数输入（qa/refuse 阈值、topK/topN/
  efSearch），**串行执行**（TEI CPU 串行下并发请求互相排队，embed 超时 60s 口径）；
  单组失败**不阻塞他组**、逐组独立错误态（对齐 W5 错误态红线）。
- 结果渲染**抽取** ResultCard 组件（R5：渲染现内联在 View 内，View 已 203 行，加对比
  模式必超 300 行红线——抽取既复用又守线）。
- 每组标题显示参数读数（如"档位 A：qa 0.85 / refuse 0.55 / topK 20 / ef 40"）；降级组
  结果卡标"简化检索（rerank 熔断，对比结论不可信）"。
- 缺省关=现状单次查询视图零变化。

## 5. 测试清单（先红，R6 补全）
- API：缺省 None 行为与现状一致（回归，逐字段）；qa 阈值调低 → qa 直答翻出（联动用例）；
  refuse 阈值调高 → 拒答；topK 变化 → 候选数变化；topN > topK 边界；越界 422/1003
  （每字段）；params/thresholds 回显断言；熔断 open 态 degraded=true（monkeypatch
  breaker）；非 admin 403/2004。
- 前端：对比模式关=现状视图（回归）；开=串行请求（顺序断言）、并排渲染 N 组、每组标题
  含参数读数、单组失败他组正常渲染、降级组标注。

## 6. 裁决点（R 系修订后确认）
1. **范围收窄**：threshold-scan 报告化已在位不重复建设；"跨轮扫描对比"若评审认为仍缺
   另立票（避免范围膨胀）。
2. **settings 副本直传而非全局**（R1 更正）：检索测试是管理端一次性取证，app.state.chat_deps
   零接触；线上调整仍走 .env + 评审流程（standards §17.3）。
3. **对比组数上限 3、串行执行**（R5）：TEI CPU 串行推理 3 次全链路 ~30-60s 可接受。
4. **chunk_dedup_sim 只回显不开放**（R4）。
5. **multi_query 不适用**（R6 注）：重放五步直放不经 retrieval_policy 动态路由，参数
   对比语义在线上链路与重放链路一致性边界内成立。

## 7. 文档同步位（R7 补全）
- api.md §4.4 检索测试：请求体/响应/错误分支/熔断期对比不可信注记。
- **frontend/src/api/schema.d.ts 再生**（RetrievalTestRequest 在生成 schema）+
  retrievalTest.ts 手写 RetrievalTestResult 增 params/degraded 键。
- user-guide 检索测试节 + help 双源：对比模式与熔断注记文案。
- AGENTS.md 当前状态节（收口时）。

## 8. 评审修订记录
- **v1.1（2026-11-01，测试工程师 QC R1-R7 有条件通过后合入）**：R1 机制更正（settings
  副本直传，弃 deps 副本与 eval_engine 失实引用）；R3 增 efSearch 参数与截断失真注记；
  R4 回显合一（thresholds 保持+params 新增，chunk_dedup_sim 只回显）；R5 前端串行+
  逐组失败态+抽 ResultCard 守行数线；R6 降级态 degraded 标注+熔断/qa 联动/回显/边界
  四类用例补全+multi_query 不适用前提；R7 补 schema.d.ts 再生与 ts 增键同步位。
  **PM 放行：2026-11-01，按 v1.1 实施**（放行条件①-⑦全部合入）。
