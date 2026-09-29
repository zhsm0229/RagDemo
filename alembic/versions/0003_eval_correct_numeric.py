"""0003: eval_runs.correct 改 NUMERIC(6,1)——人工评判 0.5 权重的半分总分（Task 7）

判定协议第 1 条（specs/features.md 验收判定协议）：总分 = 正确 + 0.5×部分正确，
录 1 道部分正确即产生 X.5 分——原 INT 列会静默舍入（如 2.5→3），与
accuracy=correct/total 的分母口径背离、污染验收硬指标 #1 的准确率证据。类型对齐
eval_service.JUDGMENT_SCORES 折算（正确 1 / 部分 0.5 / 错误 0）。迁移仅追加、不改
写历史脚本；down 回 INT（round 舍入丢半分精度，仅回滚场景可达，数据不丢行）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENT = "加权总分：判定协议 正确1/部分0.5/错误0 折算，可为半分（如 2.5）；accuracy=correct/total"


def upgrade() -> None:
    """correct INT → NUMERIC(6,1)：容纳 0.5 权重部分正确的半分总分（int→numeric 免 USING）。"""
    op.alter_column(
        "eval_runs",
        "correct",
        existing_type=sa.Integer(),
        type_=sa.Numeric(6, 1),
        existing_nullable=False,
        comment=_COMMENT,
    )


def downgrade() -> None:
    """回滚 NUMERIC(6,1) → INT：半分按 round 舍入（仅回滚场景可达），注释一并撤销。"""
    op.alter_column(
        "eval_runs",
        "correct",
        existing_type=sa.Numeric(6, 1),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="round(correct)::integer",
        comment=None,
    )
