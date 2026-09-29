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

from app.core.errors import ERR_DAILY_LIMITED, ERR_NOT_FOUND, ERR_PARAM_INVALID
from app.core.llm import DEFAULT_CHAT_CHUNKS, StubEmbedder, StubLLMProvider, StubReranker, sigmoid
from app.db.models import Chunk, Conversation, Department, Document, Message, User
from app.main import app
from app.rag.prompt import REFUSAL_TEXT

QUESTION = "公司年假有多少天"
CHUNK_TEXT = "年假按工龄五到十天"
DOC_TITLE = "全员_考勤制度"


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


def login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


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
    assert login(chat_client, "raguser", "right-one").status_code == 200

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
    }


# ---------- ② 首问标题：新会话 title=question[:20] ----------


def test_first_question_sets_conversation_title_truncated(chat_client, session) -> None:
    """conversationId 为空新建会话：title 取首问前 20 字（长问题截断），读回验证。"""
    user = make_user(session, "titleuser", "right-one", role="member")
    assert login(chat_client, "titleuser", "right-one").status_code == 200
    question = "请告诉我公司年假的具体天数规定和申请流程说明"

    resp = post_chat(chat_client, question)

    assert resp.status_code == 200
    assert [name for name, _ in _parse_sse(resp.text)] == ["citations", "token", "done"]
    conversation = _conversation_row(session)
    assert conversation.title == question[:20]
    assert (conversation.user_id, conversation.dept_id) == (user.id, user.dept_id)  # 归属提问者


# ---------- ③ 拒答序：citations(items=[])→token(精确话术)→done(refused=true) ----------


def test_refuse_stream_exact_order_and_refusal_text(chat_client, session) -> None:
    """空知识库拒答：citations 空列表也发；token 逐字 REFUSAL_TEXT；done refused=true。"""
    make_user(session, "refuseuser", "right-one", role="member")
    assert login(chat_client, "refuseuser", "right-one").status_code == 200

    resp = post_chat(chat_client, "完全无关的天书问题")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert [name for name, _ in events] == ["citations", "token", "done"]
    (_, assistant_row) = _messages(session, _conversation_row(session).id)
    assert events[0] == ("citations", {"messageId": assistant_row.id, "items": []})
    assert events[1] == ("token", {"messageId": assistant_row.id, "content": REFUSAL_TEXT})
    assert events[2][1]["refused"] is True and events[2][1]["cacheHit"] is False
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
    assert login(chat_client, "breakuser", "right-one").status_code == 200
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
    assert rows[2].content == "那病假呢"  # 重试产生新 user 消息
    assert rows[3].is_active is True and rows[3].content != ""  # 新 assistant 正常收尾


# ---------- ⑤ 问题长度：2001 字 422/1003 字段级；全空白 422 ----------


def test_question_length_and_blank_rejected_field_level(chat_client, session) -> None:
    """2001 字与全空白：均 422/1003 且 data 指名 question 字段；未落任何会话/消息。"""
    make_user(session, "limituser", "right-one", role="member")
    assert login(chat_client, "limituser", "right-one").status_code == 200

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
    assert login(chat_client, "quotauer", "right-one").status_code == 200
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
    assert login(chat_client, "owneruser", "right-one").status_code == 200
    assert post_chat(chat_client, QUESTION).status_code == 200
    conversation = _conversation_row(session)
    assert login(chat_client, "otheruser", "right-two").status_code == 200  # 切换登录态

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
    assert login(chat_client, "cacheuser", "right-one").status_code == 200

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
