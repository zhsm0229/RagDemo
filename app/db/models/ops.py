"""运维域模型：audit_logs / app_settings（拆分自原 app/db/models.py，声明逐字保留）。"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKeyConstraint, Index, Text, text
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


class AppSetting(Base):
    """app_settings：运行配置覆盖（0016，评测集自动更新 05 票）。

    key 恒等取值（评测自动域取 'eval_auto' 单行）；value 键集由读取方约定，
    行缺失 = 全部回落 Settings 缺省；updated_by 可空（系统任务写入无触发人）。
    """

    __tablename__ = "app_settings"
    __table_args__ = (
        ForeignKeyConstraint(["updated_by"], ["users.id"], name="app_settings_updated_by_fkey"),
    )

    key: Mapped[str] = mapped_column(
        Text, primary_key=True, comment="配置域键（恒等取值，如 'eval_auto'）"
    )
    value: Mapped[Any] = mapped_column(
        JSONB,
        comment="配置值（JSONB 对象）：键集由读取方约定（如 eval_auto 的五参数），"
        "行缺失 = 全部回落 app/core/config.py 缺省",
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger,
        comment="最后修改人（users.id；系统任务写入为 NULL，同 eval_runs.triggered_by 口径）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
