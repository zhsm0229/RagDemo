# 05: 调度可靠性一——防双跑唯一闸 + 失败日志

**What to build:** 周任务在多 worker/双容器部署下不再双跑、失败不再静默（开发 B 评审 P1-13/P1-14）：
1. TOCTOU：eval_auto_run.py:68-86 的 `_run_exists` 查-插间隙无锁，eval_runs.name 无唯一约束——两进程同周日同时通过检查 → 各建三个同名 run，双份三视角 LLM 账单 + 同名 run 污染环比。
2. eval_auto_run.py:89-95 异常分支无日志（全文件无 import logging）——引擎 try 之前抛的错（如 llm_base_url 未配置）只留 run 行脱敏通用语，日志零线索。

**Blocked by:** 01。

**Status:** ready-for-agent

- [ ] eval_runs 加 [自动] 前缀部分唯一索引（Alembic 仅追加迁移，中文注释），或任务体入口 pg_advisory_xact_lock
- [ ] 插入冲突时幂等跳过并记日志（非报错）
- [ ] run_weekly_eval 异常分支补 logger.exception（含 run_id 与视角码，不含内部细节泄敏）
- [ ] TDD：并发模拟（两 session 同时过幂等检查）只产生一组 run；异常路径日志有断言（caplog）
- [ ] 顺带清理：eval_auto.py 中零引用的死代码（_weekly_run_name/_run_exists/_dept_id_by_code/_PERSPECTIVE_DEPTS，grep 全库确认）删除，幂等键收敛 eval_auto_run 单处（§19.2 单一真相源）
