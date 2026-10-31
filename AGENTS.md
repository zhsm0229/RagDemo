# AGENTS.md

> 定位：活文档（项目约束入口）｜对账：2026-10-07｜同步保障：当前状态节数字须守账测试背书（standards §19.7）

面向 AI 编码代理（与新人）的项目入口说明。**规范基线是 [docs/standards.md](docs/standards.md)（v1.9，评审基线，违反 = 不合入）**；本文件只汇总日常高频约定，未覆盖之处以 standards.md 及 [docs/specs/](docs/specs/) 设计文档为准，两者冲突时也不是"二选一"——先在评审中指出矛盾，不许静默择一。全库文档地图见 [docs/README.md](docs/README.md)。**团队协作的实操手册（领卡/修 bug/评审/迁移/发布/迭代，场景式可照抄）见 [docs/playbook.md](docs/playbook.md)。**

## 项目概览

企业智能工作台（RAG 知识问答）：员工提问，系统基于部门知识文档与问答对回答并给出可核对的引用。客服 + HR 双部门试点 MVP；设计按 100x 规格一次到位，建设按触发线分期，**未到触发线禁止提前建设**（禁清单见 standards.md §25.4：Milvus、K8s、微服务、细粒度 ACL、语义缓存等）。

技术栈：FastAPI(Python 3.11+) · Vue3+TS+Element Plus · PostgreSQL+pgvector · MinerU · TEI(BGE-M3) · Docker Compose 单机 · Alembic · pytest。

## 常用命令（Windows，Git Bash）

```bash
make install    # 建 .venv 并安装 -e ".[dev]"
make lint       # ruff check app tests
make fmt        # ruff check --fix + ruff format
make test       # 全量 pytest
make test-unit  # 仅 tests/unit
make test-api   # 仅 tests/api
make ci         # pytest --cov=app/rag --cov=app/services --cov-fail-under=70
make up / down  # docker compose 起/停（pg 等）
```

日常只跑新增/改动相关测试；全量回归仅在里程碑收口或怀疑影响其他模块时执行（standards §15.10）。

## 架构与分层

五层单向依赖，禁止反向：`api（路由/DTO）→ services（业务）→ rag（解析/分块/检索/生成）→ core（llm/config/security）→ db（模型/仓储）`。

- 可替换点一律接口化，共六个：`LLMProvider` / `VectorStore` / `Parser` / `Embedder` / `Reranker` / `FileStorage`；auth 独立为 `core/security.py` + `services/auth.py`。实现类只在装配层（依赖注入处）选择。
- 跨模块取数必须走对应 service 函数，禁止直查其他模块的表；api 层禁止直写 SQL。
- 单文件 ≤300 行、单函数 ≤50 行；禁止裸 `except:`。
- LLM/向量库等外部调用必须设超时（chat 60s / embed、rerank 10s / 解析 300s）+ 一次重试。
- 新模块不得堵死预留退路（六接口、无状态 JWT、dept→ACL 演进等，§1.7）。

## 硬性红线（评审强制检查项）

1. **部门隔离**：涉及部门的表查询必须带部门过滤；按 ID 访问资源必须校验归属（防 IDOR）。
2. **SQL 一律参数绑定**（SQLAlchemy），禁止 f-string/format 拼接。
3. **知识不足必须拒答**："知识库中未找到相关内容"，绝不编造——产品验收红线。
4. 密钥只走 `.env`（git 已忽略），仓库只留 `.env.example`；密钥禁止出现在日志/响应/前端。
5. 上传三重白名单（扩展名 + Content-Type + 魔数）；文件名 UUID 落盘；解析/解压设上限（§6.3）。
6. LLM 调用：`max_tokens` 服务端 clamp；用户问题只走 user role；召回内容包裹 `<data>` 标签声明不可信（§17.5）。
7. 提交前自查：参数绑定 ✓ 密钥不泄漏 ✓ 输入校验 ✓ LLM 安全约束 ✓ 审计留痕 ✓ 错误脱敏 ✓ 界面文案同步 ✓（改界面原文必须同步 docs/user-guide.md 与 frontend/src/help/，§23.2）。

## 代码风格

- Python：ruff（lint + format，line-length 100）；全部函数带类型注解；领域词汇用英文（dept / qa_pair / chunk / citation），禁止拼音。
- 业务错误统一 `ApiError`（错误码 + 中文消息），由全局异常处理器转响应；错误码用共享枚举，禁止魔法数字。
- 注释只写"为什么"（约束、坑、决策依据），不复述代码。
- 统一响应体 `{"code": 0, "message": "ok", "data": ...}`；JSON 键 camelCase（Pydantic alias），DB snake_case 在 service 层映射，禁止直接返回原始行；列表响应 `{ items, total, page, pageSize }`。
- 新增依赖须说明用途并登记到设计文档技术栈清单，能不加就不加。

## 测试

- `rag/` 与 `services/` 实行 TDD（先写失败测试→最小实现→通过）；覆盖率 ≥70%（`make ci` 把关）。
- 写库测试必须带**读回断言**（SELECT 验证落库内容），禁止只看执行不报错。
- 断言精确：状态码、关键字段逐一断言，禁止 `A or B` 宽松断言。
- bug 修复先写复现测试再修，该测试永久保留；串库用例与部门过滤单测所有 MR 必跑（用例集就绪后生效）。

## 数据库与迁移

- 结构变更只走 Alembic，迁移**仅追加、不改历史脚本**，保持可回滚（新增列可空或带默认值）。
- 迁移中每张表、每列、每个枚举值必须加**中文注释**（业务含义与取值范围）。
- 表名复数小写下划线；主键 `id BIGSERIAL`；`created_at`/`updated_at` 必备；删除一律软删 `is_active`。
- `.env.example` 每项注明含义与是否部署必填；测试用例标题用中文描述被测行为。

## 文档规范（生成与变更）

- 文档分三档：**活文档**（standards / api / runbook / user-guide / project-brief / AGENTS.md / known-issues——永远代表现状，改代码必须同步对应文档）；**快照**（评审报告/验收状态等时点记录，文件名带日期前缀，写完即冻结，被取代后移入 `docs/archive/`）；**台账**（`.superpowers/sdd/` 与 `.zcode/`，过程流水只增不改，不作为现状依据）——细则见 standards §19.1。
- 单一真相源：每项信息只有一个"家"，他处只放"指针 + 一句话摘要"，禁止两处维护同一份清单或同一组数字（§19.2）。
- 会漂移的数字必须由守账测试背书并注明（写法："端点总数 57，由 `tests/unit/test_api_contract.py` 对账"）；无守账的动态数字不写入正文（§19.3）。
- 文档与注释引用的命令、文件、路径必须真实存在；易腐实现细节（第三方后端名、容量注释）不写死，写"为什么"（§19.5）。
- 同步义务：接口改 → 同步 api.md 标记行；界面文案改 → 同步 user-guide 与 frontend/src/help/；里程碑收口 → 更新本文件"当前状态"节（唯一允许高频改写的段落，其余段落修改走评审）（§19.6）。

## 提交

Conventional Commits：`feat/fix/docs/refactor/test/chore: 简述`；一个提交一件事，提交前 ruff 必须通过。分支：`feat/<模块>-<简述>` / `fix/<问题>`，禁止直推 main。

## 当前状态（2026-11-01）

W1-W5 五个冲刺全部收口，**内测加固完成**。已落地：脚手架与入库管道（W1）、问答全链路（W2）、管理后台与评测（W3）、前端全页面（W4）、内测加固（W5：全局 401 统一/部门口径统一/知识缺口报告/chunk 召回统计/备份恢复基建+演练/用户手册/串库数据包）。**评测集自动更新闭环（F-1019，2026-10-30）：真实拒答/点踩难例采集 → LLM 变体+文档造题双通道 → 扩充集/正式集两级题库（人工升格）→ 周日自动合成流水与三视角跑分（总闸 EVAL_AUTO_ENABLED 缺省关，配置存 app_settings 在线可调）→ 报告 byTier 分层与环比 → 健康卡退化标红**（迁移 0014-0017；工单与验收清单见 `.scratch/eval-auto-loop/issues/`；运行手册 runbook §10）。**商用就绪阶段一（2026-10-31，六角色评审 P0 收口）：F-1019 自动跑分链路修复（triggered_by 消息行归属 + config 合并快照保 perspective）、e2e_seed 公开口令防呆（非隔离库 PILOT_CONFIRM=1 + 显式口令，重跑不重置已改密账号）、[自动] run 部分唯一索引防并发双跑、SSE 断开收尾（GeneratorExit 幽灵占位行消除）、停机补偿（启动 catchup + 合成同周幂等标记 + 评测 run 幽灵清扫，misfire_grace_time 全任务化）、fpdf2 主门禁复绿、前端错误态全站收口（12 视图 + 概览 + 自动设置禁保存防覆写）、运维三件套（备份新鲜度探针/verify-restore.sh 恢复演练脚本/告警链样例）**（工单目录 `.scratch/commercial-readiness/issues/`；评审报告 `docs/2026-10-31-六角色评审报告.md`）。后端 1204+ passed、coverage 95.63%、前端四门全绿（lint/type/326/build，2026-10-31 全量口径）；接口契约见 [docs/api.md](docs/api.md)（**端点总数：74**，头部标记行由 `tests/unit/test_api_contract.py` 从 FastAPI 实际路由对账，改端点必须同步该标记行与文档章节）。**待部署机硬项**：串库真实阈值复测（runbook §5.5）、非功能基线 #7、备份调度实测、golden mineru 回归、合规清单签批——清单见 `docs/archive/acceptance-status-2026-09-30.md` §三。**商用就绪阶段二（2026-10-31）：P2 批量收口（三方镜像 digest 钉版/DB 连接池可配/STATIC_DIR 入 Settings/0014 降级防断/e2e-matrix 对账/eval_synth 红线拆分）、NFR 取证脚本（deploy/nfr-check.py，runbook §10.4）、no_ground 闭环（skip_count 重试上限+入库重扫+管理端重扫，迁移 0018）、对抗用例进正式集（注入防线抽核通过；附带修复导入库内查重缺陷）、UI 二批（表格 max-height/黑话收口/帮助双源守账测试）、两检索一致性边界收紧（生效日期前置闸/按 key 缓存失效语义分级）**。**商用就绪阶段三（2026-11-01，六角色"设计先行→评审放行→TDD 实施→独立复核"全流程）：工单 19 进程内状态外置（五 Protocol 协议化+迁移 0019 三张 state 表+五 PG 实现+StateBundle 装配，STATE_BACKEND 枚举缺省 memory 零变化）、工单 20 调度器独立单副本+gunicorn 多副本（SCHEDULER_ENABLED+advisory lock 抢主恰一 leader、catchup 跟随、compose.scale.yml、entrypoint 自愈 gate）、工单 21 令牌桶分级限流（三层限流第二层，总闸缺省关，429 带 retryAfterSec）、工单 18 尽项侧栏折叠响应式兜底（菜单图标+toggle+min-width，337 前端用例）、工单 22 生效日期前置闸、工单 23 按 key 缓存失效（语义分级）**（设计文档与 QC/PM 放行记录见 `.scratch/commercial-readiness/design/`）。后端 **1321+ passed、coverage 95.47%**、前端四门全绿（lint/type/**337**/build）；接口契约见 [docs/api.md](docs/api.md)（**端点总数：74**，守账同前）。**待部署机硬项**：串库真实阈值复测（runbook §5.5）、非功能基线 #7、备份调度实测、golden mineru 回归、合规清单签批、scale 拓扑部署验证（runbook §11 检查单）、NFR 取证（deploy/nfr-check.py）——清单见 `docs/archive/acceptance-status-2026-09-30.md` §三与 runbook §11。W6=验收评审（七条硬指标）。注意：用户并行会话在途（分块自适应父子块功能），改共享文件前先重读。
