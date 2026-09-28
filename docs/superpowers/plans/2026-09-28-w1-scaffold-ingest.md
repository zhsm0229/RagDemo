# W1 脚手架与语料入库管道 实施计划（TDD）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付可独立验收的 W1 增量——项目骨架（git/CI/配置/数据库/鉴权）+ 语料入库全链路（同步预检→解析→清洗→分块→向量化→事务入库），全部核心逻辑按 TDD 红绿循环开发。

**Architecture:** FastAPI 单体（同步 SQLAlchemy 2.0 + Alembic），五层单向依赖 `api → services → rag → core → db`；六个可替换接口（LLMProvider/VectorStore/Parser/Embedder/Reranker/FileStorage）在装配层绑定；测试用临时 pgvector 容器 + 五套确定性 stub（支持失败注入）；fake clock 注入时间。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.0 / Alembic / pgvector(PostgreSQL 15) / pytest+httpx / python-docx / pypdf / passlib[bcrypt] / APScheduler / ruff / docker compose

## Global Constraints（摘自 standards.md v1.7，逐条适用于每个任务）

- Python 3.11+；ruff lint+format；全部函数带类型注解；单文件 ≤300 行、函数 ≤50 行；禁止裸 `except:`
- SQL 一律参数绑定（SQLAlchemy），禁止拼接/format/f-string 组装 SQL
- 密码 bcrypt；JWT 8h httpOnly SameSite=Lax；失败 5 次锁 15 分钟；首登强制改密
- 涉部门表查询必须带 dept 过滤；删除一律软删；审计字段 created_at/updated_at 必备
- 业务错误统一 `ApiError(code, message, http_status)`，禁止裸抛；响应体 `{code, message, data}`，JSON 键 camelCase
- 外部调用超时：chat 60s / embed 10s / 解析 300s，失败重试 1 次
- 提交遵循 Conventional Commits；测试与实现可同 commit，但测试必须先写且确认失败过
- `rag/`、`services/` 覆盖率 ≥70%（pytest-cov 门禁）
- Alembic 迁移仅追加、不改历史；写库测试必须含读回断言（§15.4）
- 断言精确，禁止 `A or B` 宽松断言（§15.5）
- 模型/镜像固定版本 tag；密钥只走 .env（.gitignore 必须含 .env）

## File Structure（W1 全量新建）

```
app/
  main.py                    # FastAPI 装配：路由、异常处理器、依赖、lifespan
  core/
    config.py                # Settings（pydantic-settings，缺失必需项 fail-fast）
    security.py              # bcrypt + JWT 签发/校验
    errors.py                # ApiError + 全局异常处理器
    clock.py                 # Clock 接口 + SystemClock/FakeClock
    llm.py                   # LLMProvider/Embedder/Reranker 接口 + TEI/云实现 + Stub 实现
    storage.py               # FileStorage 接口 + LocalFileStorage
  db/
    base.py                  # SQLAlchemy Base/Engine/session 工厂
    models.py                # ORM 模型（与 db-design.md DDL 一一对应）
    seed.py                  # 种子：departments/admin 用户
  rag/
    clean.py                 # 纯函数：清洗规则
    chunker.py               # 纯函数：标题层级分块
    precheck.py              # 纯函数：同步预检 + 异步预检（乱码/敏感/simhash）
    parser.py                # Parser 接口 + DocxParser/PdfParser(MinerU→pypdf 降级)
  services/
    auth.py                  # 登录/锁定/改密
    ingest.py                # 入库状态机编排
    audit.py                 # 审计写入
    scheduler.py             # APScheduler：到期下线/分区补建（手动触发入口）
  api/
    auth.py                  # POST /api/auth/login /change-password
    documents.py             # POST /api/documents 上传 + GET 列表 + POST /{id}/retry
alembic/
  versions/0001_init.py      # 全量表（一次建齐 W1 所需）
tests/
  conftest.py                # pg fixture（compose 临时库+迁移）、client fixture、FakeClock、认证辅助
  stubs/                     # 五套 stub（llm/storage 时钟已含）
  unit/                      # 纯函数单测（clean/chunker/precheck/security/clock）
  api/                       # API 冒烟（auth/documents）
  integration/               # ingest 状态机（真 pg）
  fixtures/golden/           # 黄金样本（用户样例到位后移入；缺失时对应用例显式 skip+清单）
mkdocs: Makefile             # install/lint/fmt/test/test-unit/test-api/ci/up/down
docker-compose.yml           # pg(pgvector pg15) + app(后置) + 测试用 pg 测试库
pyproject.toml  .env.example  .gitignore  .github/workflows/ci.yml
```

---

### Task 1: 仓库初始化与工具链

**Files:** Create: `.gitignore`, `pyproject.toml`, `Makefile`, `app/__init__.py`, `app/core/config.py`(骨架), `.env.example`, `docker-compose.yml`
**Interfaces:** Produces: `make install/lint/fmt/test` 可用；`app.core.config.get_settings()`

- [ ] **Step 1: 写失败测试** `tests/unit/test_config.py`

```python
import pytest
from app.core.config import Settings

def test_missing_required_field_raises(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(Exception, match="JWT_SECRET"):
        Settings(_env_file=None)  # 缺必需配置必须 fail-fast（standards §6.4）

def test_defaults_load(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    s = Settings(_env_file=None)
    assert s.database_url.startswith("postgresql")
    assert s.access_token_expire_minutes == 480  # 8h
```

- [ ] **Step 2: 确认失败** `make test` → FAIL（模块不存在）
- [ ] **Step 3: 最小实现** `app/core/config.py`

```python
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://rag:rag@localhost:5433/rag"
    jwt_secret: str  # 必需，缺失拒启
    access_token_expire_minutes: int = 480
    storage_dir: str = "./data/uploads"
    embed_base_url: str = "http://localhost:8081"   # TEI
    embed_timeout_s: int = 10
    mineru_base_url: str = ""                        # 空=pypdf 降级路径
    mineru_timeout_s: int = 300
    test_mode: bool = False

@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`pyproject.toml` 含依赖（fastapi uvicorn sqlalchemy psycopg[binary] alembic passlib[bcrypt] pyjwt python-multipart python-docx pypdf pydantic-settings apscheduler httpx）与 dev 依赖（pytest pytest-cov httpx ruff）；`[tool.ruff] line-length=100`；`[tool.coverage] fail_under=70  include=["app/rag/*","app/services/*"]`。`Makefile`：`install/lint/fmt/test/test-unit/test-api/ci/up/down`。`.gitignore` 含 `.env`、`data/`、`__pycache__/`、`node_modules/`。`docker-compose.yml` 先只放 `pg`（image: pgvector/pgvector:pg15，端口 5433）与 `pg-test`（5434）。

- [ ] **Step 4: 确认通过** `make lint && make test` → PASS
- [ ] **Step 5: 提交** `git init -b main && git add -A && git commit -m "chore: scaffold project with config and toolchain"`

### Task 2: 时钟注入

**Files:** Create: `app/core/clock.py`, `tests/unit/test_clock.py`
**Interfaces:** Produces: `class Clock(Protocol): now() -> datetime`；`FakeClock.set(iso: str)`；全局 `get_clock()`

- [ ] **Step 1: 失败测试**：`FakeClock` 从 "2026-10-01T08:00:00+00:00" 起步，`now()` 返回该值；`advance(minutes=5)` 后返回 +5min；默认 `SystemClock.now()` 带时区且微秒归零
- [ ] **Step 2:** 确认 FAIL　- [ ] **Step 3:** 实现（两个类 + 模块级 `_clock` 可覆盖）　- [ ] **Step 4:** PASS
- [ ] **Step 5:** `git commit -m "feat(core): injectable clock with fake implementation"`

### Task 3: 安全模块（bcrypt + JWT）

**Files:** Create: `app/core/security.py`, `tests/unit/test_security.py`
**Interfaces:** Produces: `hash_password(pw: str) -> str`、`verify_password(pw: str, hashed: str) -> bool`、`create_token(user_id: int, role: str, dept_id: int, expires_min: int, now: datetime, secret: str) -> str`、`decode_token(token: str, secret: str) -> TokenPayload | None`（过期/篡改返回 None）

- [ ] **Step 1: 失败测试**（参数化表，来源 AUTH 用例）

| 输入 | 预期 |
|------|------|
| hash 后 verify 正确密码 | True |
| hash 后 verify 错误密码 | False |
| create→decode 有效 token | payload.user_id/role/dept_id 一致 |
| exp 已过（now+1min 签发，fake_clock 前拨 2min） | decode 返回 None |
| token 首字节篡改 | decode 返回 None |

- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 最小实现（pyjwt HS256，claims: sub/role/dept/exp/iat/jti=uuid4）
- [ ] **Step 4:** PASS　- [ ] **Step 5:** `git commit -m "feat(core): password hashing and jwt"`

### Task 4: 全量数据库迁移 + 种子

**Files:** Create: `app/db/base.py`, `app/db/models.py`, `app/db/seed.py`, `alembic/`（env + `versions/0001_init.py`）, `tests/conftest.py`, `tests/integration/test_migrations.py`
**Interfaces:** Produces: ORM 模型 `Department/User/Document/Chunk/QaPair/Conversation/Message/EvalQuestion/EvalRun/AuditLog`（字段严格按 `specs/db-design.md` DDL，含 messages 月分区+DEFAULT 分区、uq_doc_active、uq_doc_content_hash WHERE deleted_at IS NULL、HNSW 索引、messages_id_seq）；conftest fixture `session`（每测试事务回滚）与 `pg`（session 级：对 5434 执行 alembic upgrade head）

- [ ] **Step 1: 失败测试** `tests/integration/test_migrations.py`

```python
def test_all_tables_exist(session):
    rows = session.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public'")).scalars().all()
    for t in ["departments","users","documents","chunks","qa_pairs",
              "conversations","messages","eval_questions","eval_runs","audit_logs"]:
        assert t in rows

def test_messages_partitioned_and_default(session):
    kind = session.execute(text(
        "SELECT partstrat FROM pg_partitioned_table WHERE partrelid='messages'::regclass")).scalar()
    assert kind == "r"
    assert session.execute(text(
        "SELECT count(*) FROM pg_inherits WHERE inhparent='messages'::regclass")).scalar() >= 1

def test_seed_departments_and_admin(session):
    codes = {r for r in session.execute(text("SELECT code FROM departments")).scalars()}
    assert codes == {"service", "hr", "all"}
    admin = session.execute(text(
        "SELECT username FROM users WHERE role='admin'")).scalar()
    assert admin == "admin"

def test_unique_active_doc_index(session):
    sql = session.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE indexname='uq_doc_active'")).scalar()
    assert "WHERE (is_active)" in sql and "deleted_at IS NULL" not in sql
```

- [ ] **Step 2:** 对空库 FAIL（表不存在）
- [ ] **Step 3:** 模型 + `0001_init.py` 迁移：DDL 逐列照抄 `specs/db-design.md`（枚举用 TEXT+CHECK；messages 三分区语句；`CREATE EXTENSION IF NOT EXISTS vector`；HNSW 两条索引；审计表 PK(id,created_at)）；seed 插入三部门 + admin（bcrypt 哈希的随机初始密码打印一次）
- [ ] **Step 4:** `make test` PASS（含读回断言）
- [ ] **Step 5:** `git commit -m "feat(db): full schema migration with partitions and seed"`

### Task 5: FileStorage 接口（本地卷）

**Files:** Create: `app/core/storage.py`, `tests/unit/test_storage.py`
**Interfaces:** Produces: `StoredFile(NamedTuple): rel_path, sha256, size`；`FileStorage.save(content: bytes, ext: str) -> StoredFile`；`LocalFileStorage(root_dir)`

- [ ] **Step 1: 失败测试**：保存 `b"pdf-bytes"` → 返回 rel_path 以 `.pdf` 结尾且含 UUID（两级目录 `ab/cd/`），sha256 与手算一致，文件真实落盘可读回；同名内容两次保存产生两个不同路径（不覆盖）；`ext` 不带点时自动补
- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现（uuid4 命名，`hashlib.sha256`，`open(...,"xb")` 防覆盖）　- [ ] **Step 4:** PASS
- [ ] **Step 5:** `git commit -m "feat(core): local file storage with sha256"`

### Task 6: 清洗纯函数（rag/clean）

**Files:** Create: `app/rag/clean.py`, `tests/unit/test_clean.py`
**Interfaces:** Produces: `clean_text(raw: str) -> str`（规则链：去页眉页脚重复行、去目录行（`…\d+$`/`\.{3,}\s*\d+$`）、去独立页码行、去水印标记、折叠 3+ 空行、去首尾空白）；`RAW_CHARS = lambda raw: len(raw)` 供红线复用

- [ ] **Step 1: 失败测试**（参数化：每条规则 正/反 两例，输入输出精确断言）

| 输入片段 | 规则 | 预期 |
|---------|------|------|
| `"正文A\n客服_退货政策\n正文B"`（重复出现 ≥3 次的短行=页眉） | 页眉 | 该行删除，正文保留 |
| `"第3章 报销 ......... 12\n正文"` | 目录行 | 目录行删除 |
| `"内容\n12\n内容"` | 独立页码行 | 删除 |
| `"内部资料\n正文"`（水印标记表） | 水印 | 删除 |
| `"a\n\n\n\nb"` | 空行折叠 | `"a\n\nb"` |
| `"　正文　"` | 首尾全角空白 | `"正文"` |

- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现（`RULES: list[Callable[[str], str]]` 顺序执行；重复行判定：出现次数 ≥3 且长度 ≤30）　- [ ] **Step 4:** PASS（覆盖率计 clean.py 100%）
- [ ] **Step 5:** `git commit -m "feat(rag): text cleaning rules"`

### Task 7: 分块纯函数（rag/chunker）

**Files:** Create: `app/rag/chunker.py`, `tests/unit/test_chunker.py`
**Interfaces:** Produces: `@dataclass ParsedDoc: title: str; blocks: list[Block]`、`@dataclass Block: level: int; heading_path: list[str]; text: str`、`split(doc: ParsedDoc, min_len=300, max_len=800) -> list[OutChunk]`、`@dataclass OutChunk: content, title_path, position`

- [ ] **Step 1: 失败测试**（参数化）

| 场景 | 输入 | 预期 |
|------|------|------|
| 标题层级 | `第3章>3.2>正文300字` | 1 块，title_path=`"第3章 > 3.2"` |
| 超长切分 | 单节 1500 字 | ≥2 块，每块 ≤800 字，按句子边界切，均带同 title_path |
| 过短合并 | 两节各 150 字且同级 | 合并为 1 块（≥300） |
| 块前缀 | 任意 | content 以 `f"{title_path}｜"` 开头 |
| 顺序 | 多节 | position 从 0 递增 |

- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现（贪心：按标题聚合→超长按句切→尾块 <min 与前块合并）　- [ ] **Step 4:** PASS
- [ ] **Step 5:** `git commit -m "feat(rag): heading-aware chunker"`

### Task 8: 预检纯函数（rag/precheck）

**Files:** Create: `app/rag/precheck.py`, `tests/unit/test_precheck.py`
**Interfaces:** Produces: `check_sync(filename: str, mime: str, content: bytes) -> SyncResult(ok: bool, reason: str | None, sha256: str)`（白名单扩展名 pdf/docx + MIME + 魔数 `%PDF`/`PK\x03\x04` + ≤50MB + SHA-256）；`garbled_ratio(text) -> float`（替换控制字符与 U+FFFD 占比）；`scan_sensitive(text) -> list[SensitiveHit(kind, page_hint, offset)]`（身份证 18 位含校验位算法 / 手机号 1[3-9]\d{9} / 银行卡 13-19 位 Luhn；正则预编译+`re` 线性模式，禁止嵌套量词）；`simhash64(text) -> int`、`hamming(a: int, b: int) -> int`；`GARBLE_THRESHOLD=0.05`、`SIMHASH_NEAR=3`

- [ ] **Step 1: 失败测试**（参数化，来源 test-cases DOC-03/04/06/08/09、SEC-06/07）

| 输入 | 预期 |
|------|------|
| exe 字节改名 .pdf | ok=False（魔数不符） |
| 51MB bytes | ok=False（超限） |
| 合法 pdf 头两次保存 | sha256 一致且为 64 位 hex |
| `"正常中文"` | garbled_ratio=0.0 |
| `"\ufffdf\ufffdf\ufffd"` 过半 | ratio>0.05 |
| `"11010519491231002X"`（合法校验位） | scan 命中 kind=id_card |
| `"110105194912310021"`（校验位错） | 不命中 |
| `"13812345678"` | 命中 mobile |
| Luhn 合法 16 位 | 命中 bank_card |
| `"a"*100000 + "身份证"` 超长行 | scan 在 1s 内返回（性能断言 timeout） |
| 相近两文本 | hamming(simhash64) ≤3；无关文本 >3 |

- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现（身份证校验位加权算法单独函数 `id_card_valid`；Luhn 单独函数；各带 3 例参数化单测）　- [ ] **Step 4:** PASS
- [ ] **Step 5:** `git commit -m "feat(rag): sync and async prechecks"`

### Task 9: LLM 三件套接口 + 确定性 Stub

**Files:** Create: `app/core/llm.py`, `tests/unit/test_llm_stubs.py`
**Interfaces:** Produces: `Embedder.embed(texts: list[str]) -> list[list[float]]`（dim=1024）；`Reranker.rerank(query, docs: list[str]) -> list[float]`（sigmoid 归一）；`LLMProvider.chat_stream(messages, max_tokens) -> Iterator[str]`；实现类 `StubEmbedder/StubReranker/StubLLMProvider`（**同输入必同输出**；构造参数 `fail_times=0`、`hang_s=0`、`break_after=None` 注入失败）；`cosine_to_similarity(dist: float) -> float`（=1-dist）；`sigmoid(x)`——**口径必须与 standards §4.4 一致**

- [ ] **Step 1: 失败测试**：同文本两次 embed 向量相等且 dim=1024；不同文本向量不等；`fail_times=1` 第一次调用抛 `UpstreamError` 第二次成功；`break_after=2` 产出 2 段后抛 `UpstreamError(broken=True)`；rerank 分数全在 (0,1) 且按注入权重保序；cosine_to_similarity(0.2)=0.8
- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现（StubEmbedder 用 sha256(text) 播种 random 生成 1024 维归一化向量；TEI/云真实现放 `tei.py`/`openai_compat.py` 骨架抛 NotImplemented，W2 激活）　- [ ] **Step 4:** PASS
- [ ] **Step 5:** `git commit -m "feat(core): llm interfaces with deterministic stubs"`

### Task 10: 解析器（docx + pdf 降级）

**Files:** Create: `app/rag/parser.py`, `tests/unit/test_parser.py`, `tests/fixtures/samples/`（本任务生成 3 个程序化样本：`make_sample_docx.py`、`make_sample_pdf.py` 入 `tests/fixtures/tools/`）
**Interfaces:** Produces: `Parser.parse(filename: str, content: bytes) -> ParsedDoc`；`DocxParser`（python-docx：段落+标题级别）、`PdfFallbackParser`（pypdf，页文本拼接，engine="pypdf"）；`MinerUParser(base_url, timeout_s=300)`（HTTP 调用，失败重试 1 次后抛 `UpstreamError`）；`get_parser(filename, settings) -> Parser`（pdf 且配置了 mineru → MinerU，否则降级并标 degraded）

- [ ] **Step 1: 失败测试**：程序化生成 docx（含 Heading1/2 + 段落）→ parse 出 blocks 层级正确；生成含文本 pdf → pypdf 解出文本、engine="pypdf"；MinerU stub（httpx MockTransport 返回 500 两次）→ 抛 UpstreamError（重试 1 次后）；`.doc` 抛 `ApiError(1001, "请另存为 docx", 422)`
- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现　- [ ] **Step 4:** PASS
- [ ] **Step 5:** `git commit -m "feat(rag): docx/pdf parsers with mineru client and fallback"`

### Task 11: 审计服务

**Files:** Create: `app/services/audit.py`, `tests/integration/test_audit.py`
**Interfaces:** Produces: `record_audit(session, actor_user_id, action, object_type, object_id, detail: dict, ip: str | None, clock) -> None`（commit 由调用方控制）

- [ ] **Step 1: 失败测试**：写入一条 → **读回断言** actor/action/object_type/object_id/detail 逐字段一致（§15.4）；detail 为 JSONB 可 `json.loads`
- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现　- [ ] **Step 4:** PASS　- [ ] **Step 5:** `git commit -m "feat(services): audit trail"`

### Task 12: 鉴权服务与 API

**Files:** Create: `app/services/auth.py`, `app/api/auth.py`, `app/core/errors.py`, `app/main.py`(装配), `tests/api/test_auth.py`; Modify: `app/db/models.py`(如需 User 辅助方法)
**Interfaces:** Produces: `POST /api/auth/login {username,password}` → Set-Cookie(jwt, httpOnly, SameSite=Lax) 或 `ApiError(2001,...)`；`POST /api/auth/change-password {old,new}`（首登强制）；`get_current_user` 依赖（Cookie→decode→查 users.is_active）；`require_admin` 依赖；错误码：2001 凭证错误、2002 锁定、2003 未登录、2003x 权限

- [ ] **Step 1: 失败测试**（httpx 对 app，来源 AUTH-01~09）

| 用例 | 断言 |
|------|------|
| 正确登录 | 200 + Set-Cookie httpOnly；body code=0 |
| 错 4 次 | 每次提示剩余次数（message 含 "3"/"2"/"1"/"0"） |
| 错 5 次 | 2002 锁定，正确密码也 2002；users.locked_until 非空（读回）；audit 有记录 |
| 成功登录清零 | 失败 3→成功→失败 1：不锁定 |
| 首登改密 | must_change 用户登录后调业务接口 403/提示改密；改 <8 位 422；改后正常 |
| 禁用 | 禁用后带旧 cookie 请求 → 401 |
| 无 cookie | 401 |
| member 访问 admin 接口 | 403 |

- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现（服务层锁定逻辑纯函数 `evaluate_login(attempts, max=5) -> Action` 便于 TDD；限流=进程内滑动窗口 dict，测试直接注入）
- [ ] **Step 4:** PASS（`make test-api`）　- [ ] **Step 5:** `git commit -m "feat(api): login with lockout and forced password change"`

### Task 13: 入库状态机（services/ingest）

**Files:** Create: `app/services/ingest.py`, `tests/integration/test_ingest.py`
**Interfaces:** Consumes: Parser/Embedder(storage)/precheck/clean/chunker/clock/audit。Produces: `create_document(session, storage, uploader, meta, content, filename, mime) -> Document`（同步段：check_sync→重复 409/落盘/登记 parsing/审计）；`process_document(session, deps, doc_id) -> None`（异步段编排：parse→红线→clean→乱码/敏感/simhash→分块→批量 embed→事务写 chunks+旧版归档→ready；任何一步失败→failed+中文原因；重试先删本文档旧块）；`confirm_document(session, doc_id)`（待确认激活）；`expire_due_documents(session, clock)`（F-1004）

- [ ] **Step 1: 失败测试**（真 pg + StubEmbedder + fake clock，来源 DOC-01/06/10/11/12/13/20/26 + DOC-06 并发）

| 用例 | 断言 |
|------|------|
| 正常 docx 入库 | status=ready；chunks 数=分块器输出（读回）；chunks.content 带 title_path 前缀；审计两条（upload/ready） |
| 完全重复 | create_document 抛 409 ApiError 指向已有 id；库内文档数不变 |
| 软删后重传 | 旧文档 deleted_at 置位后同内容 create 成功（部分索引语义） |
| 扫描件红线 | 空文本 docx→failed 原因含"疑似扫描件或空文档"；重试仍 failed |
| 降级 | mineru_base_url="" 时 pdf 走 pypdf，documents.parse_engine="pypdf" |
| 版本切换 | 同标题第二版 ready 后：v1 is_active=False 且 deleted_at IS NULL；检索过滤（is_active）只命中 v2 |
| 待确认 | stub 敏感命中（注入含身份证文本）→ 文档 is_active=False；confirm 后 True 可检索 |
| 到期下线 | expiry_date<clock.now() 的文档执行任务后 is_active=False + 审计 |
| 失败重试幂等 | embed stub fail_times=1→failed；重试→ready；chunks 无重复（先删后插） |
| 并发版本 | 两线程同标题提交（SQLite 内存锁模拟串行化断言）→ 版本号 1、2 无冲突 |

- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现（编排函数 ≤50 行/个；写 chunks 与旧版归档同一事务）　- [ ] **Step 4:** PASS（`make test` 全绿）
- [ ] **Step 5:** `git commit -m "feat(services): ingest state machine with prechecks and versioning"`

### Task 14: 上传与文档 API

**Files:** Create: `app/api/documents.py`, `tests/api/test_documents.py`; Modify: `app/main.py`（注册路由）
**Interfaces:** Produces: `POST /api/documents`（multipart file + 表单 title/dept/owner/effectiveDate/expiryDate?/complianceConfirmed）→ 202 `{id,status}`；`GET /api/documents?dept&status&keyword&page&pageSize` → `{items,total,page,pageSize}`（camelCase）；`POST /api/documents/{id}/retry`；`POST /api/documents/{id}/confirm`

- [ ] **Step 1: 失败测试**（来源 DOC-02/04/05/15/16 + §22.1）：.doc 拒收 422 中文原因；51MB 拒；缺元数据 422 字段级；未勾合规 422；member 调用 403；成功 202 后（直接调 process_document）GET 列表出现 ready；响应键名全 camelCase（断言 `"effectiveDate" in item`）
- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现（BackgroundTasks 调 process_document；测试模式 test_mode=True 时同步执行便于断言）
- [ ] **Step 4:** PASS　- [ ] **Step 5:** `git commit -m "feat(api): document upload with sync precheck"`

### Task 15: 定时任务（scheduler）

**Files:** Create: `app/services/scheduler.py`, `tests/integration/test_scheduler.py`; Modify: `app/main.py`（lifespan 启动，`settings.test_mode` 不自启）
**Interfaces:** Produces: `run_due_sweeps(session, clock)`（到期下线，手动入口=测试与 runbook 用）；`ensure_month_partitions(n=3)`；APScheduler cron 每日 02:00 / 每月 1 日

- [ ] **Step 1: 失败测试**：FakeClock 拨到到期日后调用 `run_due_sweeps` → 文档 is_active=False + 审计（DOC-20 的可自动化实现）；分区任务对未来 3 个月幂等（重复执行 0 新增）
- [ ] **Step 2:** FAIL　- [ ] **Step 3:** 实现　- [ ] **Step 4:** PASS　- [ ] **Step 5:** `git commit -m "feat(services): scheduler for expiry and partitions"`

### Task 16: CI 最小流水线

**Files:** Create: `.github/workflows/ci.yml`（公司为 GitLab 时同结构转 `.gitlab-ci.yml`，任务内注明）
**Steps:** 无 TDD（基础设施，验收=流水线绿）：五个 job 按 §20.2——lint(ruff) / type(mypy 宽松限 rag+services+core) / test(pytest，services: pgvector:pg15 容器 + `ALEMBIC upgrade head` + 覆盖率 ≥70% fail_under) / security(pip-audit + gitleaks) / build(docker build app)。`make ci` 本地等价执行 lint+test。提交 `ci: minimal pipeline per standards §20`。

### Task 17: 黄金样本回归

**Files:** Create: `tests/integration/test_golden.py`, `tests/fixtures/golden/README.md`
**Steps:** pytest 参数化读 `golden/manifest.json`（{file, expect_engine, expect_min_chars, expect_blocks≥1, expect_fail_reason?}）；样本文件缺失时 `pytest.mark.skip(reason="待用户提供样例：specs/待办")` **并在输出汇总打印缺失清单**（§15.1 禁静默跳过）；每样本断言：解析引擎=预期、清洗后字符数 ≥ 红线、分块 ≥1、（乱码样本）failed。用户样例到位后由对接人删除 skip。提交 `test(golden): golden sample regression harness`。

---

## W1 完成定义（对照 design §10 验收）

1. `make ci` 全绿（lint+type+test+coverage≥70%+security）
2. F1 流程走通：上传合规 docx → ready → chunks 读回正确（DOC-01）
3. 黄金样本规整类通过（其余三类等样例到位）
4. 两轮评审 P1 修复项涉及 W1 的全部落地：两段式预检、SHA-256、部分唯一索引、待确认态、时钟注入

## W2-W6 计划拆分（各自里程碑边界另出计划）

- **Plan 2（W2）问答链路**：VectorStore(pgvector) 实现、检索编排（问答对匹配/折叠/重排/拒答阈值）、prompt 组装（§17）、SSE API（§3.6 DTO）、会话/messages 落库
- **Plan 3（W3）知识库与治理界面 + RAGFlow 基线**：文档/问答对管理 API+页面、解析后异步预检接线、到期/健康度、RAGFlow 对照跑分
- **Plan 4（W4-W5）管理后台与评测**：用户/记录/badcase/评测/审计页面、评测跑分器、zip 批量、阈值扫描、HR 接入与串库
