# 24: SSE 中断轮次配额回补（首 token 前中断不烧额度）

**What to build:** 用户在回答尚未输出任何内容时中断/断连（关页、网络断、手停），该轮日配额自动释放——重试零成本；回答已开始输出后中断照计（用户已获得价值）。补齐工单 14 漏实施项（"SSE 中断重试不重复计额度：mark_interrupted 轮次重试走同额度"），口径已于 2026-11-01 经用户裁决钉死。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent（口径裁决：首 token 下发前中断 → 回补该轮 1 次日配额；已输出后中断 → 照计）

- [ ] DailyCounter 增 `release(scope, key)` 回补方法：memory 实现（chat_cache.py 同族）与 PG 实现（state_pg.py）同契约，等价性测试钉死（回补不越 0 下界、并发安全、scope 隔离）
- [ ] chat_stream 中断收尾接线：复用既有"首 token 已下发"标志（SSE settled 标志模式先例）——仅首 token 前的中断收尾（GeneratorExit/异常/上游失败）调用 release；已输出后的中断不动计数
- [ ] 测试（先红）：首 token 前中断后 counter 读回该用户未计数；已输出大半后中断计数保留；评测专用 deps（旁路配额路径）行为不变；断连/异常/正常停止三入口同断言
- [ ] api.md 问答端点错误/语义行补一句中断回补口径；known-issues 开放-11 ①项关闭移出
- [ ] 全量回归 + make ci 绿
