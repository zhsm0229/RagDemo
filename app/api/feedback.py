r"""消息反馈端点（Task 11，F-208）：POST /api/messages/{id}/feedback —— 点赞/点踩。

点踩是 badcase 治理闭环（F-702/F4）的入口：down 落 feedback/feedback_comment/
feedback_category 三列并置 badcase_status='待处理' 进治理队列；up（点赞）忽略随附
备注/类型置 None、不动 badcase_status。改评语义（原型无取消反馈，允许改评）：
down→up 清空备注与类型，但 badcase_status 保持'待处理'不回退——已进治理队列的
记录不因改评消失；重复 down 幂等更新为最新值（允许改备注/类型，队列状态重回
'待处理'）。

归属校验（IDOR §21.8）：消息 user_id==当前用户 且 role=='assistant' 且
is_active=true（user 消息与中断占位行不可反馈），违规与不存在一律 404/3003 同文案，
不区分"不存在"与"无权"。跨字段校验（down → category ∈ 枚举、comment strip 后
1-500 必填）由 model_validator 抛 FeedbackFieldError：Pydantic 对 model_validator
的 value_error 不带字段级 loc，字段名经异常 .field 属性由 422 处理器带出
（app/core/errors.py，CHAT-09）。审计 CHAT-10：action=message.feedback，detail 不含
comment 原文——用户文本已在 messages 列，审计不重复存储。
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DbSession, ReadyUser
from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Message, User
from app.services.audit import record_audit

router = APIRouter(prefix="/messages", tags=["messages"])

_MSG_NOT_FOUND = "消息不存在或无权访问"
_COMMENT_MIN = 1  # §21.5：点踩备注 strip 后 1-500 字
_COMMENT_MAX = 500
_BADCASE_PENDING = "待处理"  # F-702 治理队列初始状态（messages.badcase_status 枚举）
_CATEGORIES = ("答案错误", "引用无关", "拒答不当", "其他")  # F-208 点踩类型枚举


class FeedbackFieldError(ValueError):
    """跨字段校验失败：携带 field 供 422 处理器映射字段名（model_validator loc 为空）。"""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class FeedbackRequest(BaseModel):
    """POST /api/messages/{id}/feedback 请求体：down 必带 category（枚举）与
    comment（strip 后 1-500）；up 时二者忽略置 None。"""

    feedback: Literal["up", "down"]
    category: str | None = None
    comment: str | None = None

    @model_validator(mode="after")
    def _normalize(self) -> "FeedbackRequest":
        """down → comment strip 后 1-500 必填、category 必须在枚举内（先备注后类型，
        两者皆缺时 422 先指名 comment）；up → 二者忽略置 None。"""
        if self.feedback == "up":
            self.category = None
            self.comment = None
            return self
        comment = (self.comment or "").strip()
        if not _COMMENT_MIN <= len(comment) <= _COMMENT_MAX:
            raise FeedbackFieldError(
                "comment", f"备注长度须在 {_COMMENT_MIN}-{_COMMENT_MAX} 字之间"
            )
        if self.category not in _CATEGORIES:
            raise FeedbackFieldError("category", "反馈类型须为 " + "、".join(_CATEGORIES) + " 之一")
        self.comment = comment
        return self


def _owned_answer(db: Session, user: User, message_id: int) -> Message:
    """归属解析：本人 + assistant + is_active，任一不满足与不存在同判 404/3003
    （不给探测空间，IDOR §21.8；user 消息不可反馈）。"""
    message = db.scalar(select(Message).where(Message.id == message_id))
    if (
        message is None
        or message.user_id != user.id
        or message.role != "assistant"
        or not message.is_active
    ):
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    return message


@router.post("/{message_id}/feedback")
def leave_feedback(message_id: int, body: FeedbackRequest, user: ReadyUser, db: DbSession) -> dict:
    """消息反馈（F-208）：down 三列落库并入 badcase 队列（'待处理'）；up 忽略备注/
    类型。改评不回退：down→up 清备注/类型、badcase_status 保持'待处理'；重复 down
    幂等更新为最新值。审计 detail 不含 comment 原文；事务边界：反馈与审计同事务
    commit。"""
    message = _owned_answer(db, user, message_id)
    message.feedback = body.feedback
    message.feedback_comment = body.comment
    message.feedback_category = body.category
    if body.feedback == "down":  # F-702 治理队列入口；改评（up）不触碰该列不回退
        message.badcase_status = _BADCASE_PENDING
    detail: dict = {"feedback": body.feedback}
    if body.feedback == "down":
        detail["category"] = body.category
    record_audit(db, user.id, "message.feedback", "message", message.id, detail)
    payload = {  # commit 前取值（对齐 conversations.py 先例，不触碰过期属性）
        "id": message.id,
        "feedback": message.feedback,
        "feedbackCategory": message.feedback_category,
        "feedbackComment": message.feedback_comment,
        "badcaseStatus": message.badcase_status,
    }
    db.commit()
    return {"code": 0, "message": "ok", "data": payload}
