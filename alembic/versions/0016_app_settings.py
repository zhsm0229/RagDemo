"""0016: 建 app_settings 运行配置表（评测集自动更新 05 票）

评测自动闭环的运行参数（总开关/预算/退化阈值）需要在线读写且重启不丢：
pydantic Settings 是进程静态值（env/代码缺省），无法承载管理端 PUT——本表以
key/value(JSONB) 存运行时覆盖值，读取侧缺省回落 Settings（行缺失 = 用缺省）。
key 主键 TEXT（恒等取 'eval_auto' 单行，泛化留后续配置域）；updated_by 可空
（系统任务写入无触发人，同 eval_runs.triggered_by 口径）。仅追加可回滚。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENT = (
    "配置值（JSONB 对象）：键集由读取方约定（如 eval_auto 的五参数），"
    "行缺失 = 全部回落 app/core/config.py 缺省"
)


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True, comment="配置域键（恒等取值，如 'eval_auto'）"),
        sa.Column("value", JSONB(), nullable=False, comment=_COMMENT),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            comment="最后修改人（users.id；系统任务写入为 NULL，同 eval_runs.triggered_by 口径）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], name="app_settings_updated_by_fkey"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
