r"""会话管理路由（Task 10，F-207）：列表 / 新建 / 历史消息 / 软删——薄控制器。

会话是个人资源：全部端点过 ReadyUser 门后按 user_id 归属过滤（部门过滤豁免说明：
个人会话仅属一人，user_id 校验严格强于 dept 过滤，与 services.chat 的共享解析
resolve_owned_conversation 同注同语义）。列表 §22 默认序 updated_at DESC（同刻
id 降序兜底），分页 §3.5（page ≥1、pageSize 1-100 默认 20，Pydantic Query 校验
非法 422/1003）；历史消息 ASC，assistant is_active=false 的中断占位行（CHAT-12
遗留形态）不进列表，user 消息全返回；删除为软删（is_active=false，不物理删），
重复删除幂等 200，他人会话一律 404/3003（IDOR 防线 §21.8）。DTO 键全 camelCase
（§3.4），响应体 {code, message, data}。
"""

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, or_, select

from app.api.deps import DbSession, ReadyUser
from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Conversation, Message
from app.services.chat import MSG_CONVERSATION_NOT_FOUND, resolve_owned_conversation

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _conversation_payload(conversation: Conversation) -> dict:
    """列表项 DTO（§3.4 camelCase）：数据库行不直接返回。"""
    return {
        "id": conversation.id,
        "title": conversation.title,
        "updatedAt": conversation.updated_at.isoformat(),
    }


def _message_payload(message: Message) -> dict:
    """历史消息项 DTO（§3.4）：citations 为 JSONB 原样透传（user 消息恒 None）。"""
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "citations": message.citations,
        "feedback": message.feedback,
        "feedbackCategory": message.feedback_category,
        "feedbackComment": message.feedback_comment,
        "answerType": message.answer_type,
        "cacheHit": message.cache_hit,
        "createdAt": message.created_at.isoformat(),
    }


@router.get("")
def list_conversations(
    user: ReadyUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """本人活跃会话列表（§22）：updated_at DESC（同刻 id 降序兜底），分页切片。"""
    stmt = select(Conversation).where(
        Conversation.user_id == user.id, Conversation.is_active.is_(True)
    )
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        db.execute(
            stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .offset((page - 1) * pageSize)
            .limit(pageSize)
        )
        .scalars()
        .all()
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [_conversation_payload(row) for row in rows],
            "total": total or 0,
            "page": page,
            "pageSize": pageSize,
        },
    }


@router.post("")
def create_conversation(user: ReadyUser, db: DbSession) -> dict:
    """新建空会话：title 缺省走 DB 默认"新会话"；仅问答端点自建会话时以首问前 20 字
    命题，复用已有会话标题不变（chat.py）。"""
    conversation = Conversation(user_id=user.id, dept_id=user.dept_id)
    db.add(conversation)
    db.flush()  # 先发号取 id：commit 后不再触碰过期属性
    conversation_id = conversation.id
    db.commit()
    return {"code": 0, "message": "ok", "data": {"id": conversation_id}}


@router.get("/{conversation_id}/messages")
def list_messages(conversation_id: int, user: ReadyUser, db: DbSession) -> dict:
    """会话历史消息（ASC）：归属校验 404/3003；中断占位行不出现，user 消息全返回。"""
    conversation = resolve_owned_conversation(db, user, conversation_id)
    rows = (
        db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                or_(Message.role == "user", Message.is_active.is_(True)),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
        .scalars()
        .all()
    )
    return {"code": 0, "message": "ok", "data": {"items": [_message_payload(row) for row in rows]}}


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: int, user: ReadyUser, db: DbSession) -> dict:
    """软删（F-207）：is_active=false 不物理删；重复删除幂等 200；他人会话 404/3003。

    归属边界与共享解析同为 user_id，但语义不同：已软删的本人会话"再删"仍算成功
    （幂等），故此处直接查行，不复用要求 is_active=true 的 resolve_owned_conversation。
    """
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise ApiError(ERR_NOT_FOUND, MSG_CONVERSATION_NOT_FOUND, 404)
    conversation.is_active = False
    db.commit()
    return {"code": 0, "message": "ok", "data": {"id": conversation_id, "isActive": False}}
