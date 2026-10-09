"""会话域模型：conversations / messages（拆分自原 app/db/models.py，声明逐字保留）。"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import _ts


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(["user_id"], ["users.id"], name="conversations_user_id_fkey"),
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="conversations_dept_id_fkey"),
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
    degraded: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("false"),
        comment="检索上游降级标注：embed 失败拒答或 rerank 故障向量序兜底轮为 true，正常轮恒 false",
    )  # 0012（retrieval-policy §2）：降级轮拒答闸走降级阈值、分数为召回余弦口径
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
