"""0018: hardcase_seeds 加 skip_count 列（商用就绪工单 16，no_ground 闭环）。

skip_count：pending 种子合成失败（解析失败/全变体无效）连续累计计数——达到 3 次
转 no_ground，病态种子不再每周空耗 LLM 预算；重扫（rescan）成功转回 pending 时
清零。预算耗尽的轮次不累计（运营约束不是种子缺陷）。

回滚：drop 该列即可（downgrade），不触碰数据。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hardcase_seeds",
        sa.Column(
            "skip_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="pending 种子合成失败连续累计（≥3 转 no_ground 待补语料；重扫转回清零；预算耗尽不累计）",
        ),
    )


def downgrade() -> None:
    op.drop_column("hardcase_seeds", "skip_count")
