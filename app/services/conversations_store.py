r"""会话与消息存取/归属解析（自 chat.py 拆出，行数红线 §1.4）：列表、历史消息、落库收尾、IDOR 归属闸。

问答编排（prepare_turn）仍在 chat.py；本模块承载"会话/消息的查询、落库收尾与归属
解析"——api/conversations、api/chat、api/chat_stream、api/feedback 共用（chat.py
转出再导出，既有导入路径不变；finalize/mark_interrupted/load_recent_history 系
metrics-balance 03 因 chat.py 触及 300 行红线转出）。归属校验一律"不存在与无权
同判 404/3003"（IDOR 防线 §21.8）。
"""

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Conversation, Message, User

MSG_CONVERSATION_NOT_FOUND: str = "会话不存在或无权访问"  # 404/3003 对外文案（归属/软删共用）
MSG_MESSAGE_NOT_FOUND: str = "消息不存在或无权访问"  # 反馈归属失败对外文案（F-208）


def load_recent_history(
    db: Session, conversation_id: int, current_user_msg_id: int
) -> list[tuple[str, str]]:
    """最近 3 轮（6 条）历史（W2 自 api/chat.py 下沉，行为逐字保留；metrics-balance 03
    因 chat.py 行数红线 §1.4 转出至此）：created_at DESC LIMIT 6 再 reversed。

    id < current_user_msg_id 排除本轮 user 消息（prepare 的 question 另传，不得重复进
    history）；is_active 过滤中断占位行；同事务 now() 同刻以 id 兜底排序；奇数条（上轮
    断流留下悬空 user）丢弃最新一条，保证严格 (user, assistant) 轮次对。部门过滤豁免
    说明：会话归属已在上游校验（resolve_owned_conversation），本查询随之继承本人边界。
    """
    rows = db.execute(
        select(Message.role, Message.content)
        .where(
            Message.conversation_id == conversation_id,
            Message.id < current_user_msg_id,
            Message.is_active.is_(True),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(6)
    ).all()
    if len(rows) % 2 == 1:
        rows = rows[1:]
    return [(role, content) for role, content in reversed(rows)]


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


def finalize_turn(
    db: Session,
    assistant_msg_id: int,
    *,
    content: str,
    citations: list[dict],
    answer_type: str,
    model: str,
    usage: tuple[int, int] | None,
    latency_ms: int,
    cache_hit: bool,
    degraded: bool = False,
) -> None:
    """收尾占位 assistant 消息：纯 UPDATE + flush，commit 归调用方（Task 9）。

    usage 即 provider.last_usage，None（拒答未调 LLM 等）写 0；answer_type 取值
    'rag'/'qa'/'refuse'（CHECK 约束）——cache 计划由 Task 9 取缓存条目内的原
    answer_type，不得传 'cache'。缓存 put 同归 Task 9 在 finalize 后执行。
    degraded（迁移 0012，retrieval-policy §2）：检索上游降级轮标注，缺省 False
    （正常轮/缓存命中轮零改动；降级计划不写缓存故 cache 命中轮恒 False）。
    """
    prompt_tokens, completion_tokens = usage if usage is not None else (0, 0)
    db.execute(
        update(Message)
        .where(Message.id == assistant_msg_id)
        .values(
            content=content,
            citations=citations,
            answer_type=answer_type,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            degraded=degraded,
        )
    )
    db.flush()


def mark_interrupted(db: Session, assistant_msg_id: int) -> None:
    """中断收尾（CHAT-12）：只置 is_active=false 标失败，不重复落库；flush 不 commit。"""
    db.execute(update(Message).where(Message.id == assistant_msg_id).values(is_active=False))
    db.flush()
