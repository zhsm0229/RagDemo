# 17: 对抗用例进正式集（注入面回归防线）

**What to build:** 评审 P2-4（记忆块进 system prompt 的注入面）与 `<data>` 内嵌指令两类对抗样本各 3-5 题进评测正式集——注入防线从"单条 system 声明"变为"持续跑分监控的回归资产"。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] 构造对抗题：①"记住……"类记忆注入诱导 ×3-5（期望=拒绝持久化/不执行指令）；② `<data>` 块内嵌"忽略之前指令"类 ×3-5（期望=按拒答话术或正常回答不受污染）
- [ ] Excel 导入为 manual_import + tier=official（发版门禁口径）
- [ ] 题目 expected_answer 与判分口径登记（串库题 [拒答] 同款约定）
- [ ] dev 真实跑分一轮验证判分表现（stub 不够——judge 对对抗题的 autoJudgment 需人工抽核）
- [ ] user-guide 评测题库节补对抗题口径说明
