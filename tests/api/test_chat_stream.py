r"""SSE 问答流端点测试（Task 9，brief ①-⑧ 逐条落地）：事件序 / DTO 字段 / 落库时序。

消费方式：TestClient 同步客户端整体缓冲响应体，按 `\n\n` 切事件解析 event/data 行
（_parse_sse），断言事件顺序精确、done 字段与消息读回逐项相等（§15.4 写库必读回）。
断流用 break_after=1 的 StubLLMProvider 替换 app.state.chat_deps.llm（lifespan 自建
Stub deps 后经 app.state 覆盖，W1 conftest 模式）；chat_client fixture 另把
app.api.chat.SessionLocal 指向测试连接——生成器短会话与用例读回同处测试事务
（commit 只释放 savepoint），测试结束整体回滚，不触真库其他连接。
"""

import hashlib
import json
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerReranker,
    set_rerank_breaker,
)
from app.core.clock import FakeClock
from app.core.errors import ERR_DAILY_LIMITED, ERR_NOT_FOUND, ERR_PARAM_INVALID
from app.core.llm import DEFAULT_CHAT_CHUNKS, StubEmbedder, StubLLMProvider, StubReranker, sigmoid
from app.db.models import Chunk, Conversation, Department, Document, Message, QaPair, User
from app.main import app
from app.rag.prompt import REFUSAL_TEXT
from app.services.chat import TurnPlan
from tests.conftest import login_as

QUESTION = "公司年假有多少天"
CHUNK_TEXT = "年假按工龄五到十天"
DOC_TITLE = "全员_考勤制度"


@pytest.fixture(autouse=True)
def _reset_rerank_breaker():
    """熔断器进程单例用例间隔离：进出各复位（防降级态泄漏影响后续用例）。"""
    set_rerank_breaker(None)
    yield
    set_rerank_breaker(None)


# ---------- 构造与消费辅助（口径同 tests/api/test_auth.py 与集成测试） ----------


def make_user(session: Session, username: str, password: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内；部门取全员（检索范围含自身部门）。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def post_chat(client, question: str, conversation_id: int | None = None):
    body = {"question": question}
    if conversation_id is not None:
        body["conversationId"] = conversation_id
    return client.post("/api/chat/stream", json=body)


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """响应体按空行切事件，逐块解析 event/data 行（data 为 JSON，中文原样）。"""
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = ""
        payload: dict = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        events.append((name, payload))
    return events


def _seed_rag_chunk(session: Session, user: User) -> Chunk:
    """直插 ready 文档 + 单 chunk（StubEmbedder 确定性向量与 deps 内 Stub 同算法同向量）。"""
    embedder = StubEmbedder()
    doc = Document(
        title=DOC_TITLE,
        dept_id=user.dept_id,
        original_filename=f"{DOC_TITLE}.docx",
        file_path=f"seed/{DOC_TITLE}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=hashlib.sha256(CHUNK_TEXT.encode("utf-8")).hexdigest(),
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        is_active=True,
        uploaded_by=user.id,
    )
    session.add(doc)
    session.flush()
    chunk = Chunk(
        document_id=doc.id,
        dept_id=user.dept_id,
        title_path=DOC_TITLE,
        content=CHUNK_TEXT,
        content_chars=len(CHUNK_TEXT),
        position=0,
        embedding=embedder.embed([CHUNK_TEXT])[0],
    )
    session.add(chunk)
    session.flush()
    return chunk


def _override_reranker_high() -> None:
    """lifespan 装配的默认 StubReranker 分数不可控（blake2b 派生可能低于拒答阈值）：
    注入高权重替身钉住 rag 路径（W1 conftest 的 app.state 覆盖模式）。"""
    app.state.chat_deps.reranker = StubReranker(weights=[2.0])


def _conversation_row(session: Session) -> Conversation:
    session.expire_all()  # 生成器短会话的收尾 UPDATE 不经过本 session 身份映射，强制重载
    return session.scalars(select(Conversation)).one()


def _messages(session: Session, conversation_id: int) -> list[Message]:
    session.expire_all()  # 同上：按 id 序读回全部消息（user/assistant 对）
    return list(
        session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
        ).all()
    )


# ---------- ① 正常 rag 全序：citations 先于 token，done 字段齐全，消息读回 ----------


def test_rag_stream_full_event_order_and_message_persisted(chat_client, session) -> None:
    """rag 流：事件序 citations→token×3→done；citations 先于首个 token；写库读回逐项相等。"""
    user = make_user(session, "raguser", "right-one", role="member")
    chunk = _seed_rag_chunk(session, user)
    _override_reranker_high()
    assert login_as(chat_client, "raguser", "right-one").status_code == 200

    resp = post_chat(chat_client, QUESTION)

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert [name for name, _ in events] == ["citations", "token", "token", "token", "done"]
    assert [payload["content"] for _, payload in events[1:4]] == list(DEFAULT_CHAT_CHUNKS)
    (user_row, assistant_row) = _messages(session, _conversation_row(session).id)
    assert (user_row.role, user_row.content, user_row.is_active) == ("user", QUESTION, True)
    expected_citation = {
        "n": 1,
        "refType": "doc",
        "refId": chunk.id,
        "docId": chunk.document_id,  # 文档主键：引用弹窗取原文件/图片按文档寻址（≠chunk id）
        "title": DOC_TITLE,
        "page": None,
        "titlePath": DOC_TITLE,
        "score": pytest.approx(sigmoid(2.0)),
    }
    assert events[0] == ("citations", {"messageId": assistant_row.id, "items": [expected_citation]})
    assert all(payload["messageId"] == assistant_row.id for _, payload in events[1:])
    assert assistant_row.content == "".join(DEFAULT_CHAT_CHUNKS)
    assert assistant_row.citations == events[0][1]["items"]  # JSONB 与 SSE DTO 同形态
    assert assistant_row.answer_type == "rag"
    assert (assistant_row.cache_hit, assistant_row.is_active) == (False, True)
    assert assistant_row.model == "stub"  # settings.llm_model 为空（Stub）的落库回退名
    assert assistant_row.prompt_tokens > 0 and assistant_row.completion_tokens > 0
    done = events[-1][1]
    assert done == {
        "messageId": assistant_row.id,
        "refused": False,
        "cacheHit": False,
        "promptTokens": assistant_row.prompt_tokens,
        "completionTokens": assistant_row.completion_tokens,
        "durationMs": assistant_row.latency_ms,
        "degraded": False,  # 正常 rag 轮恒 false 且字段不缺席（retrieval-policy §5）
    }


# ---------- ② 首问标题：新会话 title=question[:20] ----------


def test_first_question_sets_conversation_title_truncated(chat_client, session) -> None:
    """conversationId 为空新建会话：title 取首问前 20 字（长问题截断），读回验证。"""
    user = make_user(session, "titleuser", "right-one", role="member")
    assert login_as(chat_client, "titleuser", "right-one").status_code == 200
    question = "请告诉我公司年假的具体天数规定和申请流程说明"

    resp = post_chat(chat_client, question)

    assert resp.status_code == 200
    assert [name for name, _ in _parse_sse(resp.text)] == ["citations", "token", "done"]
    conversation = _conversation_row(session)
    assert conversation.title == question[:20]
    assert (conversation.user_id, conversation.dept_id) == (user.id, user.dept_id)  # 归属提问者


# ---------- ②b 复用会话首问改题：默认题补填 question[:20]，二轮不再改（W3 Task 1） ----------


def test_reused_conversation_retitle_from_first_question(chat_client, session) -> None:
    """复用 POST /api/conversations 所建会话（title=DB 默认"新会话"）发起首轮提问：
    事务①补题 question[:20]（长问题截断），读回验证。"""
    make_user(session, "reusetitle", "right-one", role="member")
    assert login_as(chat_client, "reusetitle", "right-one").status_code == 200
    created = chat_client.post("/api/conversations")
    assert created.status_code == 200
    conversation_id = created.json()["data"]["id"]
    question = "请告诉我公司年假的具体天数规定和申请流程说明"

    resp = post_chat(chat_client, question, conversation_id=conversation_id)

    assert resp.status_code == 200
    assert [name for name, _ in _parse_sse(resp.text)] == ["citations", "token", "done"]
    conversation = _conversation_row(session)
    assert conversation.id == conversation_id
    assert conversation.title == question[:20]


def test_second_question_does_not_retitle_reused_conversation(chat_client, session) -> None:
    """二轮提问不再改题：首轮已把默认题补成首问前 20 字，二轮问题不同也不覆盖。"""
    make_user(session, "reusetitle2", "right-one", role="member")
    assert login_as(chat_client, "reusetitle2", "right-one").status_code == 200
    created = chat_client.post("/api/conversations")
    conversation_id = created.json()["data"]["id"]
    first_question = "公司年假有多少天"

    first = post_chat(chat_client, first_question, conversation_id=conversation_id)
    assert first.status_code == 200
    second = post_chat(chat_client, "那病假怎么算", conversation_id=conversation_id)

    assert second.status_code == 200
    conversation = _conversation_row(session)
    assert conversation.title == first_question[:20]  # 保持首轮命题，未被二轮覆盖


# ---------- ③ 拒答序：citations(items=[])→token(精确话术)→done(refused=true) ----------


def test_refuse_stream_exact_order_and_refusal_text(chat_client, session) -> None:
    """空知识库拒答：citations 空列表也发；token 逐字 REFUSAL_TEXT；done refused=true。"""
    make_user(session, "refuseuser", "right-one", role="member")
    assert login_as(chat_client, "refuseuser", "right-one").status_code == 200

    resp = post_chat(chat_client, "完全无关的天书问题")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert [name for name, _ in events] == ["citations", "token", "done"]
    (_, assistant_row) = _messages(session, _conversation_row(session).id)
    assert events[0] == ("citations", {"messageId": assistant_row.id, "items": []})
    assert events[1] == ("token", {"messageId": assistant_row.id, "content": REFUSAL_TEXT})
    assert events[2][1]["refused"] is True and events[2][1]["cacheHit"] is False
    assert events[2][1]["degraded"] is False  # 常规拒答（知识不足）非降级轮：字段不缺席
    assert (events[2][1]["promptTokens"], events[2][1]["completionTokens"]) == (0, 0)
    assert events[2][1]["durationMs"] >= 0
    assert (assistant_row.content, assistant_row.answer_type) == (REFUSAL_TEXT, "refuse")
    assert assistant_row.citations == []
    assert (assistant_row.cache_hit, assistant_row.is_active) == (False, True)


# ---------- ④ 断流：error 5001 retryable、占位行 is_active=false、重试新对（CHAT-12） ----------


def test_interrupted_stream_marks_placeholder_inactive_and_retry_creates_new_pair(
    chat_client, session
) -> None:
    """break_after=1 断流：error 事件 code=5001 retryable=true；占位行置 is_active=false
    不重复落库；重试（再次请求）产生新消息对且旧行保持原样（CHAT-12）。"""
    make_user(session, "breakuser", "right-one", role="member")
    _seed_rag_chunk(
        session, session.scalars(select(User).where(User.username == "breakuser")).one()
    )
    _override_reranker_high()
    assert login_as(chat_client, "breakuser", "right-one").status_code == 200
    app.state.chat_deps.llm = StubLLMProvider(break_after=1)

    resp = post_chat(chat_client, QUESTION)

    assert resp.status_code == 200  # 响应头已发出：断流只能经 SSE error 事件表达
    events = _parse_sse(resp.text)
    assert [name for name, _ in events] == ["citations", "token", "error"]
    conversation = _conversation_row(session)
    (_, broken_row) = _messages(session, conversation.id)
    assert events[-1][1] == {
        "code": 5001,
        "message": "生成服务暂时不可用，请重试",
        "retryable": True,
    }
    assert (broken_row.content, broken_row.answer_type) == ("", None)  # 占位未被覆写
    assert broken_row.is_active is False  # 中断标记，不重复落库

    app.state.chat_deps.llm = StubLLMProvider()  # 恢复正常替身后重试：前端重新 POST 语义
    retry = post_chat(chat_client, "那病假呢", conversation_id=conversation.id)

    assert retry.status_code == 200
    assert [name for name, _ in _parse_sse(retry.text)] == [
        "citations",
        "token",
        "token",
        "token",
        "done",
    ]
    rows = _messages(session, conversation.id)
    assert [row.role for row in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[0].id == broken_row.id - 1 and rows[1].id == broken_row.id  # 旧行原地不动
    assert (rows[1].content, rows[1].is_active) == ("", False)  # 旧占位保持失败态


# ---------- ④b 空正文防护（2026-10-01 事故）：reasoning 烧尽预算 → 零正文按可重试失败 ----------


def test_empty_llm_output_treated_as_retryable_failure(chat_client, session) -> None:
    """LLM 流正常耗尽但零正文（推理模型 reasoning 烧尽 max_tokens 的事故形态）：
    必须转 error(5001, retryable) 且占位行中断标记——严禁空串当正常回答落库/进缓存。"""
    make_user(session, "emptyuser", "right-one", role="member")
    _seed_rag_chunk(
        session, session.scalars(select(User).where(User.username == "emptyuser")).one()
    )
    _override_reranker_high()
    assert login_as(chat_client, "emptyuser", "right-one").status_code == 200

    class _NoopLLM(StubLLMProvider):
        """零产出替身：流"正常结束"但无任何 token（finish=length 无 content 的形态）。"""

        def chat_stream(self, messages, max_tokens):
            return iter(())

    app.state.chat_deps.llm = _NoopLLM()
    resp = post_chat(chat_client, QUESTION)

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert [name for name, _ in events] == ["citations", "error"]  # 无 token 事件
    assert events[-1][1] == {
        "code": 5001,
        "message": "生成服务暂时不可用，请重试",
        "retryable": True,
    }
    conversation = _conversation_row(session)
    (_, row) = _messages(session, conversation.id)
    assert (row.content, row.is_active) == ("", False)  # 空串未落库，中断标记


# ---------- ④b prepare 阶段 embed 故障：SSE 200 拒答话术收口（retrieval-policy §3） ----------


def test_embed_upstream_failure_returns_sse_refuse_with_degraded(chat_client, session) -> None:
    """embed 故障（捕获层=prepare_turn，spec §11 裁决 6）：不再 500——SSE 正常四事件
    拒答话术收口，done.degraded=true；占位行正常 finalize（answer_type='refuse'、
    degraded=true 读回，非中断标记）。"""
    make_user(session, "embedfall", "right-one", role="member")
    assert login_as(chat_client, "embedfall", "right-one").status_code == 200
    app.state.chat_deps.embedder = StubEmbedder(fail_times=1)

    resp = post_chat(chat_client, QUESTION)  # 新会话无历史：改写短路，embed 首调即抛

    assert resp.status_code == 200  # 上游故障经 SSE 表达而非 500（工单 01 验收线）
    events = _parse_sse(resp.text)
    assert [name for name, _ in events] == ["citations", "token", "done"]
    (_, assistant_row) = _messages(session, _conversation_row(session).id)
    assert events[0] == ("citations", {"messageId": assistant_row.id, "items": []})
    assert events[1] == ("token", {"messageId": assistant_row.id, "content": REFUSAL_TEXT})
    assert events[2][1]["refused"] is True
    assert events[2][1]["degraded"] is True  # 降级拒答：done 布尔透出
    assert (assistant_row.content, assistant_row.answer_type) == (REFUSAL_TEXT, "refuse")
    assert (assistant_row.degraded, assistant_row.is_active) == (True, True)  # 正常收尾非中断
    assert assistant_row.cache_hit is False


def test_rerank_upstream_failure_falls_back_to_vector_order_sse(chat_client, session) -> None:
    """rerank 故障（熔断装饰器包裹的 Stub 抛错，spec §3）：SSE 正常 rag 回答而非
    500/中断——引用降为召回余弦序、done.degraded=true、messages.degraded=true 落库；
    熔断器记录 1 次失败（未达阈值仍 closed）。"""
    user = make_user(session, "rankfall", "right-one", role="member")
    chunk = _seed_rag_chunk(session, user)
    breaker = CircuitBreaker(FakeClock())
    set_rerank_breaker(breaker)
    app.state.chat_deps.reranker = CircuitBreakerReranker(StubReranker(fail_times=1), breaker)
    assert login_as(chat_client, "rankfall", "right-one").status_code == 200

    resp = post_chat(chat_client, CHUNK_TEXT)  # 同文提问：召回余弦≈1.0，降级阈值（0.63）之上放行

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert [name for name, _ in events] == ["citations", "token", "token", "token", "done"]
    citation = events[0][1]["items"][0]
    assert citation["refId"] == chunk.id  # 向量序兜底：同文块召回余弦≈1.0 照常命中
    assert citation["score"] == pytest.approx(1.0, abs=1e-6)  # 分数=召回余弦（非 rerank 分）
    assert events[-1][1]["degraded"] is True
    assert events[-1][1]["refused"] is False
    (_, assistant_row) = _messages(session, _conversation_row(session).id)
    assert (assistant_row.answer_type, assistant_row.degraded) == ("rag", True)  # 读回断言
    assert assistant_row.content == "".join(DEFAULT_CHAT_CHUNKS)  # 生成链路不受检索降级影响
    assert breaker.consecutive_failures == 1
    assert breaker.state == "closed"  # 单次失败未达 3 次阈值

    listed = chat_client.get(f"/api/conversations/{assistant_row.conversation_id}/messages")
    assert listed.status_code == 200
    assistant_item = next(i for i in listed.json()["data"]["items"] if i["role"] == "assistant")
    assert assistant_item["degraded"] is True  # GET messages 回传（cache_hit 先例，§2 全链契约）


# ---------- ⑤ 问题长度：2001 字 422/1003 字段级；全空白 422 ----------


def test_question_length_and_blank_rejected_field_level(chat_client, session) -> None:
    """2001 字与全空白：均 422/1003 且 data 指名 question 字段；未落任何会话/消息。"""
    make_user(session, "limituser", "right-one", role="member")
    assert login_as(chat_client, "limituser", "right-one").status_code == 200

    too_long = post_chat(chat_client, "字" * 2001)
    blank = post_chat(chat_client, "   \n\t ")

    for resp in (too_long, blank):
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == ERR_PARAM_INVALID
        assert {entry["field"] for entry in body["data"]} == {"question"}
    assert session.scalars(select(Conversation)).all() == []
    assert session.scalars(select(Message)).all() == []


# ---------- ⑥ 每日限额：limit=2 覆盖 → 第 3 次 429/3002（未开流普通 JSON） ----------


def test_daily_limit_third_request_rejected_429(chat_client, session) -> None:
    """daily_question_limit=2（Settings 覆盖）：前两次成功各自计数，第 3 次 429/3002。"""
    make_user(session, "quotauer", "right-one", role="member")
    assert login_as(chat_client, "quotauer", "right-one").status_code == 200
    app.state.chat_deps.settings = app.state.chat_deps.settings.model_copy(
        update={"daily_question_limit": 2}
    )

    assert post_chat(chat_client, QUESTION).status_code == 200
    assert post_chat(chat_client, "第二个不同的问题").status_code == 200
    third = post_chat(chat_client, "第三个问题")

    assert third.status_code == 429
    body = third.json()
    assert body["code"] == ERR_DAILY_LIMITED
    assert body["message"] == "今日提问次数已达上限，请明天再试"
    assert "event-stream" not in third.headers.get("content-type", "")  # 未开流：普通 JSON


# ---------- ⑦ 他人会话：404/3003（IDOR，不区分不存在与无权） ----------


def test_other_users_conversation_rejected_404(chat_client, session) -> None:
    """用户 B 携带用户 A 的 conversationId 提问：404/3003，A 的会话零写入。"""
    make_user(session, "owneruser", "right-one", role="member")
    make_user(session, "otheruser", "right-two", role="member")
    assert login_as(chat_client, "owneruser", "right-one").status_code == 200
    assert post_chat(chat_client, QUESTION).status_code == 200
    conversation = _conversation_row(session)
    assert login_as(chat_client, "otheruser", "right-two").status_code == 200  # 切换登录态

    resp = post_chat(chat_client, "我也想问", conversation_id=conversation.id)

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == ERR_NOT_FOUND
    assert body["message"] == "会话不存在或无权访问"
    assert len(_messages(session, conversation.id)) == 2  # 越权请求未写入任何消息


# ---------- ⑧ 精确缓存：二次同题 done.cacheHit=true 且生成零新增（CHAT-05） ----------


def test_second_identical_question_hits_cache_without_generation(chat_client, session) -> None:
    """同用户同问题两问（各自新会话，无历史不触发改写）：第二问命中缓存，llm 零新增
    调用，done.cacheHit=true、tokens 双 0，落库 cache_hit=true 且答案/引用与首问一致。"""
    user = make_user(session, "cacheuser", "right-one", role="member")
    _seed_rag_chunk(session, user)
    _override_reranker_high()
    assert login_as(chat_client, "cacheuser", "right-one").status_code == 200

    first = post_chat(chat_client, QUESTION)
    second = post_chat(chat_client, QUESTION)

    assert first.status_code == 200 and second.status_code == 200
    events_first = _parse_sse(first.text)
    events_second = _parse_sse(second.text)
    assert [name for name, _ in events_second] == ["citations", "token", "done"]
    assert app.state.chat_deps.llm.stream_calls == 1  # 仅首问生成一次（CHAT-05 生成零新增）
    done_first, done_second = events_first[-1][1], events_second[-1][1]
    assert done_first["cacheHit"] is False
    assert done_second["cacheHit"] is True
    assert done_second["degraded"] is False  # 缓存命中轮恒非降级（降级计划不写缓存）
    assert (
        done_second["refused"],
        done_second["promptTokens"],
        done_second["completionTokens"],
    ) == (
        False,
        0,
        0,
    )
    assert events_second[0][1]["items"] == events_first[0][1]["items"]  # 引用同源缓存条目
    rows = session.scalars(
        select(Message).where(Message.role == "assistant").order_by(Message.id)
    ).all()
    first_row, second_row = rows[0], rows[1]
    assert done_second["messageId"] == second_row.id != first_row.id
    assert second_row.cache_hit is True
    assert second_row.answer_type == "rag"  # 取缓存条目内原 answer_type，非 'cache'
    assert (second_row.content, second_row.citations) == (first_row.content, first_row.citations)
    assert (second_row.prompt_tokens, second_row.completion_tokens) == (0, 0)


# ---------- ⑨ 多轮改写（Task 9 补测义务）：检索命中基于改写后问题，history 只取 6 条 ----------

QUESTIONS = ["公司年假有多少天", "那病假呢", "补贴怎么算", "调休怎么申请", "年假可以折现吗"]
REWRITTEN = "改写后的问题"  # 替身对每次改写调用的固定产出


class _RecordingRewriteLLM(StubLLMProvider):
    """记录每次 chat_stream 收到的末条 user 消息：改写与生成共用本入口，按内容区分
    （改写 prompt 含 [当前问题] 段，生成 prompt 含 <data> 段）。"""

    def __init__(self) -> None:
        super().__init__(chunks=(REWRITTEN,))
        self.prompts: list[str] = []

    def chat_stream(self, messages, max_tokens):
        self.prompts.append(messages[-1]["content"])
        return super().chat_stream(messages, max_tokens)


def _seed_qa_pair(session: Session, user: User) -> QaPair:
    """问答对种子：embedding=embed(改写文)——只有经多轮改写的问题才可能命中（sim≈1），
    任何原始追问与它近乎正交（sim≈0 << 0.85 阈值），命中即证明检索键是改写后问题。"""
    qa = QaPair(
        dept_id=user.dept_id,
        question=REWRITTEN,
        answer="病假按工龄三到十天",
        embedding=StubEmbedder().embed([REWRITTEN])[0],
        created_by=user.id,
    )
    session.add(qa)
    session.flush()
    return qa


def _dialogue_lines(rewrite_prompt: str) -> list[str]:
    """取改写 prompt 的 [对话] 段行（[对话] 与 [当前问题] 两标记之间，逐条历史消息）。"""
    lines = rewrite_prompt.split("\n")
    return lines[lines.index("[对话]") + 1 : lines.index("[当前问题]")]


def test_multi_turn_rewrite_hits_qa_on_rewritten_question_and_history_capped(
    chat_client, session
) -> None:
    """同会话五连问：第 1 轮无历史不改写走文档检索；第 2 轮追问经改写命中 QA 对
    （refType=qa、sim≈1，原始追问绝不命中——证检索键是改写后问题）；第 3 轮起命中
    第 2 轮写入的精确缓存（键=改写后问题，CHAT-05/§17.5），citations 同源且
    cacheHit=true；第 5 轮改写 prompt 的 [对话] 段恰 6 条（最近 3 轮），第 1 轮已被
    挤出；五轮各自完整事件序 citations→token→done、每轮一对消息完整落库。"""
    user = make_user(session, "multiturn", "right-one", role="member")
    _seed_rag_chunk(session, user)
    qa = _seed_qa_pair(session, user)
    _override_reranker_high()
    recording = _RecordingRewriteLLM()
    app.state.chat_deps.llm = recording  # 定制 deps：改写恒产出 REWRITTEN（W1 app.state 模式）
    assert login_as(chat_client, "multiturn", "right-one").status_code == 200

    responses = [post_chat(chat_client, QUESTIONS[0])]
    responses += [
        post_chat(chat_client, question, conversation_id=_conversation_row(session).id)
        for question in QUESTIONS[1:]
    ]

    assert all(resp.status_code == 200 for resp in responses)
    events = [_parse_sse(resp.text) for resp in responses]
    assert all([name for name, _ in chunk] == ["citations", "token", "done"] for chunk in events)
    assert events[0][0][1]["items"][0]["refType"] == "doc"  # 第 1 轮原始问题走文档检索
    qa_citation = events[1][0][1]["items"][0]
    assert (qa_citation["refType"], qa_citation["refId"]) == ("qa", qa.id)
    assert qa_citation["score"] == pytest.approx(1.0)  # 与种子 QA 向量完全同向
    assert events[1][1][1]["content"] == REWRITTEN
    assert all(chunk[2][1]["cacheHit"] is True for chunk in events[2:])  # 第 3 轮起精确缓存
    assert all(chunk[0][1]["items"] == [qa_citation] for chunk in events[2:])
    # qa 与 cache 轮 degraded 恒 false 且字段不缺席（retrieval-policy §5）
    assert all(chunk[2][1]["degraded"] is False for chunk in events)
    # llm 调用恰 6 次：4 次改写（第 1 轮无历史短路；缓存命中轮也先改写）+ 2 次生成
    assert len(recording.prompts) == 6
    rewrites = [prompt for prompt in recording.prompts if "[当前问题]" in prompt]
    assert len(rewrites) == 4
    assert _dialogue_lines(rewrites[0]) == ["用户：公司年假有多少天", f"助手：{REWRITTEN}"]
    assert _dialogue_lines(rewrites[-1]) == [
        "用户：那病假呢",
        f"助手：{REWRITTEN}",
        "用户：补贴怎么算",
        f"助手：{REWRITTEN}",
        "用户：调休怎么申请",
        f"助手：{REWRITTEN}",
    ]  # history 只取最近 3 轮（6 条）
    assert "公司年假有多少天" not in rewrites[-1]  # 第 1 轮已被挤出改写上下文
    rows = _messages(session, _conversation_row(session).id)
    assert [row.role for row in rows] == ["user", "assistant"] * 5  # 五轮各一对完整落库
    assert [row.content for row in rows[::2]] == QUESTIONS


# ---------- ⑩ 文档操作缓存失效（W3 Task 1）：retry/confirm 成功响应前整清精确缓存 ----------


def _seed_pending_confirm_doc(session: Session, user: User) -> Document:
    """直插待确认文档行（敏感命中形态：ready + is_active=False）。confirm 仅翻
    is_active 不触文件，无需真实落盘文件；content_hash 全局唯一，独立取值防撞。"""
    return _seed_document_row(
        session,
        user,
        title="客服_敏感件_20260901",
        content_hash=hashlib.sha256(b"pending-confirm-doc").hexdigest(),
        status="ready",
        is_active=False,
    )


def _seed_failed_doc(session: Session, user: User) -> Document:
    """直插失败文档行（status=failed 可重试）。file_path 指向不存在文件：test_mode
    重跑编排解析必然再失败（process_document 异常兜底落 failed），端点仍 200。"""
    return _seed_document_row(
        session,
        user,
        title="客服_失败件_20260901",
        content_hash=hashlib.sha256(b"failed-doc-for-retry").hexdigest(),
        status="failed",
        is_active=False,
    )


def _seed_document_row(
    session: Session, user: User, *, title: str, content_hash: str, status: str, is_active: bool
) -> Document:
    """非检索用文档行种子（无 Chunk 不影响召回）：仅承载 retry/confirm 的状态机形态。"""
    doc = Document(
        title=title,
        dept_id=user.dept_id,
        original_filename=f"{title}.docx",
        file_path=f"seed/{title}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=content_hash,
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status=status,
        is_active=is_active,
        uploaded_by=user.id,
    )
    session.add(doc)
    session.flush()
    return doc


def test_confirm_clears_exact_cache_and_next_question_regenerates(chat_client, session) -> None:
    """预热精确缓存（同题二问命中，stream_calls=1）→ admin confirm 激活待确认文档 →
    同题三问不再命中（stream_calls 恢复增长为 2、done.cacheHit=false）：文档状态变化
    改变可检索事实，TTL 不可依赖（W2 移交②）。"""
    user = make_user(session, "cacheconfirm", "right-one", role="member")
    make_user(session, "cacheadmin1", "right-two", role="admin")
    _seed_rag_chunk(session, user)
    pending = _seed_pending_confirm_doc(session, user)
    _override_reranker_high()
    assert login_as(chat_client, "cacheconfirm", "right-one").status_code == 200
    assert post_chat(chat_client, QUESTION).status_code == 200
    assert post_chat(chat_client, QUESTION).status_code == 200
    assert app.state.chat_deps.llm.stream_calls == 1  # 预热：第二问命中缓存

    assert login_as(chat_client, "cacheadmin1", "right-two").status_code == 200  # 切 admin
    resp = chat_client.post(f"/api/documents/{pending.id}/confirm")

    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": pending.id, "isActive": True}
    assert login_as(chat_client, "cacheconfirm", "right-one").status_code == 200  # 切回提问者
    third = post_chat(chat_client, QUESTION)

    assert third.status_code == 200
    assert _parse_sse(third.text)[-1][1]["cacheHit"] is False
    assert app.state.chat_deps.llm.stream_calls == 2  # 缓存已被整清：第三问重新生成


def test_retry_clears_exact_cache_and_next_question_regenerates(chat_client, session) -> None:
    """预热缓存 → admin retry 重跑失败文档（test_mode 同步编排，文件缺失再失败仍
    200）→ 同题三问不再命中：失效在成功响应前生效，与编排最终状态无关。"""
    user = make_user(session, "cacheretry", "right-one", role="member")
    make_user(session, "cacheadmin2", "right-two", role="admin")
    _seed_rag_chunk(session, user)
    failed = _seed_failed_doc(session, user)
    _override_reranker_high()
    assert login_as(chat_client, "cacheretry", "right-one").status_code == 200
    assert post_chat(chat_client, QUESTION).status_code == 200
    assert post_chat(chat_client, QUESTION).status_code == 200
    assert app.state.chat_deps.llm.stream_calls == 1

    assert login_as(chat_client, "cacheadmin2", "right-two").status_code == 200
    resp = chat_client.post(f"/api/documents/{failed.id}/retry")

    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "failed"  # 编排确实重跑（文件缺失仍失败）
    assert login_as(chat_client, "cacheretry", "right-one").status_code == 200  # 切回提问者
    third = post_chat(chat_client, QUESTION)

    assert third.status_code == 200
    assert _parse_sse(third.text)[-1][1]["cacheHit"] is False
    assert app.state.chat_deps.llm.stream_calls == 2  # 缓存已被整清：第三问重新生成


# ---------------------------------------------------------------------------
# 客户端断开收尾（2026-10-31 评审 P1-5，工单 07——修复前两用例均红）
# 先例：tests/api/test_agent_runs.py::test_generator_close_sweeps_failed_no_ghost
# （gen.close() = Starlette 断连向生成器注入 GeneratorExit 的等价形态）
# ---------------------------------------------------------------------------


def _placeholder_pair(session: Session, user: User) -> Message:
    """请求阶段占位行直插（同 app/api/chat.py _persist_placeholder 事务①形态）。"""
    conv = Conversation(user_id=user.id, dept_id=user.dept_id, title="断开收尾测试")
    session.add(conv)
    session.flush()
    placeholder = Message(
        conversation_id=conv.id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="assistant",
        content="",
        is_active=True,
    )
    session.add(placeholder)
    session.flush()
    return placeholder


def _rag_plan() -> TurnPlan:
    """最小 rag 计划：citations 空表 + Stub 产段流。"""
    return TurnPlan(
        kind="rag",
        citations=[],
        rewritten_question=QUESTION,
        llm_messages=[{"role": "user", "content": QUESTION}],
        cache_entry=None,
        cache_key=None,
    )


def test_客户端断开时占位行被置inactive不留幽灵(chat_client, session) -> None:  # type: ignore[no-untyped-def]
    """客户端断连（生成器被 close）：GeneratorExit 必须触发 interrupt_turn 收尾，
    占位行置 is_active=false——修复前 GeneratorExit 是 BaseException 绕过两个 except
    分支，占位行 is_active=true 空正文永久残留（幽灵行污染多轮改写与概览分母）。"""
    from app.services.chat_stream import event_stream

    user = make_user(session, "cutchat", "right-one", "member")
    placeholder = _placeholder_pair(session, user)

    gen = event_stream(app.state.chat_deps, _rag_plan(), placeholder.id, 0.0)
    assert next(gen)[0] == "citations"
    second = next(gen)  # 推进到 token（挂起点进入 _stream_generate 内部）
    assert second[0] == "token"
    gen.close()  # 模拟 Starlette 断连：GeneratorExit 在挂起的 yield token 点注入

    session.expire_all()
    row = session.scalar(select(Message).where(Message.id == placeholder.id))
    assert row is not None
    assert row.is_active is False  # 修复前 True（幽灵）
    assert row.content == ""  # 无正文写入（中断收尾不落半截内容）


def test_正常完成后close不误伤已落库回答(chat_client, session) -> None:  # type: ignore[no-untyped-def]
    """done 事件已产出（finalize 已提交）后生成器被 close：不能把已完成的回答误置
    inactive——settled 标志必须在 yield done 之前置位，防 finally 兜底误伤。"""
    from app.services.chat_stream import event_stream

    user = make_user(session, "cutchat2", "right-one", "member")
    placeholder = _placeholder_pair(session, user)

    gen = event_stream(app.state.chat_deps, _rag_plan(), placeholder.id, 0.0)
    assert next(gen)[0] == "citations"
    events: list[str] = []
    for name, _payload in gen:
        events.append(name)
        if name == "done":
            break
    assert "done" in events
    gen.close()  # 挂在 yield done 点被关闭

    session.expire_all()
    row = session.scalar(select(Message).where(Message.id == placeholder.id))
    assert row is not None
    assert row.is_active is True
    assert row.content != ""  # finalize 正文完好
