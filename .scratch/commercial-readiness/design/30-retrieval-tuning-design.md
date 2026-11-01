# 工单 30 设计：检索质量调优工具（收窄版：检索测试参数对比）

> 版本 v1（设计先行，待 QC 评审+PM 放行）｜来源：路线图阶段二 item 4；
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

## 3. 后端（小改）
- `RetrievalTestRequest` 增可选参数：`qaThreshold`（0-1，缺省 None=读线上值）、
  `refuseThreshold`（0-1，None 同上）、`topK`（1-50）、`topN`（1-20）——`None` 时行为
  与现状逐字节一致（回归红线）。
- 检索测试链路把请求参数传到 deps 副本（同 eval_engine"评测专用 deps"先例：
  model_copy 覆盖阈值字段，不落 app.state.chat_deps——**只影响本次测试请求**）。
- 响应增 `params: {qaThreshold, refuseThreshold, topK, topN}` 回显（实际生效值）。
- 错误分支补 `422/1003`（参数越界，字段级）。

## 4. 前端（对比视图）
- RetrievalTestView 增"对比模式"开关：开时显示 2-3 组参数输入（qa/refuse 阈值、topK/topN），
  每组独立请求、并排渲染结果（命中列表+qa 直答+拒答态）——复用现有结果渲染组件
  （结果卡片抽组件或 v-for 渲染）。
- 每组标题显示参数读数（如"档位 A：qa 0.85 / refuse 0.55 / topK 20"）。
- 缺省关=现状单次查询视图零变化。

## 5. 测试清单（先红，实施时）
- API：缺省 None 行为与现状一致（回归）；显式参数生效（拒答阈值调高→拒答；topK 变化→
  候选数变化）；越界 422/1003；非 admin 403/2004。
- 前端：对比模式关=现状视图（回归）；开=并排渲染 N 组结果、每组标题含参数读数。

## 6. 裁决点（评审确认）
1. **范围收窄**：threshold-scan 报告化不再重复建设（盘点发现已在位），本工单只做参数对比——
   若评审认为仍缺"跨轮扫描对比"，另立票不并入（避免范围膨胀）。
2. **参数经 deps 副本而非全局**：检索测试是管理端一次性取证，绝不落全局配置（与评测
   专用 deps 同构）；线上调整仍走 .env + 评审流程（standards §17.3）。
3. **对比组数上限 3**：TEI CPU 串行推理下 3 次全链路 ~30-60s（可接受），更多组属
   跑分引擎职责而非测试页。

## 7. 文档同步位
- api.md §4.4 检索测试：请求体/响应/错误分支同步。
- user-guide 检索测试节 + help 双源：对比模式文案。
- AGENTS.md 当前状态节（收口时）。
