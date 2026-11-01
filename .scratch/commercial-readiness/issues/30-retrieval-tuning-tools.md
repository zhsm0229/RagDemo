# 30: 检索质量调优工具（阈值扫描报告化 + 检索测试页增强）

**What to build:** 路线图阶段二 item 4：检索质量调优工具化——阈值扫描结果报告化（threshold-scan 现仅即时摘要，落盘/对比/导出，支撑 §5.5 真实阈值复测与 §17.3 调整附对比）+ 检索测试页增强（参数对比视图：同题多档 QA_THRESHOLD/REFUSE_THRESHOLD/topK 并排看命中差异）。**需设计先行**：先出设计文档走"设计→QC 评审→PM 放行→TDD 实施"流程（同工单 18/19/20/21），再编码。

**Blocked by:** None (can start immediately)（但实施以设计评审放行为前置，票状态先行登记）。

**Status:** needs-design（路线图阶段二 item 4，2026-11-01 code-review spec 轴缺口；**设计文档已落盘待评审**：`design/30-retrieval-tuning-design.md` v1——设计期盘点发现 threshold-scan 报告化已在位，范围收窄为检索测试参数对比，避免重复建设）

- [x] 设计文档落盘（2026-11-01 v1：现状锚点核实→范围收窄至参数对比→后端可选参数面/deps 副本裁决/前端对比视图/测试清单/三裁决点）
- [ ] QC 评审 + PM 放行记录（design/ 目录，走 18/19/20/21 同款流程）
- [ ] 按设计 TDD 实施与本票验收（实施时另拆子票）
