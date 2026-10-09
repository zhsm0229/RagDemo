"""0012: messages.degraded——检索上游降级标注（retrieval-policy spec §2 全链契约）。

降级轮（embed 失败拒答 / rerank 故障向量序兜底）为 true：拒绝闸走降级阈值、citation
分数为召回余弦口径（trace 以 scoreScale="cosine" 标注），SSE done 事件同名布尔透出、
GET messages 回传——重开会话标注不消失。BOOLEAN NOT NULL DEFAULT FALSE：存量消息
（含迁移前所有正常轮）零回填即语义正确；降级态 rag 计划不写精确缓存（spec §11 裁决 2），
缓存命中轮恒 false。迁移仅追加、可回滚（down 删列，降级标注随列一并删除）。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/db/models/chat.py 的列 comment 逐字一致（autogenerate 零差异守卫依赖）
_COMMENT = "检索上游降级标注：embed 失败拒答或 rerank 故障向量序兜底轮为 true，正常轮恒 false"


def upgrade() -> None:
    """messages 增 degraded（BOOLEAN NOT NULL DEFAULT FALSE，中文注释；分区父表
    add_column 由 PG 自动传播到全部分区子表）。"""
    op.add_column(
        "messages",
        sa.Column(
            "degraded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=_COMMENT,
        ),
    )


def downgrade() -> None:
    """回滚撤销降级标注列（spec §2 全链契约随列一并退场）。"""
    op.drop_column("messages", "degraded")
