# 27: scheduler.py 拆分至 ≤300 行

**What to build:** scheduler.py 已 437 行（超 300 行红线，工单 20 后加重未拆分）——按 eval_synth 先例把 leader 抢主/锁连接管理（try_acquire_leader/release_leader/NullPool 引擎生命周期）或任务装配拆至子模块，build_scheduler 门面签名与返回类型不变。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent（code-review Standards 轴硬违反①同项）

- [ ] 拆分后 scheduler.py 与子模块各 ≤300 行、单函数 ≤50 行（ruff 把关）
- [ ] 语义零变化：test_scheduler / test_scheduler_leader / 调度相关全量用例绿，禁改断言（仅 import 路径适配）
- [ ] 锁连接生命周期语义不变：NullPool 独立连接随进程存活、release 显式释放（独立复核此点，防拆分时把"锁随连接回收静默丢失"的坑重新引入）
- [ ] known-issues 开放-11 ③项（scheduler 部分）关闭
