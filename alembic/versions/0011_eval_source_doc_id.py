"""0011: eval_questions.source_doc_id——题库到文档实体的可机检引用（metrics-balance 02）。

specs/metrics-balance.md §4.1：非拒答题导入时按来源列归一化匹配同部门文档（精确或
唯一后缀段），恰好命中 1 篇才放行，命中 id 落本列——四指标 Context Recall（§2）按
source_doc_id 机检，自由文本 source_ref 无法参与机检。列可空：拒答类豁免校验（落
NULL，报告入 unassigned 组）、存量旧题不回填（评测侧记 null 不计入，不阻塞跑分）。
FK 显式命名（现房规 §3.3）；documents 只软删（is_active/deleted_at），行不物理消失，
NO ACTION 即可。迁移仅追加、可回滚（down 先删约束再删列）。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/db/models/eval.py 的列 comment 逐字一致（autogenerate 零差异守卫依赖）
_COMMENT = (
    "评测题引用的源文档 id（documents.id，"
    "导入时按来源列匹配同部门文档落列；拒答类与存量旧题为 NULL）"
)


def upgrade() -> None:
    """eval_questions 增 source_doc_id（可空 BIGINT，FK 显式名，中文注释）。"""
    op.add_column(
        "eval_questions",
        sa.Column("source_doc_id", sa.BigInteger(), nullable=True, comment=_COMMENT),
    )
    op.create_foreign_key(
        "eval_questions_source_doc_id_fkey",
        "eval_questions",
        "documents",
        ["source_doc_id"],
        ["id"],
    )


def downgrade() -> None:
    """回滚全部撤销：存量题退回纯 source_ref 自由文本口径（列与本波数据一并删除）。"""
    op.drop_constraint("eval_questions_source_doc_id_fkey", "eval_questions", type_="foreignkey")
    op.drop_column("eval_questions", "source_doc_id")
