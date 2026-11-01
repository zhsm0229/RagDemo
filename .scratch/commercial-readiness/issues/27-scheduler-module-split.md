# 27: scheduler.py 拆分至 ≤300 行

**What to build:** scheduler.py 已 437 行（超 300 行红线，工单 20 后加重未拆分）——按 eval_synth 先例把 leader 抢主/锁连接管理或任务装配拆至子模块，build_scheduler 门面签名与返回类型不变。

**Blocked by:** None (can start immediately)。

**Status:** done（2026-11-01 实施：三子模块拆分，语义零变化）

- [x] 拆分后 scheduler.py 门面 158 行 + 三子模块（leader 57 / impl 125 / jobs 147），全部 ≤300、单函数 ≤50（ruff 把关）；`build_scheduler` 留门面（装配与选举同处审计，design/20 §9.1 裁决保持）
- [x] 语义零变化：调度五组测试 26 passed（test_scheduler / test_scheduler_flag / test_scheduler_leader / test_hit_counter_flush / test_eval_catchup）+ 后端全量绿；断言零改动，仅两处 import 路径适配（票面允许）：hit_flush 的 SessionLocal patch 目标、锁全局 `_leader_engine/_leader_conn` 读值目标随实现迁 `scheduler_jobs`/`scheduler_leader`
- [x] 锁连接生命周期语义不变（独立复核）：NullPool 独立持锁连接原样迁移、release 显式 `conn.close()`+dispose（no-op 坑注释随迁）、失败路径连接即关、`monkeypatch try_acquire_leader` 经门面 global 仍拦截 build_scheduler（R4 适配不回归）
- [x] known-issues 开放-11 ③项（scheduler 部分）关闭
