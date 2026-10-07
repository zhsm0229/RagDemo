r"""会话与消息存取/归属解析（自 chat.py 拆出，行数红线 §1.4）：列表、历史消息、IDOR 归属闸。

问答编排（prepare_turn/finalize）仍在 chat.py；本模块只承载"会话/消息的查询与归属
解析"——api/conversations、api/chat、api/feedback 三端点共用（chat.py 转出再导出，
既有导入路径不变）。归属校验一律"不存在与无权同判 404/3003"（IDOR 防线 §21.8）。
"""

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Conversation, Message, User

MSG_CONVERSATION_NOT_FOUND: str = "会话不存在或无权访问"  # 404/3003 对外文案（归属/软删共用）
MSG_MESSAGE_NOT_FOUND: str = "消息不存在或无权访问"  # 反馈归属失败对外文案（F-208）


def list_user_conversations(
    db: Session, user_id: int, page: int, page_size: int
) -> tuple[list[Conversation], int]:
    """本人活跃会话分页列表（F-207，自 api/conversations.py 下沉）：updated_at DESC
    （同刻 id 降序兜底），返回 (当前页行, 总数)；user_id 归属过滤即隔离（个人资源）。"""
    stmt = select(Conversation).where(
        Conversation.user_id == user_id, Conversation.is_active.is_(True)
    )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        db.execute(
            stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return list(rows), total


def list_conversation_messages(db: Session, conversation_id: int) -> list[Message]:
    """会话历史消息 ASC（F-207，自 api/conversations.py 下沉）：中断占位行
    （CHAT-12 遗留形态 is_active=false）不进列表，user 消息全返回。"""
    return list(
        db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                or_(Message.role == "user", Message.is_active.is_(True)),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
        .scalars()
        .all()
    )


def resolve_own_answer(db: Session, user_id: int, message_id: int) -> Message:
    """反馈归属解析（W2 自 api/feedback.py 下沉）：本人 + assistant + is_active。

    任一不满足与不存在同判 404/3003（不给探测空间，IDOR §21.8；user 消息与中断
    占位行不可反馈）。按 id 过滤而非 db.get——messages 是 (id, created_at) 复合主键，
    单值 db.get 无法构形。"""
    message = db.scalar(select(Message).where(Message.id == message_id))
    if message is None or message.user_id != user_id or message.role != "assistant":
        raise ApiError(ERR_NOT_FOUND, MSG_MESSAGE_NOT_FOUND, 404)
    if not message.is_active:
        raise ApiError(ERR_NOT_FOUND, MSG_MESSAGE_NOT_FOUND, 404)
    return message


def resolve_owned_conversation(db: Session, user: User, conversation_id: int) -> Conversation:
    """按 id 解析本人且 is_active 的会话，否则 404/3003（问答端点与会话管理端点共享）。

    不区分"不存在"与"无权"，不给探测空间（IDOR 防线 §21.8）；部门过滤豁免说明：
    个人会话仅属一人，user_id 归属校验严格强于 dept 过滤（api/chat.py 同注先例）。
    Task 10 自 api/chat.py._resolve_conversation 提取共享，两处行为必须一致。
    """
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id or not conversation.is_active:
        raise ApiError(ERR_NOT_FOUND, MSG_CONVERSATION_NOT_FOUND, 404)
    return conversation
