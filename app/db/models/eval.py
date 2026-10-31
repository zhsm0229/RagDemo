"""评测域模型：eval_questions / eval_runs / hardcase_seeds（拆分自原 app/db/models.py）。

hardcase_seeds（0015，评测集自动更新）：真实拒答/点踩问题 → 评测题合成的素材池。
无 messages 外键（分区父表外键须复合主键列），message_id+message_created_at 快照
溯源；UNIQUE(dept_id, normalized_question) 保证同部门同一问题只入池一次。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import _ts


class EvalQuestion(Base):
    __tablename__ = "eval_questions"
    __table_args__ = (
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="eval_questions_dept_id_fkey"),
        ForeignKeyConstraint(
            ["source_doc_id"], ["documents.id"], name="eval_questions_source_doc_id_fkey"
        ),
        CheckConstraint(
            "qtype IN ('factual','multi_hop','cross_reject')", name="eval_questions_qtype_check"
        ),
        CheckConstraint(
            "origin IN ('manual_import','hardcase_synth','doc_synth')",
            name="eval_questions_origin_check",
        ),
        CheckConstraint("tier IN ('official','extended')", name="eval_questions_tier_check"),
        Index("idx_eval_questions_tier", "tier", postgresql_where=text("is_active")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dept_id: Mapped[int] = mapped_column(BigInteger)
    question: Mapped[str] = mapped_column(Text)
    expected_answer: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)  # 期望出处
    # comment 与 0011 迁移逐字一致（autogenerate 零差异守卫依赖，同 qa_pairs.is_active 先例）
    source_doc_id: Mapped[int | None] = mapped_column(
        BigInteger,
        comment="评测题引用的源文档 id（documents.id，"
        "导入时按来源列匹配同部门文档落列；拒答类与存量旧题为 NULL）",
    )
    qtype: Mapped[str] = mapped_column(Text, server_default=text("'factual'"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # 以下两列 comment 与 0014 迁移逐字一致（autogenerate 零差异守卫依赖）
    origin: Mapped[str] = mapped_column(
        Text,
        server_default=text("'manual_import'"),
        comment="题目来源：manual_import=人工 Excel 导入；hardcase_synth=难例种子 LLM 合成变体；"
        "doc_synth=文档块抽样造题（评测集自动更新，自动题恒 extended 层级）",
    )
    tier: Mapped[str] = mapped_column(
        Text,
        server_default=text("'official'"),
        comment="题目层级：official=正式集（发版门禁口径，每轮发版前全量跑分只算它）；"
        "extended=自动扩充集（自动题默认落位，人工一键升格后进正式集）",
    )
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        ForeignKeyConstraint(["triggered_by"], ["users.id"], name="eval_runs_triggered_by_fkey"),
        CheckConstraint("status IN ('running','done','failed')", name="eval_runs_status_check"),
        # [自动] 前缀部分唯一索引（0017）：同周同视角自动 run 恒唯一（防并发双跑），
        # 人工 run 命名不受限——与迁移逐字一致（autogenerate 零差异守卫）
        Index(
            "uq_eval_runs_auto_name",
            "name",
            unique=True,
            postgresql_where=text("name LIKE '[自动]%'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text)  # 如"W4 客服上线前回归"；[自动] 前缀受 0017 部分唯一约束
    config: Mapped[Any] = mapped_column(JSONB)  # 阈值快照，防跨跑对比被污染
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    total: Mapped[int] = mapped_column(Integer)
    # 加权总分（判定协议 正确1/部分0.5/错误0）：人工评 1 道部分正确即成 X.5，
    # NUMERIC 容半分；0001 的 INT 会被 PG 静默舍入（0003 迁移改型，autodiff 守卫对齐）
    correct: Mapped[Decimal] = mapped_column(
        Numeric(6, 1),
        comment="加权总分：判定协议 正确1/部分0.5/错误0 折算，可为半分（如 2.5）；accuracy=correct/total",
    )
    accuracy: Mapped[Any] = mapped_column(Numeric(5, 4))
    detail: Mapped[Any | None] = mapped_column(JSONB)  # 每题判定与引用快照
    # 0014 放宽可空：定时自动跑分的 run 无触发用户（NULL=系统来源，name 以「[自动]」前缀双保险）
    triggered_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class HardcaseSeed(Base):
    """hardcase_seeds：难例种子池（真实拒答/点踩问题 → 评测题合成的素材）。

    无 messages 外键（分区父表外键须复合主键列），message_id+message_created_at
    快照溯源；UNIQUE(dept_id, normalized_question) 保证同部门同一问题只入池一次。
    """

    __tablename__ = "hardcase_seeds"
    __table_args__ = (
        ForeignKeyConstraint(["dept_id"], ["departments.id"], name="hardcase_seeds_dept_id_fkey"),
        CheckConstraint(
            "source_type IN ('refuse','feedback_down')", name="hardcase_seeds_source_type_check"
        ),
        CheckConstraint(
            "status IN ('pending','synthesized','no_ground','ignored')",
            name="hardcase_seeds_status_check",
        ),
        UniqueConstraint("dept_id", "normalized_question", name="uq_hardcase_dept_norm_question"),
        Index(
            "idx_hardcase_status_time",
            "status",
            postgresql_where=text("status IN ('pending','no_ground')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # comment 与 0015 迁移逐字一致（autogenerate 零差异守卫依赖）
    message_id: Mapped[int] = mapped_column(
        BigInteger, comment="来源消息行 id（无外键，见迁移头注释）"
    )
    message_created_at: Mapped[datetime] = mapped_column(
        _ts(), comment="来源消息 created_at（与 id 组成分区父表行定位）"
    )
    source_type: Mapped[str] = mapped_column(
        Text,
        comment="来源类型：refuse=拒答问题（answer_type='refuse'）；feedback_down=点踩回答（feedback='down'）",
    )
    dept_id: Mapped[int] = mapped_column(
        BigInteger, comment="部门 id（departments.id，取消息归属部门）"
    )
    question_text: Mapped[str] = mapped_column(Text, comment="用户问题原文快照")
    normalized_question: Mapped[str] = mapped_column(
        Text,
        comment="归一去重键（去首尾空白+全角转半角+连续空白折叠）：同部门唯一，"
        "同一问题多周重复出现不再新增行",
    )
    answer_snapshot: Mapped[str | None] = mapped_column(
        Text, comment="点踩时的回答原文快照（拒答种子为 NULL；合成期望答案的上下文之一）"
    )
    citations_snapshot: Mapped[Any | None] = mapped_column(
        JSONB,
        comment="点踩回答的引用快照（[{n,refType,refId,...}] 原样）；合成侧据此取知识块原文提取期望答案",
    )
    status: Mapped[str] = mapped_column(
        Text,
        server_default=text("'pending'"),
        comment="状态：pending=待合成；synthesized=已产出变体；no_ground=引用缺失待补语料；ignored=运营忽略（终态）",
    )
    synth_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), comment="已产出变体题数（合成成功后累加）"
    )
    last_synth_at: Mapped[datetime | None] = mapped_column(
        _ts(), comment="最近一次合成时刻（注入时钟）"
    )
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
