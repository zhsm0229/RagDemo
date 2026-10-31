# 07: SSE 断开收尾——GeneratorExit 处理 + 幽灵占位行清扫

**What to build:** 用户关页/断网后服务端把会话收干净（开发 A 评审 P1-5）。现状：chat_stream.py:206-243 的 `_stream_generate` 只有 except UpstreamError/Exception——GeneratorExit 是 BaseException 不被捕获且无 try/finally → interrupt_turn 不跑 → assistant 占位行 `is_active=true, content=''` 永久残留：空正文进多轮改写 prompt 劣化质量、以 answer_type=NULL 污染概览命中率分母、且无清扫任务兜底（agent_runs 有幽灵清扫先例，messages 没有）。

**Blocked by:** 01。

**Status:** ready-for-agent

- [ ] `_stream_generate` 增补 `except GeneratorExit`：执行 interrupt_turn 收尾后 re-raise
- [ ] finally + 完成标志兜底（覆盖其他 BaseException 路径）
- [ ] 新增定时任务：占位超时无正文的 assistant 行置 is_active=false（覆盖进程被 kill 时 finally 也不跑的情形），参照 agent_runs 幽灵清扫口径
- [ ] TDD（TDD 先红后绿）：模拟客户端断开 → 占位行被置 inactive；历史加载不出现空 assistant 行
- [ ] 复跑既有 chat_stream 全部用例确认无回归
