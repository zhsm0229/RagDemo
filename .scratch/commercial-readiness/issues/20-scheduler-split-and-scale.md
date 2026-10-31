# 20: 最小商用拓扑二——调度器独立与多副本

**What to build:** app 可水平扩展：调度器从 api 进程剥离为独立单副本（advisory lock 抢主），api 进程加 gunicorn workers/副本不再有重复调度与互杀。

**Blocked by:** 19（状态外置后副本间状态才一致）。

**Status:** ready-for-agent

- [ ] 调度器开关：SCHEDULER_ENABLED env（缺省 true 兼容现形态）——false 时 build_scheduler 不启动
- [ ] advisory lock 抢主：启动时 pg_try_advisory_lock，抢不到（双保险）则不启动调度器并记日志
- [ ] compose profile/compose 覆盖文件：scheduler 单副本服务形态（同镜像不同 command/env）
- [ ] Dockerfile/compose 支持 gunicorn workers（workers 数 env 可配，缺省 1 兼容）
- [ ] 验证：双副本起（scheduler_enabled=true）恰一个持锁调度；workers=2 下限流/计数经 PG 实现正确（依赖 19）
- [ ] deploy-requirements §7 限制清单更新（单实例约束解除的边界说明）
