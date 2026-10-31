"""0014: 评测题库加 origin/tier 两列 + eval_runs.triggered_by 放宽可空（评测集自动更新）

背景（评测集自动更新闭环）：评测题库新增两条自动供给通道——难例种子 LLM 合成变体
（hardcase_synth）与文档块抽样造题（doc_synth）。自动题与人工导入题必须可区分：
- origin：题目来源标记（全自动方案的基石，报告按来源分层展示）；
- tier：题目层级——official=正式集（发版门禁口径，每轮发版前全量跑分只算它）、
  extended=自动扩充集（自动题默认落位，admin 一键升格后进正式集）。
存量人工题两列均取缺省值（manual_import / official），与现状行为完全一致；
门禁口径零变化。仅追加可回滚（§13.4）。

eval_runs.triggered_by 放宽可空：定时自动跑分的 run 无触发用户（系统动作），
triggered_by=NULL 标记系统来源，name 以「[自动]」前缀双保险。down 逆序回滚。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN_COMMENT = (
    "题目来源：manual_import=人工 Excel 导入；hardcase_synth=难例种子 LLM 合成变体；"
    "doc_synth=文档块抽样造题（评测集自动更新，自动题恒 extended 层级）"
)
_TIER_COMMENT = (
    "题目层级：official=正式集（发版门禁口径，每轮发版前全量跑分只算它）；"
    "extended=自动扩充集（自动题默认落位，人工一键升格后进正式集）"
)


def upgrade() -> None:
    op.add_column(
        "eval_questions",
        sa.Column(
            "origin",
            sa.Text(),
            server_default=sa.text("'manual_import'"),
            nullable=False,
            comment=_ORIGIN_COMMENT,
        ),
    )
    op.create_check_constraint(
        "eval_questions_origin_check",
        "eval_questions",
        "origin IN ('manual_import','hardcase_synth','doc_synth')",
    )
    op.add_column(
        "eval_questions",
        sa.Column(
            "tier",
            sa.Text(),
            server_default=sa.text("'official'"),
            nullable=False,
            comment=_TIER_COMMENT,
        ),
    )
    op.create_check_constraint(
        "eval_questions_tier_check",
        "eval_questions",
        "tier IN ('official','extended')",
    )
    # 自动题管理页按层级筛选活跃题：部分索引只盖活跃行
    op.create_index(
        "idx_eval_questions_tier",
        "eval_questions",
        ["tier"],
        postgresql_where=sa.text("is_active"),
    )
    # 定时自动跑分的 run 无触发用户（系统动作）：放宽可空，NULL 即系统来源
    op.alter_column("eval_runs", "triggered_by", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    op.alter_column("eval_runs", "triggered_by", existing_type=sa.BigInteger(), nullable=False)
    op.drop_index("idx_eval_questions_tier", table_name="eval_questions")
    op.drop_constraint("eval_questions_tier_check", "eval_questions", type_="check")
    op.drop_column("eval_questions", "tier")
    op.drop_constraint("eval_questions_origin_check", "eval_questions", type_="check")
    op.drop_column("eval_questions", "origin")
