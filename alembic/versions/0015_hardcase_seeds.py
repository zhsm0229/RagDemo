"""0015: 建 hardcase_seeds 难例种子池表（评测集自动更新·难例通道）

真实用户的拒答问题与点踩回答自动沉淀为评测题合成素材（评测集自动更新闭环第一步）：
采集任务体扫 messages 的拒答/点踩行入池，合成任务体对池内点踩种子产 LLM 变体题。

口径：
- 不建 messages 外键——messages 为按月分区父表，外键必须包含复合主键列
  (id, created_at)；难例池只需 id+created_at 溯源（跳详情/审计），故两列存快照、
  不加约束（红线条目（点踩）本身另有 badcase 治理队列管理生命周期）。
- UNIQUE(dept_id, normalized_question)：同一问题（归一后）同部门只入池一次，
  count 语义由采集时的存在性判定让位给唯一约束（重复出现不再新增行）。
- status 流转：pending（可合成）→ synthesized（已产出变体）；no_ground（引用块
  缺失/拒答种子无引用，待补语料）；ignored（运营人工忽略，终态）。

仅追加可回滚：downgrade 直接 drop table。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMENT = (
    "归一去重键（去首尾空白+全角转半角+连续空白折叠）：同部门唯一，"
    "同一问题多周重复出现不再新增行"
)


def upgrade() -> None:
    op.create_table(
        "hardcase_seeds",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        # 溯源 messages 分区父表行：无 FK（父表外键须复合主键列），id+created_at 定位
        sa.Column(
            "message_id", sa.BigInteger(), nullable=False, comment="来源消息行 id（无外键，见迁移头注释）"
        ),
        sa.Column(
            "message_created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="来源消息 created_at（与 id 组成分区父表行定位）",
        ),
        sa.Column(
            "source_type",
            sa.Text(),
            nullable=False,
            comment="来源类型：refuse=拒答问题（answer_type='refuse'）；feedback_down=点踩回答（feedback='down'）",
        ),
        sa.Column("dept_id", sa.BigInteger(), nullable=False, comment="部门 id（departments.id，取消息归属部门）"),
        sa.Column("question_text", sa.Text(), nullable=False, comment="用户问题原文快照"),
        sa.Column("normalized_question", sa.Text(), nullable=False, comment=_COMMENT),
        sa.Column(
            "answer_snapshot",
            sa.Text(),
            comment="点踩时的回答原文快照（拒答种子为 NULL；合成期望答案的上下文之一）",
        ),
        sa.Column(
            "citations_snapshot",
            JSONB(),
            comment="点踩回答的引用快照（[{n,refType,refId,...}] 原样）；合成侧据此取知识块原文提取期望答案",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
            comment="状态：pending=待合成；synthesized=已产出变体；no_ground=引用缺失待补语料；ignored=运营忽略（终态）",
        ),
        sa.Column(
            "synth_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已产出变体题数（合成成功后累加）",
        ),
        sa.Column("last_synth_at", sa.DateTime(timezone=True), comment="最近一次合成时刻（注入时钟）"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dept_id"], ["departments.id"], name="hardcase_seeds_dept_id_fkey"),
        sa.CheckConstraint(
            "source_type IN ('refuse','feedback_down')", name="hardcase_seeds_source_type_check"
        ),
        sa.CheckConstraint(
            "status IN ('pending','synthesized','no_ground','ignored')",
            name="hardcase_seeds_status_check",
        ),
        sa.UniqueConstraint(
            "dept_id", "normalized_question", name="uq_hardcase_dept_norm_question"
        ),
    )
    # 采集任务与难例池页的主查询路径：按状态取新种子，时间倒序展示
    op.create_index(
        "idx_hardcase_status_time",
        "hardcase_seeds",
        ["status"],
        postgresql_where=sa.text("status IN ('pending','no_ground')"),
    )


def downgrade() -> None:
    op.drop_index("idx_hardcase_status_time", table_name="hardcase_seeds")
    op.drop_table("hardcase_seeds")
