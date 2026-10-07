"""内容域模型：documents / chunks / qa_pairs（拆分自原 app/db/models.py，声明逐字保留）。"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
        CheckConstraint(
            "source_type IN ('upload','draft')",
            name="documents_source_type_check",
        ),
        CheckConstraint(
            "review_status IN ('待审核','已通过','已驳回')",
            name="documents_review_status_check",
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
    parse_error: Mapped[str | None] = mapped_column(Text)  # 失败原因（中文，用户可见）
    parse_stage: Mapped[str | None] = mapped_column(
        Text, comment="解析当前阶段文案（排队中/解析中/分块中/向量化中），仅 parsing 期有值"
    )
    parse_progress: Mapped[int | None] = mapped_column(
        Integer, comment="解析真实进度 0-100（向量化阶段按嵌入批次推进），收口置空"
    )
    parse_error_detail: Mapped[str | None] = mapped_column(
        Text, comment="技术错误摘要（异常类型+消息+末栈帧），管理端定位用，与用户文案双轨"
    )
    # 以下三列 comment 与 0007 迁移逐字一致（autogenerate 零差异守卫依赖，同 qa_pairs.is_active 先例）
    source_type: Mapped[str] = mapped_column(
        Text,
        server_default=text("'upload'"),
        comment="来源=上传|Agent 生成草稿（upload|draft）",
    )
    review_status: Mapped[str | None] = mapped_column(
        Text,
        comment="草稿审核状态（待审核/已通过/已驳回），仅 source_type='draft' 有值",
    )
    reject_reason: Mapped[str | None] = mapped_column(
        Text,
        comment="驳回原因（中文，审核人填写），仅 review_status='已驳回' 有值",
    )
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
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("true"),
        # 与 0002 迁移注释逐字一致（autogenerate 零差异守卫依赖）
        comment="软删位：删除问答对置 false，退出检索与列表，行保留可追溯（QA-07）",
    )
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class DocumentImage(Base):
    """文档内嵌图片资产登记（F-1017）：OCR 前的原图落盘记录，支撑引用弹窗回显。

    行在 commit_ready（ingest_commit，#08 拆分后）与 chunks 同事务写入；解析失败时盘上文件为无害孤儿（行未写）。"""

    __tablename__ = "document_images"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="document_images_document_id_fkey"
        ),
        UniqueConstraint("document_id", "image_index", name="uq_doc_image_index"),
        Index("idx_doc_images_doc", "document_id"),
    )

    # 列 comment 与 0005 迁移逐字一致（autogenerate 零差异守卫依赖，同 qa_pairs.is_active 先例）
    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    document_id: Mapped[int] = mapped_column(
        BigInteger,
        comment="所属文档 id（documents.id，随文档软删/归档不动，回显走文档闸）",
    )
    image_index: Mapped[int] = mapped_column(
        Integer, comment="文档内图片序号（1 起，与切片'图片N：'标记对应）"
    )
    file_path: Mapped[str] = mapped_column(
        Text, comment="图片落盘相对路径（storage_dir 下 images/{doc_id}/img_{n}.{ext}）"
    )
    mime: Mapped[str] = mapped_column(
        Text, comment="图片 MIME（image/png 等，回显响应 Content-Type）"
    )
    page_no: Mapped[int | None] = mapped_column(
        Integer, comment="所在页码/幻灯片序号（无来源为 NULL）"
    )
    ocr_chars: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), comment="该图 OCR 提取的字符数（0=图无文字）"
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), comment="更新时间"
    )
