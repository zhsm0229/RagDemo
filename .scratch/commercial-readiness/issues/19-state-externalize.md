# 19: 最小商用拓扑一——进程内状态外置

**What to build:** 问答链路三类进程内单例状态（ExactCache/DailyCounter/HitCounter）外置为可插拔实现（接口不变、PG 存储实现）——app 加副本/workers 的前置条件，六接口哲学的第七个替换点。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] 接口化：Cache/Counter/HitCounter 协议面（现实现即缺省实现，行为零变化）
- [ ] PG 实现：exact_cache（命中键+TTL）、daily_counter（原子日计数）、hit_counter（聚合 flush 已走库——验证其 flush 语义在多写者下正确）
- [ ] 装配层选择：state_backend 枚举（memory|pg，缺省 memory）走进程内/PG——部署形态开关进 .env.example 与限制清单（2026-10-31 设计评审回写：原 SINGLE_INSTANCE 布尔语义锁死，按 design/19 §8.1 枚举为准，为 A 阶段 Redis 留位）
- [ ] 登录限流/登出黑名单同批评估外置（重启语义消除）
- [ ] TDD：两实现行为等价（同输入同结果）+ PG 实现并发语义（两 session 同 key 计数不丢）
- [ ] 单实例约束的限制清单（deploy-requirements §7 条目 1）随开关更新
