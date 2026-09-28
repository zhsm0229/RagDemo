"""组织域模型：departments / users（拆分自原 app/db/models.py，声明逐字保留）。"""

from datetime import datetime

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
from app.db.models.types import _ts


class Department(Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("code", name="departments_code_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(Text)  # 部门编码：service/hr/all
    name: Mapped[str] = mapped_column(Text)  # 中文名：客服/人力/全员
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="users_username_key"),
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="users_dept_id_fkey"),
        CheckConstraint("role IN ('admin','member')", name="users_role_check"),
        Index("idx_users_dept", "dept_id", postgresql_where=text("is_active")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text)  # 3-32 位字母数字
    password_hash: Mapped[str] = mapped_column(Text)  # bcrypt
    dept_id: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(Text, server_default=text("'member'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    must_change_password: Mapped[bool] = mapped_column(  # 首登强制改密
        Boolean, server_default=text("true")
    )
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(_ts())
    last_login_at: Mapped[datetime | None] = mapped_column(_ts())
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
