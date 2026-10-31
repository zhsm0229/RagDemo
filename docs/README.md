# 文档地图（README）

> 定位：活文档（全库文档路由表）｜对账：2026-11-01｜同步保障：评审纪律——新增/移动/归档任何文档必须更新本表（standards §19.7）

本项目文档按**三档生命周期**管理（详见 [standards.md](standards.md) §19）：

- **活文档**：永远代表现状，改代码必须同步对应文档；
- **快照**：某时点的记录，文件名带 ISO 日期前缀，写完即冻结，被新版取代后移入 [archive/](archive/)；
- **台账**：AI 协作过程流水（`.superpowers/sdd/`、`.zcode/`），只增不改，冲刺收口后冻结，**不作为现状依据**。

## 想找什么 → 看哪里

| 找什么 | 去哪 | 档 |
|---|---|---|
| 项目是什么、决策依据 | [project-brief.md](project-brief.md) | 活 |
| 开发规矩（宪法，违反 = 不合入） | [standards.md](standards.md) | 活 |
| 团队实操手册（领卡/修 bug/评审/迁移/发布/迭代，场景式） | [playbook.md](playbook.md) | 活 |
| AI 代理 / 新人的入口约束 | [../AGENTS.md](../AGENTS.md) | 活 |
| 接口契约（端点总数由契约测试守账） | [api.md](api.md) | 活 |
| 部署与运维操作 | [runbook.md](runbook.md) + [../deploy/](../deploy/) 脚本 | 活 |
| 硬件需求 / LLM 轨道选型 | [deploy-requirements.md](deploy-requirements.md) | 活 |
| 用户操作说明 | [user-guide.md](user-guide.md)（界面文案双源：`frontend/src/help/`） | 活 |
| 开放问题与登记裁决 | [known-issues.md](known-issues.md) | 活 |
| 手工验收测试步骤与测试账号 | [manual-test-guide.md](manual-test-guide.md) | 活 |
| P0 用例与自动化层映射 | [e2e-case-matrix.md](e2e-case-matrix.md) | 活 |
| 商用就绪阶段一评审报告（P0/P1/P2 清单对照基线，工单 01-12 来源） | [2026-10-31-六角色评审报告.md](2026-10-31-六角色评审报告.md) | 快照（在有效期） |
| 设计基线（功能/库表/流程/演进/用例/原型） | [specs/](specs/)：features / db-design / flows / scaling-plan / test-cases / ui-mockups | 活（基线） |
| 历史评审报告、验收状态、合规清单、流程偏差 | [archive/](archive/)（文件名带日期，写完即冻结） | 快照 |
| 冲刺派工单 / 回执 / 评审 diff / 进度账本 | `.superpowers/sdd/` | 台账 |
| AI 会话方案规划 / 验收工作台 | `.zcode/` | 台账 |
| 培训资料（讲师教案 / 初学者导读） | [../training/](../training/) | 活 |

## 三条使用纪律（§19 精简版）

1. **同步义务**：改接口同步 api.md 标记行；改界面文案同步 user-guide + help；里程碑收口更新 AGENTS.md"当前状态"节。
2. **新增时点性文档**：文件名带日期前缀，写完冻结；被取代的快照移入 archive/ 并更新本表。
3. **引用完整性**：引用的命令/文件/路径必须真实存在；动态数字必须注明守账测试或对账日期。
