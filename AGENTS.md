# AGENTS.md

面向 AI 编码代理（与新人）的项目入口说明。**规范基线是 [docs/standards.md](docs/standards.md)（v1.7，评审基线，违反 = 不合入）**；本文件只汇总日常高频约定，未覆盖之处以 standards.md 及 [docs/specs/](docs/specs/) 设计文档为准，两者冲突时也不是"二选一"——先在评审中指出矛盾，不许静默择一。

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
7. 提交前自查：参数绑定 ✓ 密钥不泄漏 ✓ 输入校验 ✓ LLM 安全约束 ✓ 审计留痕 ✓ 错误脱敏 ✓（§23.2）。

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

## 提交

Conventional Commits：`feat/fix/docs/refactor/test/chore: 简述`；一个提交一件事，提交前 ruff 必须通过。分支：`feat/<模块>-<简述>` / `fix/<问题>`，禁止直推 main。

## 当前状态（2026-09-28）

评审门禁 9 包件已全部通过，进入 TDD 实施阶段。已落地：`app/core`（config / clock 可注入时钟 / security bcrypt+JWT）、`app/db`（base / models / seed）、Alembic `0001_init` 及迁移集成测试。`api` / `services` / `rag` 层尚未开始。里程碑与周排期见 [docs/project-brief.md](docs/project-brief.md) 第七节。
