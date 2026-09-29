r"""管理端只读查询服务（Task 8，F-701 提问记录）：列表与详情的配对查询。

以 user 消息行为单位（role='user' AND is_eval=false——W3 全局约束：评测数据不污染
运营查询，详情侧同样 404），每条问题配对同会话中 id 大于问题行且最小的 assistant
行——占位行与回答在 api/chat._persist_placeholder 同事务先后发号，该序即问答次序
（跨会话并发交错的 sequence 值不影响同会话内相对次序，配对不依赖 id 相邻）。配对
不筛 is_active：中断占位行（is_active=false）即"该问无回答"，映射为 assistant 缺失
（answerType/hasCitations/feedback 记 None/false），而非错误地滑向下一条回答。

列表项的 answerType/hasCitations/feedback 取自配对回答（user 行自身恒空，P7 列表
"是否有引用/反馈"列语义）。hasFeedback 筛选口径为"回答被点踩"（feedback='down'；
false 为补集，含点赞/无反馈/无回答）。日期边界按 UTC 整日解析（与库内 created_at
的 UTC 写入口径一致，不依赖服务器 TimeZone GUC）；dateTo 含当日（< 次日零点）。
查询一律参数绑定；只读服务不写库不留审计（F-701 为查询视图，写入类见 badcase）。
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select, true
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.selectable import Lateral

from app.db.models import Department, Message, User

_PREVIEW_LEN = 50  # questionPreview 截断长度（brief 钉死：截 50 字）


@dataclass(frozen=True)
class QuestionFilters:
    """列表筛选与分页入参（api 层已做边界校验，此处只做查询语义）。"""

    dept_id: int | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    has_feedback: bool | None = None
    page: int = 1
    page_size: int = 20


@dataclass(frozen=True)
class QuestionRecord:
    """列表项：回答三字段取自配对回答，配对落空（未答/中断）记 None/false。"""

    id: int
    conversation_id: int
    created_at: dt.datetime
    username: str
    dept_code: str
    question_preview: str
    answer_type: str | None
    has_citations: bool
    feedback: str | None


@dataclass(frozen=True)
class QuestionAsker:
    """详情 user 段：提问者身份与完整问题原文。"""

    id: int
    conversation_id: int
    created_at: dt.datetime
    username: str
    dept_code: str
    content: str


@dataclass(frozen=True)
class QuestionAnswer:
    """详情 assistant 段：citations 为 JSONB 原样（不重组不脱敏，前端只读展示）。"""

    id: int
    content: str
    citations: Any | None
    answer_type: str | None
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int | None
    cache_hit: bool
    feedback: str | None
    feedback_category: str | None
    feedback_comment: str | None


@dataclass(frozen=True)
class QuestionDetail:
    """详情：assistant 为 None 表示该问无有效回答（未答完即中断——占位行被软删）。"""

    user: QuestionAsker
    assistant: QuestionAnswer | None


def _answer_pair(question: Any) -> Lateral:
    """LATERAL 子查询：同会话中 id 大于问题行且最小的 assistant 行（含中断行）。

    悬空 user（上轮断流遗留）后紧邻的下一轮回答不会被错配：配对锚定"id 大于问题行
    的第一条 assistant"，中断行占住该位次并由映射层置为无回答。
    """
    answer = aliased(Message)
    return (
        select(
            answer.id.label("answer_id"),
            answer.is_active.label("answer_active"),
            answer.content.label("answer_content"),
            answer.citations.label("answer_citations"),
            answer.answer_type.label("answer_type"),
            answer.model.label("answer_model"),
            answer.prompt_tokens.label("answer_prompt_tokens"),
            answer.completion_tokens.label("answer_completion_tokens"),
            answer.latency_ms.label("answer_latency_ms"),
            answer.cache_hit.label("answer_cache_hit"),
            answer.feedback.label("answer_feedback"),
            answer.feedback_category.label("answer_category"),
            answer.feedback_comment.label("answer_comment"),
        )
        .where(
            answer.conversation_id == question.conversation_id,
            answer.role == "assistant",
            answer.id > question.id,
        )
        .order_by(answer.id.asc())
        .limit(1)
        .lateral(name="answer_pair")
    )


def _utc_start(day: dt.date) -> dt.datetime:
    """日期 → UTC 当日零点（timestamptz 比较的确定边界，不依赖服务器时区）。"""
    return dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)


def _filter_conditions(
    question: Any, pair: Lateral, filters: QuestionFilters
) -> list[ColumnElement]:
    """WHERE 条件增量组装（全部参数绑定）；hasFeedback 的 true/false 为互斥全集。"""
    conditions: list[ColumnElement] = []
    if filters.dept_id is not None:
        conditions.append(question.dept_id == filters.dept_id)
    if filters.date_from is not None:
        conditions.append(question.created_at >= _utc_start(filters.date_from))
    if filters.date_to is not None:
        conditions.append(question.created_at < _utc_start(filters.date_to + dt.timedelta(days=1)))
    if filters.has_feedback is True:
        conditions.append(and_(pair.c.answer_active.is_(True), pair.c.answer_feedback == "down"))
    elif filters.has_feedback is False:
        # IS DISTINCT FROM：无反馈（NULL）与点赞同属"未点踩"补集（!= 对 NULL 判假）
        conditions.append(
            or_(
                pair.c.answer_id.is_(None),
                pair.c.answer_active.is_(False),
                pair.c.answer_feedback.is_distinct_from("down"),
            )
        )
    return conditions


def list_question_records(
    db: Session, filters: QuestionFilters
) -> tuple[int, list[QuestionRecord]]:
    """提问记录列表：过滤/排序/分页一次成型，返回 (total, 记录列表)。

    total 与明细共用同一 WHERE（含 LATERAL——hasFeedback 依赖配对列）；排序
    created_at DESC、同刻 id 降序兜底（§22，同事务 now() 同刻的既有口径）。
    """
    question = aliased(Message, name="question_msg")
    pair = _answer_pair(question)
    base = (
        select(
            question.id.label("question_id"),
            question.conversation_id.label("conversation_id"),
            question.created_at.label("question_created_at"),
            question.content.label("question_content"),
            User.username.label("username"),
            Department.code.label("dept_code"),
            pair,
        )
        .select_from(question)  # LATERAL 引用问题列：显式锚定左侧 FROM 消除多 FROM 歧义
        .outerjoin(pair, true())  # LEFT JOIN LATERAL：无回答的问题也要出现在列表
        .join(User, User.id == question.user_id)
        .join(Department, Department.id == question.dept_id)
        .where(
            question.role == "user",
            question.is_eval.is_(False),  # W3：评测数据不进运营查询
            *_filter_conditions(question, pair, filters),
        )
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        base.order_by(question.created_at.desc(), question.id.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    ).all()
    return total, [_to_record(row) for row in rows]


def _to_record(row: Any) -> QuestionRecord:
    """行 → 列表项：仅活跃配对回答贡献回答三字段（中断/缺失一律 None/false）。"""
    answered = row.answer_id is not None and bool(row.answer_active)
    return QuestionRecord(
        id=row.question_id,
        conversation_id=row.conversation_id,
        created_at=row.question_created_at,
        username=row.username,
        dept_code=row.dept_code,
        question_preview=row.question_content[:_PREVIEW_LEN],
        answer_type=row.answer_type if answered else None,
        has_citations=bool(row.answer_citations) if answered else False,
        feedback=row.answer_feedback if answered else None,
    )


def get_question_detail(db: Session, message_id: int) -> QuestionDetail | None:
    """提问详情：user 消息 + 配对回答两段；非 user 行/评测消息/不存在 → None（404）。"""
    question = db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.role == "user",
            Message.is_eval.is_(False),  # W3：评测消息不经运营详情触达
        )
    )
    if question is None:
        return None
    answer = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == question.conversation_id,
            Message.role == "assistant",
            Message.id > question.id,
        )
        .order_by(Message.id.asc())
        .limit(1)
    ).first()
    asker_user = db.get(User, question.user_id)  # users 软删不物理删，恒可取
    asker_dept = db.get(Department, question.dept_id)  # departments 为种子静态表
    asker = QuestionAsker(
        id=question.id,
        conversation_id=question.conversation_id,
        created_at=question.created_at,
        username=asker_user.username if asker_user else "",
        dept_code=asker_dept.code if asker_dept else "",
        content=question.content,
    )
    assistant = _to_answer(answer) if answer is not None and answer.is_active else None
    return QuestionDetail(user=asker, assistant=assistant)


def _to_answer(answer: Message) -> QuestionAnswer:
    """配对回答 → 详情 assistant 段（字段直取，citations JSONB 原样）。"""
    return QuestionAnswer(
        id=answer.id,
        content=answer.content,
        citations=answer.citations,
        answer_type=answer.answer_type,
        model=answer.model,
        prompt_tokens=answer.prompt_tokens,
        completion_tokens=answer.completion_tokens,
        latency_ms=answer.latency_ms,
        cache_hit=answer.cache_hit,
        feedback=answer.feedback,
        feedback_category=answer.feedback_category,
        feedback_comment=answer.feedback_comment,
    )
