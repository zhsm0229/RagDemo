# 11: 合规签批升级机制——出网白名单轨道闭环

**What to build:** 逾期未闭环的合规签批从"待办表里躺着"变为带日期的升级行动项（PM 评审 P0-4）。现状：compliance-checklist-2026-09-30.md:1"草稿，待合规方签批"、:3"已逾期"、签批人/日期空白；出网白名单（唯一出网=LLM chat）轨道未经合规方书面确认，runbook §8"合规轨道切换"就没有触发依据。

**Blocked by:** None (can start immediately，外部依赖项)。

**Status:** ready-for-agent（本工单主体为用户侧外部动作 + 文档登记）

- [ ] 合规签批列为带日期的一号行动项（升级路径：对接人 → 用户 → 交付评审）
- [ ] 短期无法取得签批时：书面按"内网轨预案"（runbook §8 三键切换）准备报价与资源口径
- [ ] known-issues §四 同步登记现状与升级日期
- [ ] 签批完成后回填 compliance-checklist 签批人/日期并归档
