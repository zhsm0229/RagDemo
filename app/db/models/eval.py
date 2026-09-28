"""评测域模型：eval_questions / eval_runs（拆分自原 app/db/models.py，声明逐字保留）。"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import _ts


class EvalQuestion(Base):
    __tablename__ = "eval_questions"
    __table_args__ = (
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="eval_questions_dept_id_fkey"),
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
        ForeignKeyConstraint(["triggered_by"], ["users.id"], name="eval_runs_triggered_by_fkey"),
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
