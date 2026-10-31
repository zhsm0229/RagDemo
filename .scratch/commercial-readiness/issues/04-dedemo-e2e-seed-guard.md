# 04: 交付包去演示化——e2e_seed 公开口令管理员防呆

**What to build:** 私有化交付路径不再可能产生"全世界知道口令、改了也会被装回"的管理员账号（架构师评审 P0-2）。现状：bootstrap 试点装配 upsert `e2e-admin/E2ePass#2026`（startup-check.sh:77-80，口令公开在仓库并打印控制台），`must_change_password=False`（e2e_seed.py:204），且幂等 upsert 会在每次重跑装配时把已改密口令重置回缺省（e2e_seed.py:58）。prod 路径用 `__smoke__` 假凭据不落账号（deploy.sh:140-141）不受影响。

**Blocked by:** 01。

**Status:** ready-for-agent

- [ ] e2e_seed 加防呆：非测试库 + TEST_MODE=false 时拒绝 upsert 管理员（fail-fast 带中文说明），或强制 must_change_password=True + 随机口令仅打印一次
- [ ] 重跑装配不再重置已改密口令（检测密码哈希被人工修改过则跳过该账号）
- [ ] deploy/bootstrap.sh、startup-check.sh 及交付文档清除有效口令字面量（改环境变量必填，缺省 fail-fast）
- [ ] 复现测试（TDD）：TEST_MODE=false + 非测试 URL → upsert 被拒绝；试点口径（显式 CONFIRM）行为不回退
- [ ] runbook/bootstrap 文档同步"冒烟账号如何安全创建"说明
