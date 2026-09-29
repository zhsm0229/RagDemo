r"""SSE 问答流端点（Task 9）：POST /api/chat/stream —— 薄控制器 + 请求阶段落库时序。

时序（brief 钉死）：①参数校验（strip 后 1-2000，422/1003 字段级）→ ②每日限额
（counter.try_consume 每请求恰一次，改写/生成等内部调用不计数；429/3002 未开流普通
JSON）→ ③会话归属（conversationId 空新建，否则校验本人且 is_active，404/3003）→
④事务①：user 消息 + assistant 占位行（content=''，nextval 发号）+ 首问标题 +
updated_at，commit 拿双 id 再开流 → ⑤history（最近 3 轮严格轮次对）+ prepare_turn →
⑥StreamingResponse：事件编排与收尾落库在 services/chat_stream（citations 恒先于
token；流中断裂 → mark_interrupted → error 事件，CHAT-12 不重复落库），本模块只做
SSE 线格式化（`event: {name}\ndata: {json}\n\n`，presentation 关切）。

连接红线：SSE 生成器自管短会话，绝不复用请求级会话横跨整个流（连接占用 60s 红线）；
请求阶段在构造 StreamingResponse 前对占位事务与 history/prepare 的读事务各 commit
一次（事务①提交、prepare 后再提交终结 autobegin 的读事务），连接即还池——在途 SSE
流期间请求级会话不占池中连接，依赖 teardown 在响应发完后才执行，不能依赖它释放。
测试经 monkeypatch 把 services.chat_stream.SessionLocal 指向测试连接
（tests/api/conftest.py chat_client），使生成器短会话与用例读回同处测试事务。
"""

import json
from collections.abc import Iterator
from time import perf_counter

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DbSession, ReadyUser
from app.core.clock import get_clock
from app.core.errors import ERR_DAILY_LIMITED, ERR_INTERNAL, ApiError
from app.core.llm import UpstreamError
from app.db.models import Conversation, Department, Message, User
from app.services.chat import ChatDeps, prepare_turn, resolve_owned_conversation
from app.services.chat_stream import event_stream, interrupt_turn

router = APIRouter(prefix="/chat", tags=["chat"])

_QUESTION_MAX_CHARS = 2000  # §21.5：问题 strip 后 1-2000 字
_ALL_DEPT_CODE = "all"  # 全员部门 code（F-502：检索范围恒为 [用户部门, 全员]）
_MSG_DAILY_LIMITED = "今日提问次数已达上限，请明天再试"
_MSG_INTERRUPTED = "系统繁忙，请稍后重试"


class ChatStreamRequest(BaseModel):
    """POST /api/chat/stream 请求体（camelCase，§3.4）：conversationId 空=新建会话。"""

    conversationId: int | None = None
    question: str

    @field_validator("question")
    @classmethod
    def _strip_and_check_length(cls, value: str) -> str:
        """strip 后 1-2000 字（§21.5 字段级 422/1003）：全空白等价空问题一并拒绝。"""
        stripped = value.strip()
        if not 1 <= len(stripped) <= _QUESTION_MAX_CHARS:
            raise ValueError("问题长度须在 1-2000 字之间")
        return stripped


def _format_stream(events: Iterator[tuple]) -> Iterator[str]:
    """SSE 事件线格式（brief 钉死）：ensure_ascii=False 保中文原样，事件间空行分隔。"""
    for name, payload in events:
        yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _resolve_conversation(
    db: Session, user: User, conversation_id: int | None, question: str
) -> Conversation:
    """conversationId 空 → 新建会话（首问标题=question[:20]）；否则校验归属本人且
    is_active（共享 services.chat.resolve_owned_conversation，Task 10 会话管理端点
    同用同语义）：404/3003 不区分"不存在"与"无权"，不给探测空间，IDOR §21.8。
    部门过滤豁免说明：个人会话仅属一人，user_id 归属校验严格强于 dept 过滤。"""
    if conversation_id is None:
        conversation = Conversation(user_id=user.id, dept_id=user.dept_id, title=question[:20])
        db.add(conversation)
        db.flush()
        return conversation
    return resolve_owned_conversation(db, user, conversation_id)


def _persist_placeholder(
    db: Session, conversation: Conversation, user: User, question: str
) -> tuple[int, int]:
    """事务①（brief 第 4 步）：user 消息 + assistant 占位行 + 会话 updated_at，commit
    后返回 (user 消息 id, assistant 占位 id)。占位行开流前落定，assistant content 留空
    待流式收尾二选一（finalize 或 mark_interrupted），两分支都不会再 INSERT 消息。"""
    user_msg = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="user",
        content=question,
    )
    assistant = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="assistant",
        content="",
    )
    db.add_all([user_msg, assistant])
    conversation.updated_at = get_clock().now()
    db.commit()  # flush 取 nextval 发号 id；提交后占位行对生成器短会话可见
    return user_msg.id, assistant.id


def _load_history(
    db: Session, conversation_id: int, current_user_msg_id: int
) -> list[tuple[str, str]]:
    """最近 3 轮（6 条）历史（Task 9 强制移交①）：created_at DESC LIMIT 6 再 reversed。

    id < current_user_msg_id 排除本轮 user 消息（prepare 的 question 另传，不得重复进
    history）；is_active 过滤中断占位行；同事务 now() 同刻以 id 兜底排序；奇数条（上轮
    断流留下悬空 user）丢弃最新一条，保证严格 (user, assistant) 轮次对。部门过滤豁免
    说明：会话归属已在上游 _resolve_conversation 校验，本查询随之继承本人边界。
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


def _all_dept_id(db: Session) -> int:
    """全员部门 id（F-502）：种子幂等存在，缺失属环境装配错误按 500 兜底拒绝。"""
    dept_id = db.scalar(select(Department.id).where(Department.code == _ALL_DEPT_CODE))
    if dept_id is None:
        raise ApiError(ERR_INTERNAL, _MSG_INTERRUPTED, 500)
    return dept_id


@router.post("/stream")
def chat_stream(
    body: ChatStreamRequest, user: ReadyUser, db: DbSession, request: Request
) -> StreamingResponse:
    """问答流式端点：请求阶段完成限额/落库/计划（brief 第 1-5 步），生成器只管流与收尾。"""
    deps: ChatDeps = request.app.state.chat_deps
    if not deps.counter.try_consume(user.id, deps.settings.daily_question_limit):  # 每请求恰一次
        raise ApiError(ERR_DAILY_LIMITED, _MSG_DAILY_LIMITED, 429)
    conversation = _resolve_conversation(db, user, body.conversationId, body.question)
    user_msg_id, assistant_msg_id = _persist_placeholder(db, conversation, user, body.question)
    history = _load_history(db, conversation.id, user_msg_id)
    try:
        plan = prepare_turn(db, deps, user.dept_id, _all_dept_id(db), body.question, history)
    except UpstreamError:
        interrupt_turn(assistant_msg_id)  # CHAT-12：事务①已提交，不留 is_active=true 幽灵占位
        raise  # 开流前失败：交全局 500 兜底（话术脱敏不变），事务随依赖 teardown 回滚
    db.commit()  # 终结 history/prepare 的 autobegin 读事务：连接还池，流期间不占连接
    return StreamingResponse(
        _format_stream(event_stream(deps, plan, assistant_msg_id, perf_counter())),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
