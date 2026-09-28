"""ORM 模型：与 specs/db-design.md v1.0 的 DDL 逐列映射（枚举一律 TEXT+CHECK）。

建表唯一入口是 Alembic 迁移（alembic/versions/0001_init.py，DDL 逐字照抄 spec）；
本文件中的 Constraint / Index 声明用于文档化与未来 autogenerate 对齐，不参与建表。
vector 列用 UserDefinedType 声明（不引入 pgvector-python 依赖，embedding 的
Python 侧读写适配留给检索任务）；messages 为声明式分区父表，ORM 直接对父表读写，
行路由由 PostgreSQL 完成。
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from app.db.base import Base


class Vector(UserDefinedType):
    """pgvector 的 vector(n) 列类型（仅 DDL 声明与映射用）。"""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self) -> str:
        return f"vector({self.dim})"


def _ts() -> DateTime:
    return DateTime(timezone=True)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(Text, unique=True)  # 部门编码：service/hr/all
    name: Mapped[str] = mapped_column(Text)  # 中文名：客服/人力/全员
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','member')", name="users_role_check"),
        Index("idx_users_dept", "dept_id", postgresql_where=text("is_active")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, unique=True)  # 3-32 位字母数字
    password_hash: Mapped[str] = mapped_column(Text)  # bcrypt
    dept_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(Text, server_default=text("'member'"), name="role")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    must_change_password: Mapped[bool] = mapped_column(  # 首登强制改密
        Boolean, server_default=text("true")
    )
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(_ts())
    last_login_at: Mapped[datetime | None] = mapped_column(_ts())
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','parsing','ready','failed')",
            name="documents_status_check",
        ),
        UniqueConstraint("dept_id", "title", "version", name="uq_doc_title_dept_ver"),
        # 单一生效版本数据库级强制：同部门同主题仅一个 is_active 版本（§11.6）
        Index("uq_doc_active", "dept_id", "title", unique=True, postgresql_where=text("is_active")),
        # 精确查重（F-1001）：软删不占 hash，可重传
        Index(
            "uq_doc_content_hash",
            "content_hash",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_documents_list", "dept_id", "is_active", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text)  # 规范命名：部门_主题_版本_日期
    dept_id: Mapped[int] = mapped_column(BigInteger)
    original_filename: Mapped[str] = mapped_column(Text)  # 原始文件名仅存库
    file_path: Mapped[str] = mapped_column(Text)  # UUID 落盘路径
    mime: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(Text)  # SHA-256，精确查重（F-1001）
    simhash: Mapped[int | None] = mapped_column(BigInteger)  # 近似查重（F-1002）
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    effective_date: Mapped[date] = mapped_column()  # 元数据必填
    expiry_date: Mapped[date | None] = mapped_column()  # 到期自动下线（F-1004）
    owner_name: Mapped[str] = mapped_column(Text)  # 责任人必填
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    parse_engine: Mapped[str | None] = mapped_column(Text)
    parse_error: Mapped[str | None] = mapped_column(Text)  # 失败原因（中文）
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    deleted_at: Mapped[datetime | None] = mapped_column(_ts())  # 软删时间
    uploaded_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "idx_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_where=text("is_active"),
        ),
        Index("idx_chunks_dept", "dept_id", postgresql_where=text("is_active")),
        Index("idx_chunks_doc", "document_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(BigInteger)
    dept_id: Mapped[int] = mapped_column(BigInteger)  # 冗余隔离键，检索免 JOIN
    title_path: Mapped[str] = mapped_column(Text)  # 标题路径前缀
    content: Mapped[str] = mapped_column(Text)  # 300-800 字
    content_chars: Mapped[int] = mapped_column(Integer)
    page_no: Mapped[int | None] = mapped_column(Integer)  # 引用定位用
    position: Mapped[int] = mapped_column(Integer)  # 块序
    embedding: Mapped[Any] = mapped_column(Vector(1024))  # BGE-M3
    embedding_model: Mapped[str] = mapped_column(Text, server_default=text("'bge-m3'"))
    hit_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # F-1011
    last_hit_at: Mapped[datetime | None] = mapped_column(_ts())
    dup_of: Mapped[int | None] = mapped_column(BigInteger)  # 近似重复批注（F-1003）
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class QaPair(Base):
    __tablename__ = "qa_pairs"
    __table_args__ = (
        CheckConstraint("length(question) BETWEEN 1 AND 2000", name="qa_pairs_question_check"),
        CheckConstraint("length(answer) BETWEEN 1 AND 10000", name="qa_pairs_answer_check"),
        CheckConstraint("status IN ('enabled','disabled')", name="qa_pairs_status_check"),
        Index(
            "idx_qa_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("status = 'enabled'"),
        ),
        Index("idx_qa_dept", "dept_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dept_id: Mapped[int] = mapped_column(BigInteger)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(1024))  # 问题向量化
    embedding_model: Mapped[str] = mapped_column(Text, server_default=text("'bge-m3'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'enabled'"))
    batch_no: Mapped[str | None] = mapped_column(Text)  # 导入批次
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index(
            "idx_conv_user", "user_id", text("updated_at DESC"), postgresql_where=text("is_active")
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    dept_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(Text, server_default=text("'新会话'"))  # 取首问截断
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))  # 软删
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class Message(Base):
    """messages：声明式按月分区父表（PARTITION BY RANGE (created_at)）。

    分区键必须入主键 → PK(id, created_at)；id 全局唯一由显式 sequence 保证
    （PG<17 分区表不支持 identity 列）。ORM 直接对父表读写。
    """

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="messages_role_check"),
        CheckConstraint(
            "feedback IN ('up','down') OR feedback IS NULL", name="messages_feedback_check"
        ),
        CheckConstraint(
            "badcase_status IN ('待处理','已修语料','已忽略') OR badcase_status IS NULL",
            name="messages_badcase_status_check",
        ),
        CheckConstraint(
            "answer_type IN ('rag','qa','refuse') OR answer_type IS NULL",
            name="messages_answer_type_check",
        ),
        ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_msg_conv"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_msg_user"),
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="fk_msg_dept"),
        Index("idx_msg_conv", "conversation_id", "created_at"),
        Index("idx_msg_dept_time", "dept_id", text("created_at DESC")),
        Index(
            "idx_msg_feedback",
            "dept_id",
            text("created_at DESC"),
            postgresql_where=text("feedback = 'down'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, server_default=text("nextval('messages_id_seq')")
    )
    conversation_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    dept_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[Any | None] = mapped_column(JSONB)  # [{n, refType, refId, ...}]
    feedback: Mapped[str | None] = mapped_column(Text)  # 'up'/'down'
    feedback_comment: Mapped[str | None] = mapped_column(Text)
    feedback_category: Mapped[str | None] = mapped_column(Text)
    badcase_status: Mapped[str | None] = mapped_column(Text)  # F-702 处理状态
    is_eval: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))  # F-5
    answer_type: Mapped[str | None] = mapped_column(Text)  # 'rag'/'qa'/'refuse'
    model: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))  # F-209
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class EvalQuestion(Base):
    __tablename__ = "eval_questions"
    __table_args__ = (
        CheckConstraint(
            "qtype IN ('factual','multi_hop','cross_reject')", name="eval_questions_qtype_check"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dept_id: Mapped[int] = mapped_column(BigInteger)
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)  # 期望出处
    qtype: Mapped[str] = mapped_column(Text, server_default=text("'factual'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','done','failed')", name="eval_runs_status_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text)  # 如"W4 客服上线前回归"
    config: Mapped[Any] = mapped_column(JSONB)  # 阈值快照，防跨跑对比被污染
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    total: Mapped[int] = mapped_column(Integer)
    correct: Mapped[int] = mapped_column(Integer)
    accuracy: Mapped[Any] = mapped_column(Numeric(5, 4))
    detail: Mapped[Any | None] = mapped_column(JSONB)  # 每题判定与引用快照
    triggered_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class AuditLog(Base):
    """audit_logs：只增不改，无 updated_at；PK(id, created_at) 为按月分区前置。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_time", text("created_at DESC")),
        Index("idx_audit_actor", "actor_user_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text)  # user.create / document.upload / ...
    object_type: Mapped[str] = mapped_column(Text)  # user/document/qa_pair/eval/message
    object_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[Any] = mapped_column(JSONB)  # 前后值快照
    ip: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), primary_key=True
    )
