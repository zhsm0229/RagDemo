# 06: 调度可靠性二——持久化 + 停机补偿扫描

**What to build:** 容器停机跨过周日 04:00/05:00 UTC 后，重启能自动补跑当周合成流水与三视角跑分——F-1019"每周必有一轮"的承诺在无人值守场景成立（架构师+PM 评审互证 P1：MemoryJobStore 无持久化、未配 misfire_grace_time/coalesce，停机跨窗口任务静默丢失且无补偿）。

**Blocked by:** 03（补偿验证需要跑分链路真的能成功）、05（补偿触发依赖幂等闸防补跑变双跑）。

**Status:** ready-for-agent

- [ ] 调度持久化：任务触发记录落库（app_settings 或专用表，Alembic 迁移仅追加+中文注释）或 APScheduler SQLAlchemyJobStore——择一，与"任务体纯函数可手动调用"模式兼容
- [ ] 启动补偿扫描：应用启动时检查"当周应跑未跑"（复用 eval 任务"同周在册跳过"幂等）→ 补跑一次，日志留痕
- [ ] APScheduler 显式配 misfire_grace_time + coalesce（其余 5 个日常任务一并覆盖）
- [ ] 评测域附带收口：running 幽灵 run 清扫（崩溃遗留的 running 占住幂等键导致该周静默缺失——复用 agent_runs stale sweep 先例）
- [ ] TDD：FakeClock 模拟"停机跨周日"→ 启动 → 补跑恰好一轮；同周重放不重复
- [ ] runbook §10 增补停机补偿行为说明与"周一核查 [自动] run"项
