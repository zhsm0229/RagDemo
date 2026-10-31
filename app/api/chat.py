r"""SSE 问答流端点（Task 9）：POST /api/chat/stream —— 薄控制器 + 请求阶段落库时序。

时序（brief 钉死）：①参数校验（strip 后 1-2000，422/1003 字段级）→ ②每日限额
（counter.try_consume 每请求恰一次，改写/生成等内部调用不计数；429/3002 未开流普通
JSON；其前的令牌桶限流见工单 21，总闸缺省关）→ ③会话归属（conversationId 空新建，
否则校验本人且 is_active，404/3003；
复用会话若仍是默认题"新会话"/空以首问补题，二轮起已命题不再改，W3 Task 1）→
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
（tests/api/conftest.py chat_client）。

长期记忆抽取（F-210 §3.2，agent-evolution）：qa/rag/refuse 计划在构造 StreamingResponse
前挂 BackgroundTasks（A7：拒答轮用户问题同样可能携带画像信息，refused 标志随任务
传入）——响应发完后由任务体（memory_extract.extract_memory_task，自管 SessionLocal
短会话）按 assistant_msg_id 读回收尾后的回答再抽取，cache 命中不挂任务；端点阶段
回答尚未生成，"test_mode 同步执行"（documents.py 先例）在此不适用——同步跑在端点内
读到的是空占位正文；测试可依赖 TestClient 同步驱动完整 ASGI 生命周期（后台任务在
响应体发完后、client.post 返回前执行完毕），可断言性与同步模式等价。
"""

import json
import math
from collections.abc import Iterator
from time import perf_counter

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.api.deps import DbSession, ReadyUser
from app.core.clock import get_clock
from app.core.errors import ERR_DAILY_LIMITED, ERR_RATE_LIMITED, ApiError
from app.core.llm import UpstreamError
from app.db.models import Conversation, Message, User
from app.services.chat import (
    ChatDeps,
    load_recent_history,
    prepare_turn,
    resolve_owned_conversation,
)
from app.services.chat_stream import event_stream, interrupt_turn
from app.services.departments import all_dept_id
from app.services.memory_extract import extract_memory_task

router = APIRouter(prefix="/chat", tags=["chat"])

_QUESTION_MAX_CHARS = 2000  # §21.5：问题 strip 后 1-2000 字
_MSG_DAILY_LIMITED = "今日提问次数已达上限，请明天再试"
_MSG_RATE_LIMITED = "操作过于频繁，请稍后重试"  # 令牌桶限流文案（工单 21，与日配额分档）


def _retry_after(rate_per_min: int) -> int:
    """补足一个令牌的最小时长（秒）：max(1, ceil(60/rate))——配置推导的保守提示而非
    精确水位倒推（协议保持 bool 纯函数，design/21 §7.3）。"""
    return max(1, math.ceil(60 / rate_per_min))


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


_DEFAULT_TITLES = ("新会话", "")  # DB server_default 与历史空标题：复用会话首问补题的触发值


def _resolve_conversation(
    db: Session, user: User, conversation_id: int | None, question: str
) -> Conversation:
    """conversationId 空 → 新建会话（首问标题=question[:20]）；否则校验归属本人且
    is_active（共享 services.chat.resolve_owned_conversation，Task 10 会话管理端点
    同用同语义）：404/3003 不区分"不存在"与"无权"，不给探测空间，IDOR §21.8。
    复用会话若仍是默认题（"新会话"/空）以首问补题（W3 Task 1 改题语义）：ORM 变更
    随事务①一并提交，二轮起标题已非默认值不再改题。部门过滤豁免说明：个人会话仅属
    一人，user_id 归属校验严格强于 dept 过滤。"""
    if conversation_id is None:
        conversation = Conversation(user_id=user.id, dept_id=user.dept_id, title=question[:20])
        db.add(conversation)
        db.flush()
        return conversation
    conversation = resolve_owned_conversation(db, user, conversation_id)
    if conversation.title in _DEFAULT_TITLES:
        conversation.title = question[:20]  # 首问补题：随 _persist_placeholder 的事务①提交
    return conversation


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


@router.post("/stream")
def chat_stream(
    body: ChatStreamRequest,
    user: ReadyUser,
    db: DbSession,
    request: Request,
    background: BackgroundTasks,
) -> StreamingResponse:
    """问答流式端点：请求阶段完成限额/落库/计划（brief 第 1-5 步），生成器只管流与收尾。"""
    deps: ChatDeps = request.app.state.chat_deps
    # 令牌桶限流（工单 21，三层第 2 层）：先于日配额——桶拒绝不烧配额、不落占位行；
    # bucket 缺省 None 兜底（PG 分支未装配），总闸关时短路行为与现状逐字节一致
    if (
        deps.settings.rate_limiter_enabled
        and deps.bucket is not None
        and not deps.bucket.try_acquire(
            "chat", str(user.id), deps.settings.rate_chat_per_min, deps.settings.rate_chat_burst
        )
    ):
        raise ApiError(
            ERR_RATE_LIMITED,
            _MSG_RATE_LIMITED,
            429,
            data={"retryAfterSec": _retry_after(deps.settings.rate_chat_per_min)},
        )
    if not deps.counter.try_consume(user.id, deps.settings.daily_question_limit):  # 每请求恰一次
        raise ApiError(ERR_DAILY_LIMITED, _MSG_DAILY_LIMITED, 429)
    conversation = _resolve_conversation(db, user, body.conversationId, body.question)
    user_msg_id, assistant_msg_id = _persist_placeholder(db, conversation, user, body.question)
    history = load_recent_history(db, conversation.id, user_msg_id)
    try:
        plan = prepare_turn(
            db, deps, user.dept_id, all_dept_id(db), body.question, history, user_id=user.id
        )
    except UpstreamError:
        interrupt_turn(assistant_msg_id)  # CHAT-12：事务①已提交，不留 is_active=true 幽灵占位
        raise  # 开流前失败：交全局 500 兜底（话术脱敏不变），事务随依赖 teardown 回滚
    if plan.kind in ("qa", "rag", "refuse"):
        # 长期记忆抽取（F-210 §3.2；A7：refuse 轮用户问题同样可能携带画像信息，如
        # "记住我喜欢简洁回答"）：qa/rag/refuse 挂后台任务，cache 命中不抽取；任务体
        # 自管短会话读回收尾后的回答执行（响应发完后运行，失败静默不影响主链路）；
        # refuse 轮回答正文是逐字拒答话术，任务体按 refused 标志以声明文字替代回答段
        background.add_task(
            extract_memory_task,
            deps,
            user.id,
            assistant_msg_id,
            body.question,
            refused=plan.kind == "refuse",
        )
    db.commit()  # 终结 history/prepare 的 autobegin 读事务：连接还池，流期间不占连接
    return StreamingResponse(
        _format_stream(event_stream(deps, plan, assistant_msg_id, perf_counter())),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
