"""0013: chunks 增加父块序号 parent_position（切分策略 B：notice/table_report 父子块档）

子块（≤child_len=400）进向量库保召回精度；命中后按（document_id, parent_position）
取回兄弟块拼接还原父块正文，保上下文完整（services/context_expand）。NULL=本块即
检索与上下文单元（general/operation_manual 档、条款档与全部存量块）。仅追加可回滚：
列可空（§13.4 红线①，无默认值需求）；down 先删具名部分索引再删列。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENT = (
    "父块序号（切分策略 B）：父子块档子块所属父块在文档内的 0 基序号，"
    "命中后据此取兄弟块拼回父块正文；NULL=本块即检索与上下文单元"
)


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column("parent_position", sa.Integer(), nullable=True, comment=_COMMENT),
    )
    # 兄弟块取回路径（document_id, parent_position）：部分索引只盖子块行，父块查询不走全表
    op.create_index(
        "idx_chunks_doc_parent",
        "chunks",
        ["document_id", "parent_position"],
        postgresql_where=sa.text("parent_position IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_chunks_doc_parent", table_name="chunks")
    op.drop_column("chunks", "parent_position")
