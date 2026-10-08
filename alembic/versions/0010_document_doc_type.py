"""0010: documents 增加分型列 doc_type（metrics-balance 工单 01，文档自动分型）

五档自动分型的落库前置：入库管道在分块前按结构信号检测（app/rag/doc_profile 纯函数
域），doc_type 随收口写 documents；查询期按档取精排 top_n、评测报告按型分组（§3.2）。
VARCHAR(32) 存 code（中文文案映射在前端，§3.3 现房规）；NOT NULL + server_default
'general' 满足可回滚红线（存量行自动补兜底档，up/down 成对：down 先删具名 CHECK 再
删列向前兼容）。CHECK 具名 documents_doc_type_check（同 status 先例），枚举值与
app/rag/doc_profile.DocProfile 同源，ORM 列 comment 与本迁移逐字一致
（autogenerate 零差异守卫依赖）。迁移仅追加，不改写历史脚本。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENT = (
    "文档分型：入库自动检测五档（policy_clause=制度条款/table_report=表格报表/"
    "notice=通知公告/operation_manual=操作手册/general=通用），可人工修正"
)

# 与 app/rag/doc_profile.DocProfile 五档 code 同源；ORM CheckConstraint 同文（零差异守卫）
_CHECK_SQL = "doc_type IN ('policy_clause','table_report','notice','operation_manual','general')"


def upgrade() -> None:
    """documents 加 doc_type：五档 code，NOT NULL DEFAULT 'general' + 具名 CHECK + 中文注释。"""
    op.add_column(
        "documents",
        sa.Column(
            "doc_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'general'"),
            comment=_COMMENT,
        ),
    )
    op.create_check_constraint("documents_doc_type_check", "documents", _CHECK_SQL)


def downgrade() -> None:
    """回滚先删 CHECK 再删列向前兼容：分型信息随列消失（仅回滚场景可达），分组报告
    与按档 top_n 退回无分型口径（doc_type 缺失按 general 档）。"""
    op.drop_constraint("documents_doc_type_check", "documents", type_="check")
    op.drop_column("documents", "doc_type")
