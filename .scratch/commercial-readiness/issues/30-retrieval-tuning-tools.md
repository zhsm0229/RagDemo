# 30: 检索质量调优工具（阈值扫描报告化 + 检索测试页增强）

**What to build:** 路线图阶段二 item 4：检索质量调优工具化——阈值扫描结果报告化（threshold-scan 现仅即时摘要，落盘/对比/导出，支撑 §5.5 真实阈值复测与 §17.3 调整附对比）+ 检索测试页增强（参数对比视图：同题多档 QA_THRESHOLD/REFUSE_THRESHOLD/topK 并排看命中差异）。**需设计先行**：先出设计文档走"设计→QC 评审→PM 放行→TDD 实施"流程（同工单 18/19/20/21），再编码。

**Blocked by:** None (can start immediately)（但实施以设计评审放行为前置）。

**Status:** done（2026-11-01 实施：设计 v1.1（QC R1-R7 有条件通过合入）+ PM 放行 → TDD 实施 → 后端 7 用例 + 前端 19 用例全绿）

- [x] 设计文档落盘并走评审（v1 → QC 测试工程师 R1-R7 有条件通过 → 修订合入 v1.1 + PM 放行，记录见 `design/30-retrieval-tuning-design.md` §8）：范围收窄（threshold-scan 报告化已在位不重复建设）、机制更正（settings 副本直传，弃 deps 副本失实引用）、增 efSearch 联动参数、回显合一、前端串行+逐档失败态+抽 ResultCard 守行数线、降级态 degraded 标注、schema 同步位
- [x] 后端：RetrievalTestRequest 增 5 可选档位（None=线上值逐字节一致；范围全在请求层 validators——model_copy 跳过校验），settings 副本直传 `run_retrieval_test`（app.state.chat_deps 零接触）；响应增 `params{topK,topN,efSearch,chunkDedupSim}`（chunkDedupSim 只回显）与 `degraded` 熔断标注；先红 7 用例全绿（缺省回归/qa 档位/refuse 两端/topK 截断/topN+ef 回显/越界 422×8/熔断 degraded）
- [x] 前端：结果渲染抽 `RetrievalResultCard`（单次/对比共用，View 守行数线）；对比模式缺省关=现状零变化，开=2-3 档参数输入+**串行执行**+单档失败不阻塞他档+degraded 档警示条；先红 5 用例全绿（缺省回归/档位伸缩/串行两档+读数标题/单档失败/降级标注）；档位增减按钮用原生 button（el-button 经组件 emit 的 click 在 jsdom 不可靠，工单 25 同款处置——模板曾引用未注入的 handler 致点击静默无效，调试定位后修复）
- [x] 同步位：api.md §4.4.1（请求体/响应/params/degraded/错误分支/最后更新台账）、schema.d.ts 再生、retrievalTest.ts 增键、前端既有 fixture 补新契约键（3 处）
- [x] 前端四门全绿（lint/prettier/type/vitest/build）；后端检索测试两文件 17 passed + make lint 通过
