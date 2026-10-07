"""0007: documents 草稿来源与审核状态机——Agent 产物的归宿与闸门（agent-evolution #05）。

背景（docs/specs/agent-evolution.md §五）：LLM 产出有编造风险，agent 场景的红线 =
无审核不落地——Agent 生成的产物一律是"草稿"，人工审核（admin）通过才可进入既有入库
管道（解析/查重/敏感扫描全走，无旁路）。本迁移给 documents 增三列承载该状态机：
source_type 区分上传语料与 Agent 草稿（存量行经 server_default 归为 upload，CHECK 兜底）；
review_status 承载草稿三态（待审核/已通过/已驳回，仅 draft 有值）；reject_reason 留档
驳回原因。检索红线不依赖新列：草稿登记期 status='pending' 且 is_active=False，检索侧
"仅 ready 且 is_active"过滤天然不命中（tests/integration 有读回断言）。
迁移仅追加、可回滚（down 全部撤销）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """documents 增 source_type/review_status/reject_reason 三列（每列中文注释，零差异守卫对齐）。"""
    op.add_column(
        "documents",
        sa.Column(
            "source_type",
            sa.Text(),
            server_default=text("'upload'"),
            nullable=False,
            comment="来源=上传|Agent 生成草稿（upload|draft）",
        ),
    )
    op.create_check_constraint(
        "documents_source_type_check",
        "documents",
        "source_type IN ('upload','draft')",
    )
    op.add_column(
        "documents",
        sa.Column(
            "review_status",
            sa.Text(),
            nullable=True,
            comment="草稿审核状态（待审核/已通过/已驳回），仅 source_type='draft' 有值",
        ),
    )
    op.create_check_constraint(
        "documents_review_status_check",
        "documents",
        "review_status IN ('待审核','已通过','已驳回')",
    )
    op.add_column(
        "documents",
        sa.Column(
            "reject_reason",
            sa.Text(),
            nullable=True,
            comment="驳回原因（中文，审核人填写），仅 review_status='已驳回' 有值",
        ),
    )


def downgrade() -> None:
    """回滚全部撤销：drop 三列与两个 CHECK（草稿行未走库故无数据保全问题，属设计内）。"""
    op.drop_column("documents", "reject_reason")
    op.drop_constraint("documents_review_status_check", "documents", type_="check")
    op.drop_column("documents", "review_status")
    op.drop_constraint("documents_source_type_check", "documents", type_="check")
    op.drop_column("documents", "source_type")
