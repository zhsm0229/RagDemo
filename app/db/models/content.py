"""内容域模型：documents / chunks / qa_pairs（拆分自原 app/db/models.py，声明逐字保留）。"""

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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import Vector, _ts


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="documents_dept_id_fkey"),
        ForeignKeyConstraint(["uploaded_by"], ["users.id"], name="documents_uploaded_by_fkey"),
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
        ForeignKeyConstraint(["document_id"], ["documents.id"], name="chunks_document_id_fkey"),
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="chunks_dept_id_fkey"),
        ForeignKeyConstraint(
            ["dup_of"], ["chunks.id"], ondelete="SET NULL", name="chunks_dup_of_fkey"
        ),
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
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="qa_pairs_dept_id_fkey"),
        ForeignKeyConstraint(["created_by"], ["users.id"], name="qa_pairs_created_by_fkey"),
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
