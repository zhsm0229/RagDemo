# 24: SSE 中断轮次配额回补（首 token 前中断不烧额度）

**What to build:** 用户在回答尚未输出任何内容时中断/断连（关页、网络断、手停），该轮日配额自动释放——重试零成本；回答已开始输出后中断照计（用户已获得价值）。补齐工单 14 漏实施项（"SSE 中断重试不重复计额度：mark_interrupted 轮次重试走同额度"），口径已于 2026-11-01 经用户裁决钉死。

**Blocked by:** None (can start immediately)。

**Status:** done（2026-11-01 实施：TDD 先红 12 用例 → 实现 → 全绿；超线拆分连带完成）

- [x] DailyCounter 增 `release(user_id)` 回补方法：memory（chat_cache.py）与 PG（state_pg.py，UPDATE...WHERE day=当日 + GREATEST(n-1,0) 防负账）同契约；等价性测试钉死（回补后可再消费、重复回补不越零下界、无记录零写入、跨日 no-op）
- [x] chat_stream 中断收尾接线：event_stream 门面新增"非空白 token 交付"守卫（emitted 标志）——收尾时零交付（首 token 前断连 GeneratorExit / 上游故障 / 空正文 error）调 release；已输出后中断照计；prepare 失败 500 分支（chat.py interrupt 后）同回补
- [x] 测试（先红→绿）：memory 3 用例 + PG 等价 4 用例（两后端×4）+ api 5 用例（断连零交付回补/已交付照计/上游故障回补/空正文回补/prepare 失败回补+占位行中断标记）；评测专用 deps 不经 event_stream 守卫天然不受影响
- [x] api.md 问答端点补"配额回补"语义行 + 最后更新台账；known-issues 开放-11 ①项关闭
- [x] 增量触 300 行红线连带拆分（eval_synth 门面先例）：chat_cache 拆出 hit_counter.py、chat_stream 拆出 chat_stream_branches.py——门面 re-export 导入路径不变（conftest SessionLocal patch 目标随迁），行数全部回线
