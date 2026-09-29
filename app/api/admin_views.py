r"""管理后台路由（M7 起）：/api/admin/*——admin 专属查询/处理端点的汇聚 router。

本文件是管理端路由唯一挂载点（Task 9 badcase / Task 10 检索测试 / Task 11 运营
概览陆续加入本 router，保持单文件 ≤300 行，超出时按域拆子 router）。全部端点过
AdminUser 门（依赖链：登录 → 首登改密 → 角色，401/2003、403/2004，§5.5）；提问
记录（F-701）以 user 消息行为单位且恒滤 is_eval（W3 约束：评测数据不污染运营
查询）。查询与配对逻辑在 services.admin_views，本层只做参数接收与 DTO 组装
（§1.2 分层）；DTO 键全 camelCase（§3.4），列表 {items,total,page,pageSize}
（§3.5），响应体 {code,message,data}。
"""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, DbSession
from app.core.errors import ERR_NOT_FOUND, ApiError
from app.services.admin_views import (
    QuestionAnswer,
    QuestionDetail,
    QuestionFilters,
    QuestionRecord,
    get_question_detail,
    list_question_records,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_MSG_NOT_FOUND = "提问记录不存在"  # 非 user 行 / 评测消息 / 不存在 id 同文案（不给探测空间）


def _record_payload(record: QuestionRecord) -> dict:
    """列表项 DTO（§3.4 camelCase）：questionPreview 已在 service 截 50 字。"""
    return {
        "id": record.id,
        "conversationId": record.conversation_id,
        "createdAt": record.created_at.isoformat(),
        "username": record.username,
        "deptCode": record.dept_code,
        "questionPreview": record.question_preview,
        "answerType": record.answer_type,
        "hasCitations": record.has_citations,
        "feedback": record.feedback,
    }


def _answer_payload(answer: QuestionAnswer) -> dict:
    """详情 assistant 段 DTO：citations JSONB 原样透传（含 id——badcase 处理入口
    按 assistant 消息 id 寻址，Task 9 消费）。"""
    return {
        "id": answer.id,
        "content": answer.content,
        "citations": answer.citations,
        "answerType": answer.answer_type,
        "model": answer.model,
        "promptTokens": answer.prompt_tokens,
        "completionTokens": answer.completion_tokens,
        "latencyMs": answer.latency_ms,
        "cacheHit": answer.cache_hit,
        "feedback": answer.feedback,
        "feedbackCategory": answer.feedback_category,
        "feedbackComment": answer.feedback_comment,
    }


def _detail_payload(detail: QuestionDetail) -> dict:
    """详情 DTO：{user, assistant}，assistant 为 null 表示该问无有效回答（中断）。"""
    user = detail.user
    user_part = {
        "id": user.id,
        "conversationId": user.conversation_id,
        "createdAt": user.created_at.isoformat(),
        "username": user.username,
        "deptCode": user.dept_code,
        "content": user.content,  # 提问原文（区别于列表的 50 字摘要）
    }
    assistant = None if detail.assistant is None else _answer_payload(detail.assistant)
    return {"user": user_part, "assistant": assistant}


@router.get("/messages")
def list_messages(
    user: AdminUser,
    db: DbSession,
    deptId: Annotated[int | None, Query(ge=1)] = None,
    dateFrom: Annotated[dt.date | None, Query()] = None,
    dateTo: Annotated[dt.date | None, Query()] = None,
    hasFeedback: Annotated[bool | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """提问记录列表（F-701）：以 user 消息行为单位（role='user' AND is_eval=false），
    按部门 / 日期范围（含首尾日，UTC 边界）/ 是否点踩（回答 feedback='down'）筛选，
    created_at DESC（同刻 id 降序兜底）。回答三字段（answerType/hasCitations/feedback）
    取自配对回答；无回答/中断的问题记 None/false。"""
    total, records = list_question_records(
        db,
        QuestionFilters(
            dept_id=deptId,
            date_from=dateFrom,
            date_to=dateTo,
            has_feedback=hasFeedback,
            page=page,
            page_size=pageSize,
        ),
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [_record_payload(record) for record in records],
            "total": total,
            "page": page,
            "pageSize": pageSize,
        },
    }


@router.get("/messages/{message_id}")
def get_message_detail(message_id: int, user: AdminUser, db: DbSession) -> dict:
    """提问详情（F-701）：user 段（提问者身份 + 完整问题原文）+ assistant 段（回答
    全字段：citations JSONB 原样、model/tokens/latency/cacheHit、反馈三列）；无有效
    回答（未答完即中断——占位行被软删）assistant 为 null。非 user 行 / 评测消息 /
    不存在 id → 404/3003 同文案。"""
    detail = get_question_detail(db, message_id)
    if detail is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    return {"code": 0, "message": "ok", "data": _detail_payload(detail)}
