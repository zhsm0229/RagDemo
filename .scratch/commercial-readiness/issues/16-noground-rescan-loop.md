# 16: no_ground 种子闭环——语料更新后重扫 + 重试上限

**What to build:** 难例池"待补语料"从死清单变为活闭环：补完语料的种子可重新出题；病态种子不再每周空耗 LLM 预算。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] 种子重试上限：pending 种子合成失败/无据累计 skip_count，超 3 次转 no_ground（或需人工态），防每周重复消耗预算
- [ ] no_ground 重扫任务体：文档（重）入库成功后，按 dept+归一问题重扫 no_ground 种子——引用可达的转回 pending（幂等：UNIQUE 兜底不变）
- [ ] 手动重扫 API（admin，审计 hardcase.rescan）+ 难例池 UI"重扫"入口（带确认）
- [ ] TDD：skip_count 累计与转态、重扫转回 pending、UNIQUE 幂等、审计读回
- [ ] user-guide/help 双源同步"忽略/重扫"语义
