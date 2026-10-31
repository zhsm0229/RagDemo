# 03: 修 F-1019 双 P0——自动跑分链路接通（triggered_by + config 合并快照）

**What to build:** 周日自动跑分从"必失败"变为端到端可用。两个缺陷（开发工程师 B 评审 P0-1/P0-2，主干已复核）：
1. `eval_engine.py:212` 的 `user_id = run.triggered_by`——自动 run 的 triggered_by=None，直写 messages.user_id（NOT NULL，0001_init.py:142）→ 首题 flush 即 IntegrityError，被收口为 failed。引擎对会话 owner 已有 admin 回落（:92-97），消息行漏了。
2. `eval_engine.py:313` 的 `run.config = snapshot` 整体覆写——把 06 票写入的 `config={"perspective": code}`（eval_auto_run.py:80）抹掉 → mark_regression 读 perspective=None 直接返回，vsPrevious 恒 null、退化标记永不生效、健康卡 evalQuality 永不出现。人工 run 的 config 从未有过 perspective，07 票"人工 run 同样享受对比"同样不成立。

**Blocked by:** 01（基线冻结后新改动新提交）。

**Status:** ready-for-agent

- [ ] `_run_one` 的消息行 user_id 改用 conversation.user_id（已含 admin 回落）而非 run.triggered_by
- [ ] `_finish` 改合并快照：`run.config = {**(run.config or {}), **snapshot}`（保留 perspective 等预写键）
- [ ] 人工 run 创建路径（eval_service）同步落 perspective 键
- [ ] 补"真链路"端到端测试（TDD 先红后绿）：triggered_by=None → 真引擎（非 executor 替身）→ 读回消息行 user_id=admin → 报告 vsPrevious 非空 → 人工构造退化数据 → detail 含 regression 标记
- [ ] 修复后手动触发一次三视角跑分（dev 库），验收报告页按层级表+环比行真实出现
