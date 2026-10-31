# 21: 令牌桶分级限流（memory 实现 + 两入口接线）

**What to build:** 在登录滑窗与日配额之间插入第二层平滑限速：令牌桶按"用户×入口域"限速（问答 30/min 突发 10、agent 10/min 突发 4），三层独立判定（桶拒绝不烧日配额）；总闸缺省关（行为与现状一致，灰度由部署决策）。防脚本化高频打满 TEI/LLM，agent 多轮链路与问答主链各自独立水位。

**Blocked by:** 工单 19 的协议面合流先行合入主干（RateBucketProto + StateBundle + ChatDeps.bucket 第 6 可选字段，默认 None）——PM 评审放行条件②。

**Status:** ready-for-agent（设计文档 design/21-rate-limiter-design.md v1，测试工程师 QC 有条件通过 + R7 修订已合入）

- [ ] TokenBucketLimiter memory 实现（懒补充/拒绝不扣水位/last 推进防时钟回拨/FakeClock 精确推演）：突发连发全放行、超突发拒、每秒恰补 1 令牌、水位封顶、scope 隔离、reset 契约、并发恰放行 burst 次
- [ ] RateBucketProto 入 chat_cache.py 协议族；ChatDeps/AgentDeps 加 bucket 可选字段共享实例；_build_state 装配（memory 分支）
- [ ] Settings 四键+总闸（rate_chat_per_min=30/burst=10、rate_agent_per_min=10/burst=4、rate_limiter_enabled=False 缺省关）；两入口接线（chat.py 日配额前、agent_runs.py 并发闸后），429 带 retryAfterSec=max(1,ceil(60/rate))
- [ ] API 测试：桶拒绝 429 未开流 + retryAfterSec≥1、桶拒绝不烧日配额、agent 入口对称断言（不烧日配额且不落占位行，R7）、总闸关闭行为不变
- [ ] PG 桶实现不实施（state_counters scope='bucket' 由迁移 0020 预留 refill_at，随多副本部署决策触发）
- [ ] 文档：api.md 两端点 429 说明补 retryAfterSec；user-guide/help 补"操作过于频繁"文案含义；deploy-requirements §7 容量口径
