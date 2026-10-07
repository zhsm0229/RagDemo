"""0004: documents 加解析可观测三列——parse_stage/parse_progress/parse_error_detail。

背景（2026-10-02 试点反馈）：解析失败后用户只能看到脱敏文案"向量化服务调用失败"，
真实技术原因（TEI 422/超时）完全不可见；"解析中"进度条是固定 50% 的装饰动画，
无法判断在正常处理还是已死。三列均为可空（不参与状态机 CHECK，收口时置 NULL）：
- parse_stage：当前阶段文案（排队中/解析中/分块中/向量化中），parsing 期驱动 UI；
- parse_progress：0-100 真实进度（向量化按批次推进）；
- parse_error_detail：技术错误摘要（异常类型+消息+末栈帧，管理端定位用），
  与用户友好的 parse_error 双轨——脱敏对外、细节给管理员。
迁移仅追加；可空列无 server_default（先例：parse_engine/parse_error，0001_init）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGE_COMMENT = "解析当前阶段文案（排队中/解析中/分块中/向量化中），仅 parsing 期有值"
_PROGRESS_COMMENT = "解析真实进度 0-100（向量化阶段按嵌入批次推进），收口置空"
_DETAIL_COMMENT = "技术错误摘要（异常类型+消息+末栈帧），管理端定位用，与用户文案双轨"


def upgrade() -> None:
    """documents 加三列：解析阶段/真实进度/技术错误摘要（均可空，无默认值）。"""
    op.add_column("documents", sa.Column("parse_stage", sa.Text(), nullable=True, comment=_STAGE_COMMENT))
    op.add_column("documents", sa.Column("parse_progress", sa.Integer(), nullable=True, comment=_PROGRESS_COMMENT))
    op.add_column("documents", sa.Column("parse_error_detail", sa.Text(), nullable=True, comment=_DETAIL_COMMENT))


def downgrade() -> None:
    """回滚删除三列（可空列无数据依赖，直接 drop）。"""
    op.drop_column("documents", "parse_error_detail")
    op.drop_column("documents", "parse_progress")
    op.drop_column("documents", "parse_stage")
