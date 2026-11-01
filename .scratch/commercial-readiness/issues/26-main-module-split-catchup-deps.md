# 26: main.py 拆分至 ≤300 行 + catchup 依赖注入修复

**What to build:** main.py 已 418 行（超 300 行红线，工单 19/20 后加重未拆分）——按 eval_synth.py 拆分先例（门面+子模块，语义零变化）把装配代码拆出：StateBundle/_build_state/_build_chat_deps/启动补偿至装配子模块，main.py 门面保留 create_app/lifespan 编排。顺带修 catchup 孤立 HitCounter：启动补偿线程缺省自建 chat_deps（memory 后端下召回计数落永不 flush 的孤立实例）——改为注入 lifespan 已建 deps，memory/pg 两后端计数同源。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent（code-review Standards 轴硬违反①+裁量⑤；注意：用户并行会话在途，改共享文件前先重读）

- [ ] 拆分后 main.py ≤300 行、子模块各 ≤300 行、单函数 ≤50 行（ruff 把关）
- [ ] 语义零变化：全量回归绿，既有测试仅允许 import 路径适配，禁改断言
- [ ] catchup 注入修复：补偿线程复用 lifespan deps（或同源装配），memory 后端下召回计数落 lifespan 同一 HitCounter 实例（测试：catchup 执行后 hit_counter 可见增量）
- [ ] known-issues 开放-11 ③④项关闭移出（④并入本票）
