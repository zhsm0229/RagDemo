r"""管理端 badcase 治理服务（Task 9，F-702）：点踩队列查询与处理状态流转。

队列口径（brief 钉死）：messages 行 feedback='down' AND is_eval=false 为一条点踩
记录——一条 assistant 回答一条记录，直接以该行为主行天然不重复；is_eval 的点踩
不进队列（W3 约束：评测数据不污染运营查询，处置寻址同样 404）。questionPreview
取该回答配对的问题：同会话中 id 小于回答行且最大的 user 行（占位行与回答同事务
先后发号，该序即问答次序——与 services.admin_views 的配对互为反向）。
badcaseStatus 缺省不过滤（brief：'待处理' 由前端传）；枚举外状态值按普通过滤
语义落空集（同 deptId=不存在部门的既有口径，不在查询层重复校验）。

处置（update_badcase）只迁移到 admin 终态（'已修语料'/'已忽略' 由 api 层 Literal
把关；'待处理' 仅由 W2 点踩入口产生，不提供重开回退）；admin 终态不被用户 down
重开的裁决已在 W2 反馈端点落地（app/api/feedback.py），本服务只写终态。note 是
admin 处理备注，只入审计不入 messages（W3 计划钉死）；审计与状态更新同事务
commit（留痕与事实一致，§23）。查询一律参数绑定。
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, func, select, true
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.selectable import Lateral

from app.db.models import Department, Message
from app.services.audit import record_audit

_PREVIEW_LEN = 50  # questionPreview 截断长度（与 admin_views 同一 brief 口径）


@dataclass(frozen=True)
class BadcaseFilters:
    """队列筛选与分页入参（api 层已做边界校验，此处只做查询语义）。"""

    dept_id: int | None = None
    badcase_status: str | None = None
    page: int = 1
    page_size: int = 20


@dataclass(frozen=True)
class BadcaseRecord:
    """队列项：category/comment 取回答行反馈两列（用户最新一次点踩的值）。"""

    message_id: int
    created_at: dt.datetime
    dept_code: str
    question_preview: str
    category: str | None
    comment: str | None
    badcase_status: str | None


def _question_pair(answer: Any) -> Lateral:
    """LATERAL 子查询：同会话中 id 小于回答行且最大的 user 行（该答对应的问题）。

    与 services.admin_views._answer_pair 互为反向；悬空回答（上轮断流遗留）配对
    落空 → question_content 为 None，映射层截断前置空兜底。
    """
    question = aliased(Message)
    return (
        select(question.content.label("question_content"))
        .where(
            question.conversation_id == answer.conversation_id,
            question.role == "user",
            question.id < answer.id,
        )
        .order_by(question.id.desc())
        .limit(1)
        .lateral(name="question_pair")
    )


def list_badcases(db: Session, filters: BadcaseFilters) -> tuple[int, list[BadcaseRecord]]:
    """点踩队列列表：过滤/排序/分页一次成型，返回 (total, 记录列表)。

    total 与明细共用同一 WHERE（含 LATERAL）；排序 created_at DESC、同刻 id 降序
    兜底（§22，同事务 now() 同刻的既有口径）。
    """
    answer = aliased(Message, name="badcase_msg")
    pair = _question_pair(answer)
    conditions: list[ColumnElement] = []
    if filters.dept_id is not None:
        conditions.append(answer.dept_id == filters.dept_id)
    if filters.badcase_status is not None:
        conditions.append(answer.badcase_status == filters.badcase_status)
    base = (
        select(
            answer.id.label("message_id"),
            answer.created_at.label("answer_created_at"),
            answer.badcase_status.label("badcase_status"),
            answer.feedback_category.label("category"),
            answer.feedback_comment.label("comment"),
            Department.code.label("dept_code"),
            pair,
        )
        .select_from(answer)  # LATERAL 引用回答列：显式锚定左侧 FROM 消除多 FROM 歧义
        .outerjoin(pair, true())  # LEFT JOIN LATERAL：悬空回答也出现在队列
        .join(Department, Department.id == answer.dept_id)
        .where(
            answer.feedback == "down",
            answer.is_eval.is_(False),  # W3：评测数据不进运营队列
            *conditions,
        )
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        base.order_by(answer.created_at.desc(), answer.id.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    ).all()
    return total, [_to_record(row) for row in rows]


def _to_record(row: Any) -> BadcaseRecord:
    """行 → 队列项：questionPreview 恰截 50 字（悬空回答配对落空记空串）。"""
    return BadcaseRecord(
        message_id=row.message_id,
        created_at=row.answer_created_at,
        dept_code=row.dept_code,
        question_preview=(row.question_content or "")[:_PREVIEW_LEN],
        category=row.category,
        comment=row.comment,
        badcase_status=row.badcase_status,
    )


def update_badcase(
    db: Session, message_id: int, admin_id: int, new_status: str, note: str | None
) -> int | None:
    """处置一条点踩记录：badcase_status 迁移 + 审计同事务 commit，返回消息 id。

    寻址宇宙与队列一致（feedback='down' AND is_eval=false），宇宙外返回 None 由
    api 层 404/3003 同文案（不给探测空间，IDOR §21.8）；审计 detail
    {messageId,from,to,note}（from 允许 null——早期未入队列的历史点踩），note 只
    入审计不入 messages。"""
    message = db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.feedback == "down",
            Message.is_eval.is_(False),  # W3：评测消息不经运营处置触达
        )
    )
    if message is None:
        return None
    old_status = message.badcase_status
    message.badcase_status = new_status
    record_audit(
        db,
        admin_id,
        "badcase.update",
        "message",
        message.id,
        {"messageId": message.id, "from": old_status, "to": new_status, "note": note},
    )
    db.commit()  # 业务写入与审计同事务（§23）；调用方只拿 id 组装响应
    return message.id
