"""0008: agent_runs 表——agent 任务审计面（F-211/F-212）。

背景（docs/specs/agent-evolution.md §四.5、§六；db-design §六.17）：/api/agent/runs
（工单 #07）每开一流落一行，归属提问者并快照部门隔离口径来源；runs 可更新
（running → 终态回填 output/error/模型/耗时），前端 AG-UI 事件序 = 库内轨迹
（specs §6.1），收缩旧 /chat/stream 后仍可回放。agent_steps（逐步轨迹，只增不改）
随工单 #06 的审计接线另行迁移（工具层审计语义已声明于 ToolSpec.audit_note），
编号顺延不改本表设计。迁移仅追加、可回滚（down 整表撤销，审计行随之丢失属设计内）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 agent_runs（每列中文注释，零差异守卫对齐 app/db/models/agent.py）。"""
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键"),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            nullable=False,
            comment="归属用户 id（users.id）；任务按用户归属，轨迹回放/清单查询须校验本人（IDOR §21.8）",
        ),
        sa.Column(
            "dept_id",
            sa.BigInteger(),
            nullable=False,
            comment="归属部门 id（departments.id）：检索类工具隔离口径的来源快照（F-502 同源），审计与隔离排查用",
        ),
        sa.Column(
            "run_type",
            sa.Text(),
            nullable=False,
            comment="任务类型：kb_qa（知识库问答）/ template_draft（模板草稿，#06 落地）等；枚举随任务扩展（TEXT 不设 CHECK，迁移友好）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=text("'running'"),
            nullable=False,
            comment="任务状态：running=进行中 / succeeded=成功 / failed=失败（含流中断裂与上游故障）/ cancelled=人工终止（预留）；流收尾必达终态，不留 running 幽灵行",
        ),
        sa.Column(
            "input",
            JSONB(),
            nullable=False,
            comment="任务输入快照：{taskType, input}（原样落库，回放与审计依据）",
        ),
        sa.Column(
            "output",
            JSONB(),
            nullable=True,
            comment="任务产物：{text: 最终成稿文本}（审核前皆为草稿，无审核不落地，specs §五）",
        ),
        sa.Column(
            "error",
            sa.Text(),
            nullable=True,
            comment="失败中文摘要（对外脱敏话术，技术细节只进服务端日志 §6.8）；succeeded 时为 NULL",
        ),
        sa.Column(
            "model",
            sa.Text(),
            nullable=True,
            comment="实际使用的模型名（settings.agent_model_model → llm_model → 'stub' 回退链）；成功时回填",
        ),
        sa.Column(
            "prompt_tokens",
            sa.Integer(),
            server_default=text("0"),
            nullable=False,
            comment="输入 token 用量（成本口径同 messages F-209）；通道尚未回传用量，恒为 0 待通道补齐",
        ),
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            server_default=text("0"),
            nullable=False,
            comment="输出 token 用量（成本口径同 messages F-209）；通道尚未回传用量，恒为 0 待通道补齐",
        ),
        sa.Column(
            "latency_ms",
            sa.Integer(),
            nullable=True,
            comment="任务耗时毫秒（自请求处理起算到收尾完成，与 messages.latency_ms 同口径）",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="任务开始时刻（占位行落库即开始）",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="任务终态时刻（succeeded/failed 回填）；running 时为 NULL",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled')",
            name="agent_runs_status_check",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="agent_runs_user_id_fkey"),
        sa.ForeignKeyConstraint(["dept_id"], ["departments.id"], name="agent_runs_dept_id_fkey"),
    )
    # 清单按用户 / 部门时间倒序各一（管理页轨迹查看与审计排查消费，非部分索引）
    op.create_index("idx_agent_runs_user", "agent_runs", ["user_id", text("created_at DESC")])
    op.create_index("idx_agent_runs_dept_time", "agent_runs", ["dept_id", text("created_at DESC")])


def downgrade() -> None:
    """回滚全部撤销：drop agent_runs 索引与表（审计行随之丢失，属设计内）。"""
    op.drop_index("idx_agent_runs_dept_time", table_name="agent_runs")
    op.drop_index("idx_agent_runs_user", table_name="agent_runs")
    op.drop_table("agent_runs")
