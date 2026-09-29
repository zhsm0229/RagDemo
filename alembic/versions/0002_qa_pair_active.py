"""0002: qa_pairs 增加软删位 is_active（F-402/QA-06/07 管理面）

问答对管理三操作（停用/编辑/删除）的前置列：删除 = is_active=false 软删，退出
检索与列表但行保留可追溯（审计红线）；检索侧 match_qa 追加 is_active 过滤构成
status+is_active 双闸（QA-07）。带 server_default 满足可回滚红线①（新增列带默认
值且 NOT NULL，存量行自动补真，up/down 成对、down 删列向前兼容）。迁移仅追加：
本文件不改写 0001。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """qa_pairs 加 is_active：软删位，删除（DELETE 端点）置 false 后退出检索与列表。"""
    op.add_column(
        "qa_pairs",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="软删位：删除问答对置 false，退出检索与列表，行保留可追溯（QA-07）",
        ),
    )


def downgrade() -> None:
    """回滚删列向前兼容：is_active 的过滤随之消失，仅回滚场景可达（数据不删）。"""
    op.drop_column("qa_pairs", "is_active")
