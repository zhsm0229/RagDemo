"""记忆域模型：user_memories 用户长期记忆（F-210，agent-evolution §三）。

列 comment 与 0006 迁移逐字一致（autogenerate 零差异守卫依赖，DocumentImage/
qa_pairs.is_active 先例）；source_message_id 为逻辑引用不声明 FK（messages 为
分区表，迁移同口径）；无向量列——注入是"最近发生的画像"（时间倒序）而非"最
相关的画像"（语义召回），语义检索无必要（禁清单纪律，到触发线再评审）。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import _ts


class UserMemory(Base):
    """用户长期记忆一行 = 一条画像结论；按用户隔离，只注入本人会话。"""

    __tablename__ = "user_memories"
    __table_args__ = (
        CheckConstraint("length(content) BETWEEN 1 AND 200", name="user_memories_content_check"),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="user_memories_user_id_fkey"),
        # 注入取最近 ≤10 条时间倒序：部分索引即可覆盖，不建向量索引
        Index(
            "idx_user_memories_user",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        comment="归属用户 id（users.id）；记忆按用户隔离（非部门维度），只注入本人会话，绝不跨用户",
    )
    content: Mapped[str] = mapped_column(
        Text,
        comment="记忆内容（画像结论）：CHECK 兜底 1-200 字；自动抽取单条 ≤40 字（服务层约束），手动添加上限 200 字",
    )
    source_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        comment="抽取来源消息 id（messages.id 逻辑引用，不建物理 FK——分区表限制），保留期内可追溯来源；NULL=手动添加",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        server_default=text("true"),
        comment="软删位：管理页删除与容量上限（每用户 30 条）淘汰最旧均置 false，行保留可追溯，不物理删",
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), comment="更新时间"
    )
