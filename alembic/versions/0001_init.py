"""0001: MVP 全量表结构（specs/db-design.md v1.0 DDL 逐列照抄）

10 张业务表 + messages 按月分区（DEFAULT + 示例月分区）；枚举一律 TEXT+CHECK；
软删/审计字段齐全；vector 扩展在迁移首步启用。迁移仅追加：后续变更一律新增
revision，禁止改写本文件。

Revision ID: 0001
Revises:
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 升序 DDL：顺序即依赖顺序（departments → users → documents → chunks → ...）
_UPGRADE_DDL: tuple[str, ...] = (
    # 迁移首步：pgvector 扩展（chunks/qa_pairs 的 vector(1024) 依赖）
    "CREATE EXTENSION IF NOT EXISTS vector;",
    # 1. departments 部门
    """CREATE TABLE departments (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,            -- 部门编码：service/hr/all
    name        TEXT NOT NULL,                   -- 中文名：客服/人力/全员
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);""",
    # 2. users 用户
    """CREATE TABLE users (
    id                   BIGSERIAL PRIMARY KEY,
    username             TEXT NOT NULL UNIQUE,           -- 3-32 位字母数字
    password_hash        TEXT NOT NULL,                  -- bcrypt
    dept_id              BIGINT NOT NULL REFERENCES departments(id),
    role                 TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin','member')),
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    must_change_password BOOLEAN NOT NULL DEFAULT TRUE,  -- 首登强制改密
    failed_attempts      INT NOT NULL DEFAULT 0,
    locked_until         TIMESTAMPTZ,
    last_login_at        TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_dept ON users(dept_id) WHERE is_active;""",
    # 3. documents 文档
    """CREATE TABLE documents (
    id                BIGSERIAL PRIMARY KEY,
    title             TEXT NOT NULL,                 -- 规范命名：部门_主题_版本_日期
    dept_id           BIGINT NOT NULL REFERENCES departments(id),
    original_filename TEXT NOT NULL,                 -- 原始文件名仅存库
    file_path         TEXT NOT NULL,                 -- UUID 落盘路径（FileStorage 接口）
    mime              TEXT NOT NULL,
    size_bytes        BIGINT NOT NULL,
    content_hash      TEXT NOT NULL,                 -- SHA-256，精确查重（F-1001）
    simhash           BIGINT,                        -- 文档级近似查重（F-1002），解析后写入
    version           INT NOT NULL DEFAULT 1,
    effective_date    DATE NOT NULL,                 -- 元数据必填
    expiry_date       DATE,                          -- 可选：到期自动下线（F-1004）
    owner_name        TEXT NOT NULL,                 -- 责任人必填
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','parsing','ready','failed')),
    parse_engine      TEXT,                          -- mineru / pypdf / python-docx / pypdf-degraded
    parse_error       TEXT,                          -- 失败原因（中文）
    is_active         BOOLEAN NOT NULL DEFAULT TRUE, -- 生效版本；新版本上传旧版置 false
    deleted_at        TIMESTAMPTZ,                   -- 软删时间（区别于版本归档）
    uploaded_by       BIGINT NOT NULL REFERENCES users(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_doc_title_dept_ver UNIQUE (dept_id, title, version)
);
-- 单一生效版本数据库级强制（standards §11.6）：同部门同主题仅一个 is_active 版本
CREATE UNIQUE INDEX uq_doc_active ON documents(dept_id, title) WHERE is_active;
-- 精确查重（F-1001）：同内容仅一份；软删（deleted_at 非空）不占 hash，可重传
CREATE UNIQUE INDEX uq_doc_content_hash ON documents(content_hash) WHERE deleted_at IS NULL;
CREATE INDEX idx_documents_list ON documents(dept_id, is_active, created_at DESC);""",
    # 4. chunks 向量块
    """CREATE TABLE chunks (
    id              BIGSERIAL PRIMARY KEY,
    document_id     BIGINT NOT NULL REFERENCES documents(id),
    dept_id         BIGINT NOT NULL REFERENCES departments(id), -- 冗余隔离键，检索免 JOIN
    title_path      TEXT NOT NULL,                 -- 标题路径前缀
    content         TEXT NOT NULL,                 -- 300-800 字
    content_chars   INT NOT NULL,
    page_no         INT,                           -- 引用定位用
    position        INT NOT NULL,                  -- 块序
    embedding       vector(1024) NOT NULL,         -- BGE-M3
    embedding_model TEXT NOT NULL DEFAULT 'bge-m3',
    hit_count       INT NOT NULL DEFAULT 0,        -- 召回统计（F-1011）：批量原子累加
    last_hit_at     TIMESTAMPTZ,
    dup_of          BIGINT REFERENCES chunks(id) ON DELETE SET NULL,  -- 近似重复批注（F-1003）
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chunks_embedding ON chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)
    WHERE is_active;
CREATE INDEX idx_chunks_dept ON chunks(dept_id) WHERE is_active;
CREATE INDEX idx_chunks_doc ON chunks(document_id);""",
    # 5. qa_pairs 问答对
    """CREATE TABLE qa_pairs (
    id              BIGSERIAL PRIMARY KEY,
    dept_id         BIGINT NOT NULL REFERENCES departments(id),
    question        TEXT NOT NULL CHECK (length(question) BETWEEN 1 AND 2000),
    answer          TEXT NOT NULL CHECK (length(answer) BETWEEN 1 AND 10000),
    embedding       vector(1024) NOT NULL,         -- 问题向量化
    embedding_model TEXT NOT NULL DEFAULT 'bge-m3', -- 模型升级重建时按此列判别
    status          TEXT NOT NULL DEFAULT 'enabled' CHECK (status IN ('enabled','disabled')),
    batch_no        TEXT,                          -- 导入批次
    created_by      BIGINT NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_qa_embedding ON qa_pairs
    USING hnsw (embedding vector_cosine_ops) WHERE status = 'enabled';
CREATE INDEX idx_qa_dept ON qa_pairs(dept_id, status);""",
    # 6. conversations 会话
    """CREATE TABLE conversations (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    dept_id     BIGINT NOT NULL REFERENCES departments(id),
    title       TEXT NOT NULL DEFAULT '新会话',     -- 取首问截断
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,     -- 软删
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_conv_user ON conversations(user_id, updated_at DESC) WHERE is_active;""",
    # 7. messages 消息（100x 规格：声明式按月分区，MVP 即建成）
    # 分区父表主键用显式 sequence 发号（PG<17 分区表不支持 identity 列）
    "CREATE SEQUENCE messages_id_seq;",
    """CREATE TABLE messages (
    id                BIGINT NOT NULL DEFAULT nextval('messages_id_seq'),
    conversation_id   BIGINT NOT NULL,
    user_id           BIGINT NOT NULL,
    dept_id           BIGINT NOT NULL,
    role              TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content           TEXT NOT NULL,
    citations         JSONB,                       -- [{n, refType:'doc'|'qa', refId, title, page, titlePath, score}]
    feedback          TEXT CHECK (feedback IN ('up','down') OR feedback IS NULL),
    feedback_comment  TEXT,
    feedback_category TEXT,                        -- 答案错误/引用无关/拒答不当/其他
    badcase_status    TEXT CHECK (badcase_status IN ('待处理','已修语料','已忽略') OR badcase_status IS NULL),  -- F-702 处理状态落库（spec 原稿右括号笔误，OR 移入 CHECK 括号内）
    is_eval           BOOLEAN NOT NULL DEFAULT FALSE,  -- 评测链路标记（F-5）
    answer_type       TEXT CHECK (answer_type IN ('rag','qa','refuse') OR answer_type IS NULL),  -- 拒答率/命中率口径
    model             TEXT,
    prompt_tokens     INT NOT NULL DEFAULT 0,      -- F-209 成本地基
    completion_tokens INT NOT NULL DEFAULT 0,
    latency_ms        INT,
    cache_hit         BOOLEAN NOT NULL DEFAULT FALSE,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at),                  -- 分区键必须入主键；id 全局唯一由 sequence 保证
    CONSTRAINT fk_msg_conv FOREIGN KEY (conversation_id) REFERENCES conversations(id),
    CONSTRAINT fk_msg_user FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_msg_dept FOREIGN KEY (dept_id) REFERENCES departments(id)
) PARTITION BY RANGE (created_at);""",
    # 追加 DEFAULT 分区兜底：补建失败时写入不致全线失败，但触发告警人工处理
    "CREATE TABLE messages_default PARTITION OF messages DEFAULT;",
    # 示例月分区；后续月份由 ensure_month_partitions()（启动与月度任务）补建未来 3 个月
    """CREATE TABLE messages_2026_10 PARTITION OF messages
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');""",
    "CREATE INDEX idx_msg_conv ON messages(conversation_id, created_at);",
    "CREATE INDEX idx_msg_dept_time ON messages(dept_id, created_at DESC);",
    "CREATE INDEX idx_msg_feedback ON messages(dept_id, created_at DESC) WHERE feedback = 'down';",
    # 8. eval_questions 评测题
    """CREATE TABLE eval_questions (
    id              BIGSERIAL PRIMARY KEY,
    dept_id         BIGINT NOT NULL REFERENCES departments(id),
    question        TEXT NOT NULL,
    expected_answer TEXT NOT NULL,
    source_ref      TEXT,                          -- 期望出处（文档/问答对）
    qtype           TEXT NOT NULL DEFAULT 'factual'
                    CHECK (qtype IN ('factual','multi_hop','cross_reject')), -- 串库拒答类
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);""",
    # 9. eval_runs 跑分记录
    """CREATE TABLE eval_runs (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,                   -- 如"W4 客服上线前回归"
    config        JSONB NOT NULL,                  -- {model, promptVersion, ...} 阈值快照
    status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','done','failed')),  -- 崩溃后可判态
    total         INT NOT NULL,
    correct       INT NOT NULL,
    accuracy      NUMERIC(5,4) NOT NULL,
    detail        JSONB,                           -- 每题判定与引用快照
    triggered_by  BIGINT NOT NULL REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);""",
    # 10. audit_logs 审计（只增不改，无 updated_at）
    """CREATE TABLE audit_logs (
    id            BIGSERIAL NOT NULL,
    actor_user_id BIGINT NOT NULL REFERENCES users(id),
    action        TEXT NOT NULL,        -- user.create / document.upload / qa.import / eval.run / ...
    object_type   TEXT NOT NULL,        -- user/document/qa_pair/eval/message
    object_id     BIGINT,
    detail        JSONB NOT NULL,       -- 前后值快照
    ip            TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)        -- 100x 按月分区的前置条件：分区键必须入主键
);
CREATE INDEX idx_audit_time ON audit_logs(created_at DESC);
CREATE INDEX idx_audit_actor ON audit_logs(actor_user_id, created_at DESC);""",
)

# 降序回滚：严格逆序，分区父表连带其分区；messages_id_seq 在 messages 之后删
_DOWNGRADE_DDL: tuple[str, ...] = (
    "DROP INDEX IF EXISTS idx_audit_actor;",
    "DROP INDEX IF EXISTS idx_audit_time;",
    "DROP TABLE IF EXISTS audit_logs;",
    "DROP TABLE IF EXISTS eval_runs;",
    "DROP TABLE IF EXISTS eval_questions;",
    "DROP INDEX IF EXISTS idx_msg_feedback;",
    "DROP INDEX IF EXISTS idx_msg_dept_time;",
    "DROP INDEX IF EXISTS idx_msg_conv;",
    "DROP TABLE IF EXISTS messages CASCADE;",  # 连带 messages_default / messages_2026_10
    "DROP SEQUENCE IF EXISTS messages_id_seq;",
    "DROP TABLE IF EXISTS conversations;",
    "DROP INDEX IF EXISTS idx_qa_dept;",
    "DROP INDEX IF EXISTS idx_qa_embedding;",
    "DROP TABLE IF EXISTS qa_pairs;",
    "DROP INDEX IF EXISTS idx_chunks_doc;",
    "DROP INDEX IF EXISTS idx_chunks_dept;",
    "DROP INDEX IF EXISTS idx_chunks_embedding;",
    "DROP TABLE IF EXISTS chunks;",
    "DROP INDEX IF EXISTS idx_documents_list;",
    "DROP INDEX IF EXISTS uq_doc_content_hash;",
    "DROP INDEX IF EXISTS uq_doc_active;",
    "DROP TABLE IF EXISTS documents;",
    "DROP INDEX IF EXISTS idx_users_dept;",
    "DROP TABLE IF EXISTS users;",
    "DROP TABLE IF EXISTS departments;",
    "DROP EXTENSION IF EXISTS vector;",
)


def upgrade() -> None:
    for ddl in _UPGRADE_DDL:
        op.execute(sa.text(ddl))


def downgrade() -> None:
    for ddl in _DOWNGRADE_DDL:
        op.execute(sa.text(ddl))
