# 商用就绪阶段三：统筹执行计划（execution-plan）

> 编制：PM｜日期：2026-10-31｜输入：design/18·19·20·21 四份设计 + issues/18·19·20 工单（**21 号工单票缺失**，见 §6）+ AGENTS.md 当前状态节
> 基线：standards.md v1.9；触发线红线 §25.4；本文为统筹安排快照，现状以各设计与工单原文为准（§19.1）

## 1. 批次与依赖

- **批次 B0（立即开工）**：工单 18 尽项——纯前端、无后端耦合，独立先行（约 0.5 人日）。
- **批次 B1**：工单 19 全量（五协议面 → PG 五实现 → StateBundle 装配 → 迁移 0020）。无上游阻塞。
- **批次 B1.5（并行窗口）**：19 的**协议面**（五 Protocol + ChatDeps 注解 + StateBundle 骨架）先行合入主干后，开发 B 即开工 21（memory 令牌桶 + 两入口接线）；开发 A 并行收尾 19 的 PG 实现与集成测试。21 只依赖协议形状、不依赖 PG 实现——合流点 = 19 设计 §3/§6。
- **批次 B2**：工单 20（调度器独立 + gunicorn 多副本）。**对 19 整单串行**——多副本语义成立的前提是副本间状态经 PG 外置（20 设计 §1"唯一有状态点已由 19 外置"；scale 覆盖文件顶部 fail-fast 声明 STATE_BACKEND=pg 前置）。
- 批次收口顺序（每批）：TDD 门禁（§3）→ 全量回归 → 文档同步（§5）→ PM 放行门。
- 迁移号：19 按 0020 撰写（0019 预留给并行父子块车道），合入时以 alembic/versions 实际 head+1 复核（现 head=0018 已核）。

## 2. 人力分配

| 角色 | 分工 |
|---|---|
| 开发 A | 工单 19 全量：协议+PG 实现+装配+迁移+等价性/并发集成测试 |
| 开发 B | 先支援 19 协议面评审；协议面合流后主责 21；19 整单收口后主责 20 |
| UI | 工单 18 实施 + 三态视觉验收（18 设计 §5 五项人工走查） |
| 测试 | 每批次红清单确认与测试设计评审 + 批次间全量回归 + 18 前端四门复核 |
| 架构师 | 实现期技术裁决（裁决点争议、迁移号冲突、compose 版本二选一等）+ PR 评审 |
| PM | 放行门：TDD 门禁核对、文档同步核对、批次放行与放行记录维护 |

## 3. 每工单 TDD 门禁（先红 → 实现 → 绿 → 回归）

**通用放行条件（全部工单）**：红清单经测试角色确认后方可开工；make lint 过；make ci 过（coverage ≥70%）；写库测试带读回断言、断言精确禁 `A or B`；各设计"既有测试不动清单"逐项核对。

- **19**：红 = 19 设计 §7.1 等价性参数化（memory/pg 双后端、rag_test 真库 TRUNCATE 隔离）+ §7.2 并发（计数不丢/flush 不丢）+ §7.3 装配（StateBundle 五件、test_mode 恒 memory、路由接线触达 bundle）。绿后回归：test_chat_cache / test_auth_policy / test_jti_blacklist（memory 专属契约，原样保留）+ tests/api 全量。放行条件：TTL 惰性删、clear_by_ref 四分支、flush 失败滚入均有读回断言。
- **20**：红 = 20 设计 §7（test_scheduler_flag 三态 + test_scheduler_leader 抢主/释放/失败不 start + lifespan catchup 只在 leader 起）。绿后回归：build_scheduler 既有用例零破坏。放行条件：scale 双副本部署验证进 runbook 检查单（日志恰一份"调度启动"、各副本一份"主锁被持有"）——非 pytest 项，随部署机窗口执行。
- **21**：红 = 21 设计 §5.1（FakeClock 令牌桶九例，含并发恰放行 burst）+ §5.2（两入口 429/ERR_RATE_LIMITED + retryAfterSec≥1、桶拒绝不烧日配额、总闸关回归）。绿后回归：chat 与 agent_runs 既有用例全量。放行条件：总闸缺省关与现状逐字节一致；bucket=None 兜底短路不抛。
- **18**：红 = main-layout.spec.ts 六例（18 设计 §6.5：默认展开/折叠切换/持久化/非法值容错/aria 随态/守账词）。绿后：前端四门（lint/type/326/build）全绿 + 三态人工走查；守账词未动、help-sync 无需改。

## 4. 风险 Top5 与缓解

| # | 风险 | 等级 | 缓解（引设计裁决点） |
|---|---|---|---|
| 1 | PG 计数并发竞态（超放/丢增量） | 高 | 19 §5.2 单语句 ON CONFLICT…RETURNING（PK 行锁原子，拒绝零写入）+ §5.3 FOR UPDATE 暂存-搬运两段式；§7.2 线程池并发测试钉死 |
| 2 | 双调度器（锁随连接回收静默丢失） | 高 | 20 §9.2 NullPool 独立连接持锁（生命周期=进程）；锁键字面量进 docstring 供 pg_locks 排查；§7 集成测试"恰一 True" |
| 3 | 多副本自愈误杀其他副本在途任务 | 中 | 20 §5 自愈 gate=SCHEDULER_ENABLED 副本专属；残余风险已量化（管理员可重试），instance_id 过滤留触发线后置 |
| 4 | 并行父子块车道迁移号冲突（0019） | 中 | 19 §4 预留声明 + 合入按实际 head+1 复核；AGENTS.md 警示改共享文件前重读 |
| 5 | 21 与 19 合流缝隙（StateBundle×bucket） | 中 | 19 §2.2 明示"bucket 归 21、本工单不动 ChatDeps 形状"；21 §3 以可选字段默认 None 合入（kwargs 零改动）；编排上协议面 PR 先行（§1） |

## 5. 文档同步清单汇总

| 工单 | 同步位 |
|---|---|
| 19 | deploy-requirements §7 条目 1/2（本单先补"进程内状态可外置"事实，终稿随 20）；.env.example 加 STATE_BACKEND；runbook 备份范围补 state_* 三表；api.md 无端点增减（标记行不动） |
| 20 | deploy-requirements §7 条目 1/2 终稿改写（单实例约束解除边界）；scheduler.py docstring；.env.example 加 SCHEDULER_ENABLED、WEB_CONCURRENCY；runbook 新部署拓扑小节（含锁排查 SQL） |
| 21 | api.md chat/stream 与 agent/runs 的 429 说明补一行；user-guide + frontend/src/help/ 补"操作过于频繁"文案；deploy-requirements §7 容量口径；.env.example 不加键（内部阈值口径，指针在 config.py 注释） |
| 18 | user-guide + employee.ts 各一句（折叠侧栏/展开侧栏）；@element-plus/icons-vue 登记技术栈清单 |
| 收口 | AGENTS.md"当前状态"节随各工单收口更新（唯一高频改写段） |

## 6. 设计评审放行记录（2026-10-31，PM 交叉评审）

评审维度：范围边界、裁决点合理性、与 §25.4 触发线一致性、工单间衔接缝隙。锚点抽验已做：ChatDeps 三字段为具体类注解（turn_plan.py）、scheduler start 闸（scheduler.py:238-239）、alembic head=0018、chat.py:139-141 与 agent_runs.py:85-90 接线位、ERR_RATE_LIMITED(2005)、auth default_limiter——均与设计相符。

- **19 有条件放行**。条件：①工单票"装配层选择：SINGLE_INSTANCE=true…"条目与设计 `state_backend` 枚举冲突，按设计 §8.1 裁决以枚举为准（为 A 阶段 Redis 留位），须**回写工单票注明偏差**（禁静默择一）；②迁移落地前按 alembic 实际 head 复核编号。裁决面（协议就近放置、test_mode 恒 memory、DailyCounter 单语句、HitCounter 暂存搬运、StateBundle 显式接线）全部认可；仅留 Redis 演进位不提前建设，§25.4 合规。
- **20 放行**。附注两条随实施 PR 执行：①compose ≥v2.24 的 `!reset null` 与独立 project 文件二选一，按部署机实测钉死并记录 runbook（设计 §6 已自含）；②scale 双副本实测属部署机硬项，随部署窗口完成并进 runbook 检查单。抢主锁在 build_scheduler 内/NullPool 持锁/catchup 跟随 leader/自愈 gate 用开关四项裁决认可；未建 LB、统一入口、instance_id 列——触发线纪律合规。
- **21 有条件放行**。条件：①**工单票缺失**（issues/ 目录仅 01-20，无 21 号票）——开工前由 PM/架构师补登 issues/21 票面（范围 = memory 令牌桶 + 两入口接线 + 总闸缺省关；PG 桶随 19 预留列后置触发），否则无票开工违反流程；②开工门 = 19 协议面（Proto + StateBundle + ChatDeps 注解）已合入主干。附注：rate_limiter_enabled 不进 .env.example（同 agent_model_* 内部阈值口径）评审接受，灰度开关口径以 deploy-requirements §7 说明兜底。令牌桶钉死语义（纠正票面"漏桶"混称）/bool 纯函数协议/AgentDeps 共享桶三项裁决认可。
- **18 放行**。范围与"不做"清单明确；e2e 零影响论证成立（storageState 不含 localStorage、spec 不依赖侧栏交互）；守账词未动；图标依赖带否决退路（内联 SVG）与 300 行红线预案。抽验确认工单 18 前五项已在库（badcase 菜单已改"问题反馈管理"、max-height 已铺），本设计即末项收口，无遗漏。
- **衔接缝隙结论**：StateBundle×bucket 合流 = 21 以第 6 字段可选合入（19 不动形状、21 默认 None），无冲突；20 对 19 为整单串行、21 对 19 为协议面级依赖，批次编排见 §1；除 21 缺票外无其余缝隙。
