# 30: 检索质量调优工具（阈值扫描报告化 + 检索测试页增强）

**What to build:** 路线图阶段二 item 4：检索质量调优工具化——阈值扫描结果报告化（threshold-scan 现仅即时摘要，落盘/对比/导出，支撑 §5.5 真实阈值复测与 §17.3 调整附对比）+ 检索测试页增强（参数对比视图：同题多档 QA_THRESHOLD/REFUSE_THRESHOLD/topK 并排看命中差异）。**需设计先行**：先出设计文档走"设计→QC 评审→PM 放行→TDD 实施"流程（同工单 18/19/20/21），再编码。

**Blocked by:** None (can start immediately)（但实施以设计评审放行为前置，票状态先行登记）。

**Status:** needs-design（路线图阶段二 item 4，2026-11-01 code-review spec 轴缺口；范围以设计文档评审收口为准，避免提前建设）

- [ ] 设计文档落盘（现状锚点：threshold-scan 端点与检索测试页现有能力盘点 → 报告化/对比最小闭环 → 裁决点：报告存储位置、对比维度、是否入 app_settings 审计）
- [ ] QC 评审 + PM 放行记录（.scratch/commercial-readiness/design/ 目录）
- [ ] 按设计 TDD 实施与本票验收（实施时另拆子票）
