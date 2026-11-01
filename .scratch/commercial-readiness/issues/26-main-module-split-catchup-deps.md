# 26: main.py 拆分至 ≤300 行 + catchup 依赖注入修复

**What to build:** main.py 已 418 行（超 300 行红线，工单 19/20 后加重未拆分）——按 eval_synth 先例（门面+子模块，语义零变化）把装配代码拆出：StateBundle/_build_state/_build_chat_deps/启动补偿至装配子模块，main.py 门面保留 create_app/lifespan 编排。顺带修 catchup 孤立 HitCounter：启动补偿线程缺省自建 chat_deps（memory 后端下召回计数落永不 flush 的孤立实例）——改为注入 lifespan 已建 deps，memory/pg 两后端计数同源。

**Blocked by:** None (can start immediately)。

**Status:** done（2026-11-01 实施：拆分完成；catchup 注入修复经评审降级为后续项，理由见下）

- [x] 拆分后 main.py 门面 180 行 + deps_assembly.py 221 行 + spa_static.py 58 行，全部 ≤300、单函数 ≤50（ruff 把关）；lifespan/_startup_catchup/create_app 留守门面——保 test_scheduler_flag 五个 monkeypatch 目标经 main 模块 global 的拦截语义（改目标属断言改动，违"禁改断言"红线，故留守优于迁移）
- [x] 语义零变化：后端全量 1337 passed，断言零改动（仅 test_scheduler_leader 锁全局读值目标随实现迁——属工单 27 连带适配一并落此）
- [ ] catchup 注入修复（降级后续项）：拆分评审发现 `_build_chat_deps` 缺省自建 StateBundle 是**票面写明的兼容契约**（scheduler 周评测与补偿调用点零改动，工单 19 docstring），改为注入需同步改 scheduler_jobs._run_eval_auto_job 调用点并影响其测试——超出"拆分零变化"边界，且 pg 后端无此问题、memory 后端试点规模影响有限（开放-11 ④项本来就标"触发线后置"）——留触发线（memory 后端 + 高召回关注）后置，已知开放-11 ④继续登记
- [x] known-issues 开放-11 ③项（main 部分）全清；行数红线全仓回线
