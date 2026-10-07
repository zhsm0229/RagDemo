"""Agent 域模型：agent_runs 一次 agent 任务一行（F-211/F-212，agent-evolution §四.5）。

runs 属审计面且可更新（running → 终态回填 output/error/用量/耗时），审核回放与
"前端事件序 = 库内轨迹"（specs §6.1）都以此为准；agent_steps（逐步轨迹，只增不改）
随工单 #06 的审计接线同域迁入本文件。列 comment 与 0008/0009 迁移逐字一致（零差异
守卫依赖，UserMemory 先例）；删除无软删位——审计行不物理删、保留策略按 specs §六.17
预告随合规签批评估，status 的 cancelled 取值预留给人工终止（当前无人写该值）。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import _ts


class AgentRun(Base):
    """一次 agent 任务：归属提问者 + 部门隔离口径来源 + 状态机与产物快照。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed','cancelled')",
            name="agent_runs_status_check",
        ),
        ForeignKeyConstraint(["user_id"], ["users.id"], name="agent_runs_user_id_fkey"),
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="agent_runs_dept_id_fkey"),
        Index("idx_agent_runs_user", "user_id", text("created_at DESC")),
        Index("idx_agent_runs_dept_time", "dept_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        comment="归属用户 id（users.id）；任务按用户归属，轨迹回放/清单查询须校验本人（IDOR §21.8）",
    )
    dept_id: Mapped[int] = mapped_column(
        BigInteger,
        comment="归属部门 id（departments.id）：检索类工具隔离口径的来源快照（F-502 同源），审计与隔离排查用",
    )
    run_type: Mapped[str] = mapped_column(
        Text,
        comment="任务类型：kb_qa（知识库问答）/ template_draft（模板草稿，#06 落地）等；枚举随任务扩展（TEXT 不设 CHECK，迁移友好）",
    )
    status: Mapped[str] = mapped_column(
        Text,
        server_default=text("'running'"),
        comment="任务状态：running=进行中 / succeeded=成功 / failed=失败（含流中断裂与上游故障）/ cancelled=人工终止（预留）；流收尾必达终态，不留 running 幽灵行",
    )
    input: Mapped[Any] = mapped_column(
        JSONB, comment="任务输入快照：{taskType, input}（原样落库，回放与审计依据）"
    )
    output: Mapped[Any | None] = mapped_column(
        JSONB, comment="任务产物：{text: 最终成稿文本}（审核前皆为草稿，无审核不落地，specs §五）"
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        comment="失败中文摘要（对外脱敏话术，技术细节只进服务端日志 §6.8）；succeeded 时为 NULL",
    )
    model: Mapped[str | None] = mapped_column(
        Text,
        comment="实际使用的模型名（settings.agent_model_model → llm_model → 'stub' 回退链）；成功时回填",
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer,
        server_default=text("0"),
        comment="输入 token 用量（成本口径同 messages F-209）；通道尚未回传用量，恒为 0 待通道补齐",
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer,
        server_default=text("0"),
        comment="输出 token 用量（成本口径同 messages F-209）；通道尚未回传用量，恒为 0 待通道补齐",
    )
    latency_ms: Mapped[int | None] = mapped_column(
        Integer, comment="任务耗时毫秒（自请求处理起算到收尾完成，与 messages.latency_ms 同口径）"
    )
    started_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), comment="任务开始时刻（占位行落库即开始）"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        _ts(), comment="任务终态时刻（succeeded/failed 回填）；running 时为 NULL"
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), comment="更新时间"
    )


class AgentStep(Base):
    """一次工具调用的轨迹行：与 AG-UI 的 TOOL_CALL_* 事件一一对应（specs §6.1）。

    只增不改：观察随行一次写入、之后不 UPDATE（audit_logs 同口径），故无 updated_at；
    一期只写 step_type='tool'，llm/planning 取值预留给 DeepAgents 深化（触发线评审后启用）。
    """

    __tablename__ = "agent_steps"
    __table_args__ = (
        CheckConstraint(
            "step_type IN ('llm','tool','planning')",
            name="agent_steps_step_type_check",
        ),
        ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="agent_steps_run_id_fkey"),
        Index("idx_agent_steps_run", "run_id", "seq"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键"
    )
    run_id: Mapped[int] = mapped_column(
        BigInteger, comment="所属 agent 任务 id（agent_runs.id）：轨迹按 run 聚合回放"
    )
    seq: Mapped[int] = mapped_column(
        Integer, comment="步序（run 内从 1 递增）：与 AG-UI 事件序一致，回放排序依据"
    )
    step_type: Mapped[str] = mapped_column(
        Text,
        comment="步骤类型：tool=工具调用 / llm=模型推理 / planning=规划；一期只写 tool",
    )
    tool_name: Mapped[str | None] = mapped_column(
        Text,
        comment="工具名（step_type='tool' 时必填，如 knowledge_base_search / save_draft）；子代理与文件系统操作逐工具落此列（审计面逐工具落实，specs §四.5）",
    )
    args: Mapped[Any | None] = mapped_column(
        JSONB,
        comment="工具参数快照（超限截断为 preview 形态，防审计面膨胀；上限实现期定）",
    )
    observation: Mapped[str | None] = mapped_column(
        Text,
        comment="工具观察结果快照（超长截断存储，截断处标注）：审核回放'结论怎么来的'的依据",
    )
    error: Mapped[str | None] = mapped_column(
        Text,
        comment="工具失败中文原因（对外脱敏话术，与 run.error 同口径）；成功时为 NULL",
    )
    latency_ms: Mapped[int | None] = mapped_column(
        Integer,
        comment="该步耗时毫秒（自工具调用事件至观察返回的近似墙钟，含编排开销；仅供排查参考）",
    )
    created_at: Mapped[datetime] = mapped_column(
        _ts(), server_default=text("now()"), comment="创建时间"
    )
