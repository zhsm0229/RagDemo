"""0006: user_memories 表 + users.memory_enabled——长期记忆数据层（F-210）。

背景（docs/specs/agent-evolution.md §三）：长期记忆是从多轮对话沉淀的用户画像结论
（如"倾向简洁回答""负责客服排班"），按用户隔离（非部门维度，不存在串库面），只
注入本人会话；写入路径 = 问答后自动抽取（带 source_message_id 溯源）+ 管理页手动
添加（source_message_id 为空）。无向量列：注入 = 按 is_active 取最近 ≤10 条时间
倒序（"最近发生的画像"而非"最相关的画像"），语义召回无必要（禁清单纪律，到触发
线再评审）。source_message_id 为逻辑引用（messages 为分区表，不建物理 FK），
保留期内可追溯"这条画像从哪句话来"。users.memory_enabled 是用户级开关（默认开）：
关闭即不注入不提炼，已有记忆保留不删除；全局开关为部署配置项不进库。
迁移仅追加、可回滚（down 全部撤销）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 user_memories + users 增列 memory_enabled（每列中文注释，零差异守卫对齐）。"""
    op.create_table(
        "user_memories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键"),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            nullable=False,
            comment="归属用户 id（users.id）；记忆按用户隔离（非部门维度），只注入本人会话，绝不跨用户",
        ),
        sa.Column(
            "content",
            sa.Text(),
            nullable=False,
            comment="记忆内容（画像结论）：CHECK 兜底 1-200 字；自动抽取单条 ≤40 字（服务层约束），手动添加上限 200 字",
        ),
        sa.Column(
            "source_message_id",
            sa.BigInteger(),
            nullable=True,
            comment="抽取来源消息 id（messages.id 逻辑引用，不建物理 FK——分区表限制），保留期内可追溯来源；NULL=手动添加",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=text("true"),
            nullable=False,
            comment="软删位：管理页删除与容量上限（每用户 30 条）淘汰最旧均置 false，行保留可追溯，不物理删",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 200", name="user_memories_content_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="user_memories_user_id_fkey"),
    )
    # 注入取最近 ≤10 条时间倒序：部分索引 (user_id, created_at DESC) WHERE is_active 即可覆盖，不建向量索引
    op.create_index(
        "idx_user_memories_user",
        "user_memories",
        ["user_id", text("created_at DESC")],
        postgresql_where=text("is_active"),
    )
    op.add_column(
        "users",
        sa.Column(
            "memory_enabled",
            sa.Boolean(),
            server_default=text("true"),
            nullable=False,
            comment="用户级记忆开关（true=参与自动提炼与问答注入；false=冻结：不注入不提炼，已有记忆保留不删除）；全局开关为部署配置项不进库",
        ),
    )


def downgrade() -> None:
    """回滚全部撤销：drop users.memory_enabled 与 user_memories 表/索引（软删行随之丢失，属设计内）。"""
    op.drop_column("users", "memory_enabled")
    op.drop_index("idx_user_memories_user", table_name="user_memories")
    op.drop_table("user_memories")
