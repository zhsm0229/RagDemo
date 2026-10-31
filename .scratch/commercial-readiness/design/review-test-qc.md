# 测试设计评审（QC 角色）——工单 18/19/20/21 四份设计

> 评审角色：测试工程师（六角色，TDD 门禁）｜日期：2026-10-31｜性质：快照（写完即冻结）
> 对照物：issues/18、19、20 + AGENTS.md 测试纪律（先红后绿／写库读回断言／禁 A or B／覆盖 ≥70%）。**issues/ 无 21 号工单文件**（见 R8），21 按设计自述范围核对。
> 核查方式：四设计逐条对照工单验收标准；源码与既有测试实读抽查（chat_cache/main/scheduler/auth/security/turn_plan/eval_engine/deps、tests/unit/test_chat_cache·test_auth_policy·test_jti_blacklist、tests/integration/test_hit_counter_flush·test_scheduler·test_migrations、tests/api/conftest、tests/conftest）。设计 file:line 锚点抽核全部属实。

## 一、19-state-externalize-design.md —— 有条件通过

**通过理由**：① 测试清单覆盖工单 19 全部 6 条验收标准（协议化/PG 三件/装配开关/登录限流黑名单同批/等价+并发/限制清单）；② 等价性可行——PG 实现一律"注入时钟算好再绑定（不用 SQL now()）"，FakeClock 可逐值对齐；TRUNCATE 每 Case 隔离与既有 pg fixture（rag_test，upgrade head 自动带上迁移 0020）契合；契约偏差（PG 空 flush 发一条 SELECT）显式登记；③ clear_by_ref 四分支、try_consume 跨日/恰限/拒不计、hit 读回/滚入、并发两用例齐；④ "既有测试不动"声明经实读核验基本成立（三个 memory 契约测试直接构造具体类、conftest:75-76 复位因 memory 分支复用 default_limiter/default_blacklist 单例而继续有效、test_hit_counter_flush 全部 test_mode=True 不触新闸）。ChatDeps 改 Protocol 注解对 7 处 kwargs 构造与 isinstance（无）零影响，核实无误。

**必修项**：
- **R1【19§7.1/7.2】跨实例共享语义无测试**：等价清单全部单实例视角——"外置"的核心价值（多进程同库互见、重启语义消除，票面原话）只对 daily_counter（两 session）与 hit（add/flush 并发）验证；limiter 与 jti 黑名单均无"两实例同库互见"用例。须补：PgSlidingWindowLimiter 两实例同键事件累计互见、PgJtiBlacklist 一实例 revoke 后另一实例 revoked=True（cache 跨实例读写可顺带）。
- **R2【19§5.1】clear_by_ref 等价域过强**：PG 版 refId 文本比较 vs 内存版 int 相等——JSON 里字符串 "7"／浮点 7.0 等异型 refId 两侧行为发散（内存不删、PG 删），"语义与内存版一致"表述不成立。须补异型 refId 用例钉死预期，或把等价域限缩为"refId 恒为应用侧 int"并写入设计。
- **R3【19§7.2】flush/add 并发确定性手段未指名**："flush 会话内 execute 前后由另一连接 add"未给注入点，不指名极易滑向 sleep 等待型 flaky。须钉死机制（建议 SQLAlchemy 事件钩子，如 after_execute/before_commit 触发他连接 add，或分步替身会话），并对齐 test_chat_cache.py:362 的确定性复现先例。

**建议**：新三表须同步既有 schema 契约测试（tests/integration/test_migrations.py 的表/列断言）与 specs/db-design.md——§9 文档同步位漏列（test_all_tables_exist 是成员断言不会红，但同步义务在）；PgSlidingWindowLimiter(60,10) 在 _build_state 硬编码复制 auth.py:102 字面量，宜两后端引同一常量+装配断言；jti 重复撤销多行、put 键前缀解析失败 dept_id=NULL 补两用例；等价测试显式传 session_factory（测试库 engine），chunks 种子沿用 committed seed/cleanup 纪律。

## 二、20-scheduler-split-design.md —— 有条件通过

**通过理由**：advisory lock 机制本身可测——会话级锁 + 两个 NullPool 引擎同进程即可确定性模拟两进程（顺序"恰一 True 一 False"+释放后再抢），互斥由 PG 语义背书、真并发留给 runbook 部署验证，口径成立；catchup 门控 monkeypatch Thread 可行；"开关只影响 start、装配恒完整"保住了 job 注册表断言。

**必修项**：
- **R4【20§7/§9.1】既有测试必破，"零破坏"声明不实**：tests/integration/test_scheduler.py:219 `test_build_scheduler_starts_in_normal_mode` 以 test_mode=False 直调 build_scheduler——新闸真连 settings.database_url（该用例未挂 pg fixture → 连 dev 库或连接失败）；失败走 except → is_leader=False → `assert scheduler.running is True` 必红；dev 机则真抢 dev 库锁且用例不释放。必须改写该用例（挂 TEST_DB_URL + 用毕 release/dispose）并在设计保护面登记。
- **R5【20§3.1】api 测试基建受染未评估**：tests/api `client` 形态（无 TEST_MODE）每用例 lifespan 都将抢锁/释锁（每次 +2 次 DB 往返 + NullPool 引擎创建，40+ 用例放大）。须写明处置：conftest 统一 SCHEDULER_ENABLED=false（并核实无 api 用例断言 scheduler.running/catchup 线程存在）或接受成本并量测；release_leader 生命周期（lifespan finally）须有集成断言。
- **R6【20 票面】验证项无归宿**：票面"workers=2 下限流/计数经 PG 实现正确（依赖 19）"在 §7 无对应用例。须补跨实例共享用例（与 19-R1 可合并为同一条：两实例互见即 workers=2 的进程内可测等价形态）或显式指针到 19 §7.2 并说明等价，runbook 部署验证单同步登记。

**建议**：try_acquire_leader 失败分支应 dispose engine 且不改 _leader_engine 全局（现草案失败路径既泄漏连接又覆盖全局引用，"持锁后再抢失败→释放"的单测形态会被 clobber 破坏）；"恰一 True 一 False"用例 docstring 注明互斥证明口径（顺序证明 + PG 语义 + 部署验证三层）。

## 三、21-rate-limiter-design.md —— 有条件通过

**通过理由**：① 协议纯函数化（参数进签名）使 memory/PG 等价测试无需动 Settings，方向正确；② FakeClock 精确推演成立——所选速率 FP 精确（60/min=1/s、30/min=0.5/s）、<1.0 边界判定确定，"拒绝只推进 last"用行为化断言（前拨恰补 1 令牌时长恰 1 次 True）不窥内部结构；③ 并发用例（Barrier+tiny_switch_interval+FakeClock 冻结）确定性成立（冻结期零补充、恰 burst 次放行）；④ 总闸关回归、bucket=None 短路、chat 拒绝不烧日配额（_usage 读回）齐；ChatDeps 追加缺省字段零影响核实无误。

**必修项**：
- **R7【21§4.3/§5.2】agent 入口对称断言缺失**：§4.3 核心声明"被拒请求未落占位行、未烧额度"在 agent 入口无测试（§5.2 只列 429 形态）。须补：agent 桶拒绝后 DailyCounter 未计数读回 + agent_runs 无占位行落库断言，与 chat 入口对称钉死。
- **R8【流程】对照物缺失**：issues/ 目录无 21 号工单，验收标准以设计 §6 自证，"覆盖工单全部验收标准"无从核对。须补票（含验收清单）或在设计头部注明出处（阶段三评审记录编号），补票前不得进入开发。

**建议**：API 测试开启口径写进 §5.2（RATE_LIMITER_ENABLED env + get_settings.cache_clear，仿 test_mode fixture 先例）；retryAfterSec 钉精确值断言（rate=30→2）锁死推导函数；PG 桶落地时按 19 等价基建补 memory/PG 参数化（§3 预留一句即可）。

## 四、18-ui-responsive-design.md —— 有条件通过（接近放行）

**通过理由**：① 事实断言全部核实——src 无 localStorage 使用、@element-plus/icons-vue 未装（仅 element-plus 2.14.6）、e2e 无 menuitem/aside 依赖、storageState 不含 localStorage → 默认展开论证成立；三处 flex-wrap 缺失清单可信；② main-layout.spec 六用例断言精确（逐一 class 存在性、禁 A or B、非法值容错），守账词断言与 help-sync 双保险，符合纪律；③ 双源文案同步（user-guide + help/employee.ts）履行红线 7，aria 命名避开"收起"歧义考虑周到；④ 本设计只覆盖工单 18 末项，范围声明清楚。

**必修项**：
- **R9【18§6.5】组件测试挂载口径缺失**：MainLayout 的 el-menu 为 router 模式，vitest 组件测试须注入 router（memory router 即可）+ 明确 Element Plus 组件注册方式（全局/按需）+ 用例间 localStorage 清理（jsdom 全局持久）。补三句挂载口径，否则首跑即卡。

**建议**：flex-wrap/min-width 纯 CSS 无自动化断言，可给 QuestionBankPanel 补一条样式存在性断言作最低防线；否则把 §5"人工三态走查"升格为工单验收必做项。可选 P3 e2e 维持可选。

## 总体放行建议：带条件放行

- 四份设计现状锚点全部核实准确，等价性/FakeClock 策略与项目测试纪律相符；放行前置 = 修完 R1–R9（设计文档修订 + 测试清单补全，均不涉代码返工）。
- 顺序：R4/R1 为"必破既有测试／核心价值未测"级，最优先；19→20 有依赖先修 19，21 依赖 19 协议面，18 可独立并行；R2/R3/R7 可随 TDD 首轮红测试一并落。
- 修订后按角色复审一轮即可开发；复审只验必修项落位，不重开全量评审。

**总结论：带条件放行——四份设计均"有条件通过"，修完 R1–R9（其中 R4 必破既有测试最优先）后即可进入 TDD 开发。**
