r"""管理后台路由（M7 起）：/api/admin/*——admin 专属查询/处理端点的汇聚 router。

本文件是管理端路由唯一挂载点（Task 9 badcase / Task 10 检索测试 / Task 11 运营
概览陆续加入本 router，保持单文件 ≤300 行，超出时按域拆子 router）。全部端点过
AdminUser 门（依赖链：登录 → 首登改密 → 角色，401/2003、403/2004，§5.5）；提问
记录（F-701）与 badcase 队列（F-702）恒滤 is_eval（W3 约束：评测数据不污染运营
查询）。查询与配对逻辑在 services.admin_views / services.badcase_views，本层只做
参数接收与 DTO 组装（§1.2 分层）；DTO 键全 camelCase（§3.4），列表
{items,total,page,pageSize}（§3.5），响应体 {code,message,data}。
"""

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, model_validator

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
from app.services.badcase_views import (
    BadcaseFilters,
    BadcaseRecord,
    list_badcases,
    update_badcase,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_MSG_NOT_FOUND = "提问记录不存在"  # 非 user 行 / 评测消息 / 不存在 id 同文案（不给探测空间）
_BADCASE_NOT_FOUND = "点踩记录不存在"  # 不存在 / 非 down / 评测点踩同文案（不给探测空间）


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


_NOTE_MIN = 1  # §21.5：处理理由 strip 后 1-500 字（与 W2 点踩备注 comment 同一口径）
_NOTE_MAX = 500


class BadcaseFieldError(ValueError):
    """note 跨字段校验失败：携带 field 供 422 处理器映射字段名（同 feedback.py 的
    CHAT-09 机制：model_validator 的 loc 为空，字段名经 ctx.error.field 带出）。"""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class BadcasePatchRequest(BaseModel):
    """PATCH /api/admin/badcases/{message_id} 请求体：处置只收两个 admin 终态
    （'待处理' 仅由 W2 点踩入口产生，不提供重开回退；枚举外 422 字段级指名
    badcaseStatus）；note 只入审计不入 messages，规则随冻结基线 ADM-03（规格
    优先于计划，评审修复轮 1 裁决）：'已忽略' 必填理由、'已修语料' 可选，给了则
    strip 后 1-500。"""

    badcaseStatus: Literal["已修语料", "已忽略"]
    note: str | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "BadcasePatchRequest":
        """'已忽略' → note strip 后 1-500 必填（ADM-03：忽略必填理由，全空白等同
        未填）；'已修语料' → note 可选，给了同样校验 1-500；strip 后回写（落审计
        的是整洁文本）。"""
        if self.note is None:
            if self.badcaseStatus == "已忽略":
                raise BadcaseFieldError("note", "忽略必须填写处理理由")
            return self
        note = self.note.strip()
        if not _NOTE_MIN <= len(note) <= _NOTE_MAX:
            raise BadcaseFieldError("note", f"处理理由长度须在 {_NOTE_MIN}-{_NOTE_MAX} 字之间")
        self.note = note
        return self


def _badcase_payload(record: BadcaseRecord) -> dict:
    """队列项 DTO（§3.4 camelCase）：questionPreview 已在 service 截 50 字。"""
    return {
        "messageId": record.message_id,
        "createdAt": record.created_at.isoformat(),
        "deptCode": record.dept_code,
        "questionPreview": record.question_preview,
        "category": record.category,
        "comment": record.comment,
        "badcaseStatus": record.badcase_status,
    }


@router.get("/badcases")
def list_badcase_queue(
    user: AdminUser,
    db: DbSession,
    deptId: Annotated[int | None, Query(ge=1)] = None,
    badcaseStatus: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """badcase 队列（F-702）：feedback='down' AND is_eval=false 的消息 DESC（同刻
    id 降序兜底），一条回答一条记录不重复；badcaseStatus 缺省不过滤（brief 钉死：
    '待处理' 由前端传，默认视图由前端带参）；item 携 questionPreview（截 50 字）
    与用户点踩 category/comment。"""
    total, records = list_badcases(
        db,
        BadcaseFilters(
            dept_id=deptId,
            badcase_status=badcaseStatus,
            page=page,
            page_size=pageSize,
        ),
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [_badcase_payload(record) for record in records],
            "total": total,
            "page": page,
            "pageSize": pageSize,
        },
    }


@router.patch("/badcases/{message_id}")
def patch_badcase(
    message_id: int, body: BadcasePatchRequest, user: AdminUser, db: DbSession
) -> dict:
    """badcase 处置（F-702）：badcase_status 迁移至 admin 终态（枚举外 422 字段
    级）；note 入审计 detail 不入 messages；审计 badcase.update {messageId,from,
    to,note} 与状态更新同事务落库（services 层 commit）。寻址宇宙 = 队列口径
    （down 且非评测），宇宙外 404/3003 同文案（不给探测空间）。"""
    updated = update_badcase(db, message_id, user.id, body.badcaseStatus, body.note)
    if updated is None:
        raise ApiError(ERR_NOT_FOUND, _BADCASE_NOT_FOUND, 404)
    return {
        "code": 0,
        "message": "ok",
        "data": {"messageId": updated, "badcaseStatus": body.badcaseStatus},
    }
