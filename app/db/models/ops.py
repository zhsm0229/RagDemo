"""运维域模型：audit_logs（拆分自原 app/db/models.py，声明逐字保留）。"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKeyConstraint, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import _ts


class AuditLog(Base):
    """audit_logs：只增不改，无 updated_at；PK(id, created_at) 为按月分区前置。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        ForeignKeyConstraint(["actor_user_id"], ["users.id"], name="audit_logs_actor_user_id_fkey"),
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
