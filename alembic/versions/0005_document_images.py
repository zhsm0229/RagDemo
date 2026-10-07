"""0005: document_images 表——内嵌图片 OCR（F-1017）的图片资产登记。

背景（2026-10-06 试点反馈）：内嵌图片 OCR 后，切片文本带"图片N："标记可被检索，
但引用原文核对时用户需要看到**实际图片**。本表登记每张抽取图片的落盘路径与元数据，
支撑 GET /api/documents/{id}/images/{index} 回显端点；file_path 为 storage_dir 相对
路径（images/{doc_id}/img_{n}.{ext}）。行在 _commit_ready 与 chunks 同事务写入；
解析失败时盘上文件为无害孤儿（行未写，不清理，试点权衡）。迁移仅追加。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 document_images：每张成功 OCR 的内嵌图片一行（与文档同生命周期）。"""
    op.create_table(
        "document_images",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键"),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            nullable=False,
            comment="所属文档 id（documents.id，随文档软删/归档不动，回显走文档闸）",
        ),
        sa.Column("image_index", sa.Integer(), nullable=False, comment="文档内图片序号（1 起，与切片'图片N：'标记对应）"),
        sa.Column("file_path", sa.Text(), nullable=False, comment="图片落盘相对路径（storage_dir 下 images/{doc_id}/img_{n}.{ext}）"),
        sa.Column("mime", sa.Text(), nullable=False, comment="图片 MIME（image/png 等，回显响应 Content-Type）"),
        sa.Column("page_no", sa.Integer(), nullable=True, comment="所在页码/幻灯片序号（无来源为 NULL）"),
        sa.Column("ocr_chars", sa.Integer(), nullable=False, server_default=text("0"), comment="该图 OCR 提取的字符数（0=图无文字）"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], name="document_images_document_id_fkey"),
        sa.UniqueConstraint("document_id", "image_index", name="uq_doc_image_index"),
    )
    op.create_index("idx_doc_images_doc", "document_images", ["document_id"])


def downgrade() -> None:
    """回滚删除表（图片落盘文件不随迁移删除，属可再生物）。"""
    op.drop_index("idx_doc_images_doc", table_name="document_images")
    op.drop_table("document_images")
