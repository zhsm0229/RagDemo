"""0009: agent_steps 表——agent 逐工具调用轨迹（F-211 审计面，工单 #06）。

背景（docs/specs/agent-evolution.md §四.5、§五；db-design §六.17）：agent_runs（0008）
记任务级审计，本表记步级——每次工具调用一行（step_type='tool'），与 AG-UI 的
TOOL_CALL_* 事件一一对应（"前端看到的事件序 = 库内轨迹"，specs §6.1），审核人可
回放"结论怎么来的"（调了哪些工具、参数与观察快照）。表设计按 db-design §六.17
预告落地并按实现工单补 error 列（工具失败原因快照，脱敏话术与 run.error 同口径）。
只增不改：无 updated_at（audit_logs 同口径）；审计行不物理删，保留策略随合规签批
评估。迁移仅追加、可回滚（down 整表撤销，轨迹行随之丢失属设计内）。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 agent_steps（每列中文注释，零差异守卫对齐 app/db/models/agent.py）。"""
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键"),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            nullable=False,
            comment="所属 agent 任务 id（agent_runs.id）：轨迹按 run 聚合回放",
        ),
        sa.Column(
            "seq",
            sa.Integer(),
            nullable=False,
            comment="步序（run 内从 1 递增）：与 AG-UI 事件序一致，回放排序依据",
        ),
        sa.Column(
            "step_type",
            sa.Text(),
            nullable=False,
            comment="步骤类型：tool=工具调用 / llm=模型推理 / planning=规划；一期只写 tool",
        ),
        sa.Column(
            "tool_name",
            sa.Text(),
            nullable=True,
            comment="工具名（step_type='tool' 时必填，如 knowledge_base_search / save_draft）；子代理与文件系统操作逐工具落此列（审计面逐工具落实，specs §四.5）",
        ),
        sa.Column(
            "args",
            JSONB(),
            nullable=True,
            comment="工具参数快照（超限截断为 preview 形态，防审计面膨胀；上限实现期定）",
        ),
        sa.Column(
            "observation",
            sa.Text(),
            nullable=True,
            comment="工具观察结果快照（超长截断存储，截断处标注）：审核回放'结论怎么来的'的依据",
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
            comment="工具失败中文原因（对外脱敏话术，与 run.error 同口径）；成功时为 NULL",
        ),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            nullable=True,
            comment="该步耗时毫秒（自工具调用事件至观察返回的近似墙钟，含编排开销；仅供排查参考）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "step_type IN ('llm','tool','planning')",
            name="agent_steps_step_type_check",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="agent_steps_run_id_fkey"),
    )
    # 回放按 run 聚合 + seq 排序（管理页轨迹查看与审核回放消费，非部分索引）
    op.create_index("idx_agent_steps_run", "agent_steps", ["run_id", "seq"])


def downgrade() -> None:
    """回滚全部撤销：drop agent_steps 索引与表（轨迹行随之丢失，属设计内）。"""
    op.drop_index("idx_agent_steps_run", table_name="agent_steps")
    op.drop_table("agent_steps")
