r"""AG-UI agent 流端点测试（工单 #07）：事件序 / 关键载荷 / 限额 / 失败终态无幽灵。

消费方式与 test_chat_stream 同款：TestClient 整体缓冲响应体按 `\n\n` 切事件解析，
断言事件名顺序精确、AG-UI 关键载荷（thread_id/run_id/tool_call_name/delta/result）
与 agent_runs 读回逐项相等（§15.4 写库必读回）。agent_client 本地构造（不并入共享
conftest，避免碰并行在途文件）：在 chat_client 三件套之上另把 services.agent_runs
的 SessionLocal（生成器短会话的会话工厂）指向测试连接——短会话与用例读回同处测试
事务（commit 只释放 savepoint），测试结束整体回滚，不触真库。事件序以装配层
_TEST_AGENT_TURNS 剧本为准（工具调用一轮 + 最终正文，零网络确定性）。
"""

import hashlib
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ERR_DAILY_LIMITED, ERR_INTERNAL, ERR_LLM_UNAVAILABLE, ERR_PARAM_INVALID
from app.core.llm import StubEmbedder, UpstreamError
from app.core.tools import RunContext
from app.db.models import AgentRun, AgentStep, Chunk, Department, Document, User
from app.main import app
from app.services.agent_runs import event_stream
from tests.conftest import login_as

INPUT_TEXT = "公司年假有多少天"
FINAL_TEXT = "根据《考勤制度》，年假按工龄五到十天。"
CHUNK_TEXT = "年假按工龄五到十天"
DOC_TITLE = "全员_考勤制度"

# 装配层剧本决定的全序：RUN → 一步工具调用 → 最终文本 → RUN 收尾
EXPECTED_ORDER = [
    "RUN_STARTED",
    "STEP_STARTED",
    "TOOL_CALL_START",
    "TOOL_CALL_ARGS",
    "TOOL_CALL_END",
    "STEP_FINISHED",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "RUN_FINISHED",
]


# ---------- 构造与消费辅助（口径同 tests/api/test_chat_stream.py） ----------


def make_user(session: Session, username: str, password: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内；部门取全员（检索范围含自身部门）。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role="member",
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _seed_chunk(session: Session, user: User) -> Chunk:
    """直插 ready 文档 + 单 chunk（StubEmbedder 确定性向量与 deps 内 Stub 同算法同向量）：
    证明工具调用真的穿过 VectorStore SQL（检索管道坏掉会以 RUN_ERROR 暴露）。"""
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


def post_run(client: TestClient, task_type: str = "kb_qa", task_input: str = INPUT_TEXT):
    return client.post("/api/agent/runs", json={"taskType": task_type, "input": task_input})


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


def _run_row(session: Session) -> AgentRun:
    session.expire_all()  # 生成器短会话的收尾 UPDATE 不经过本 session 身份映射，强制重载
    return session.scalars(select(AgentRun)).one()


@pytest.fixture()
def agent_client(session, fake_clock, test_mode, monkeypatch):
    """AG-UI agent 测试客户端（chat_client 三件套 + 短会话工厂指向测试连接）：
    lifespan 以 Stub/剧本装配 agent_deps；services.agent_runs（生成器短会话/收尾落库）、
    services.agent_bridge（工具调用短会话，工单 #06 拆出）与 services.agent_steps（逐步
    轨迹落库）三处 SessionLocal 同指测试连接——短会话与用例读回同处测试事务。"""
    from app.api.deps import get_db
    from app.services import agent_bridge, agent_runs, agent_steps
    from app.services.auth import default_limiter

    factory = sessionmaker(bind=session.connection(), expire_on_commit=False)
    monkeypatch.setattr(agent_runs, "SessionLocal", factory)
    monkeypatch.setattr(agent_bridge, "SessionLocal", factory)
    monkeypatch.setattr(agent_steps, "SessionLocal", factory)
    default_limiter.reset()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http
    app.dependency_overrides.pop(get_db, None)


# ---------- ① 正常全序：AG-UI 事件序与关键载荷，run 读回 succeeded ----------


def test_agent_run_full_event_order_and_run_persisted(agent_client, session) -> None:
    """kb_qa run：RUN_STARTED→(STEP→TOOL 三连→STEP 收)→TEXT 三连→RUN_FINISHED 全序精确；
    载荷关键键逐项断言；agent_runs 读回 succeeded + 产物/归属/耗时齐全。"""
    user = make_user(session, "agentuser", "right-one")
    _seed_chunk(session, user)
    assert login_as(agent_client, "agentuser", "right-one").status_code == 200

    resp = post_run(agent_client)

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert [name for name, _ in events] == EXPECTED_ORDER
    run = _run_row(session)
    run_id = str(run.id)
    # RUN_STARTED：thread/run 编址（thread 每 run 独立，MVP 无会话实体）
    assert events[0][1] == {
        "type": "RUN_STARTED",
        "thread_id": f"agent-run-{run.id}",
        "run_id": run_id,
    }
    # 步骤与工具组：步名 step-1，工具名/参数 delta 与剧本一致
    assert events[1][1] == {"type": "STEP_STARTED", "step_name": "step-1"}
    assert events[2][1]["type"] == "TOOL_CALL_START"
    assert events[2][1]["tool_call_name"] == "knowledge_base_search"
    assert events[3][1]["tool_call_id"] == events[2][1]["tool_call_id"] == "call_1"
    assert json.loads(events[3][1]["delta"]) == {"query": "年假"}
    assert events[4][1] == {"type": "TOOL_CALL_END", "tool_call_id": "call_1"}
    assert events[5][1] == {"type": "STEP_FINISHED", "step_name": "step-1"}
    # 文本组：同一 message_id，delta 即最终正文；RUN_FINISHED.result 携带成稿
    message_id = events[6][1]["message_id"]
    assert events[6][1]["role"] == "assistant"
    assert events[7][1] == {
        "type": "TEXT_MESSAGE_CONTENT",
        "message_id": message_id,
        "delta": FINAL_TEXT,
    }
    assert events[8][1] == {"type": "TEXT_MESSAGE_END", "message_id": message_id}
    assert events[9][1] == {
        "type": "RUN_FINISHED",
        "thread_id": f"agent-run-{run.id}",
        "run_id": run_id,
        "result": {"text": FINAL_TEXT},
    }
    # 读回：终态 succeeded + 产物与归属逐项相等（不留 running 幽灵行）
    assert run.status == "succeeded"
    assert run.output == {"text": FINAL_TEXT}
    assert run.error is None
    assert run.model == "stub"  # settings 模型名为空（test_mode Stub）的落库回退名
    assert (run.user_id, run.dept_id) == (user.id, user.dept_id)  # 归属提问者
    assert run.run_type == "kb_qa"
    assert run.input == {"taskType": "kb_qa", "input": INPUT_TEXT}
    assert isinstance(run.latency_ms, int) and run.latency_ms >= 0
    assert run.finished_at is not None
    assert session.scalars(select(AgentRun).where(AgentRun.status == "running")).all() == []
    # 逐步轨迹（#06 接线后 kb_qa 同样落 agent_steps）：一次工具调用恰一行，与工具事件组一致
    steps = session.scalars(
        select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.seq)
    ).all()
    assert [(s.seq, s.step_type, s.tool_name) for s in steps] == [
        (1, "tool", "knowledge_base_search")
    ]
    assert steps[0].args == {"query": "年假"}
    assert steps[0].error is None
    assert steps[0].observation  # 命中快照非空（标题来自种子语料）
    assert DOC_TITLE in steps[0].observation


# ---------- ② 参数校验：未知 taskType / 空白与超长 input → 422/1003 字段级 ----------


def test_unknown_task_type_rejected_422(agent_client, session) -> None:
    """taskType 不在白名单（kb_qa/template_draft）：422/1003 且字段定位 taskType，
    未落任何 run 行。（template_draft 已随工单 #06 放开，另见 test_agent_template_draft.py）"""
    make_user(session, "typetype", "right-one")
    assert login_as(agent_client, "typetype", "right-one").status_code == 200

    resp = post_run(agent_client, task_type="weekly_report")

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["data"][0]["field"] == "taskType"
    assert session.scalars(select(AgentRun)).all() == []  # 校验拒绝不落行（开流前拒绝）


def test_blank_and_overlong_input_rejected_422(agent_client, session) -> None:
    """input 全空白与超 2000 字：422/1003 且字段定位 input。"""
    make_user(session, "inputuser", "right-one")
    assert login_as(agent_client, "inputuser", "right-one").status_code == 200

    blank = post_run(agent_client, task_input="   ")
    overlong = post_run(agent_client, task_input="字" * 2001)

    for resp in (blank, overlong):
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == ERR_PARAM_INVALID
        assert body["data"][0]["field"] == "input"


# ---------- ③ 鉴权与限额：未登录 401；每日限额复用问答同一计数器（429/3002） ----------


def test_unauthenticated_rejected_401(agent_client) -> None:
    """未登录直接 POST：401/2003（与全站鉴权口径一致），无事件流。"""
    resp = post_run(agent_client)

    assert resp.status_code == 401
    assert resp.json()["code"] == 2003
    assert "event-stream" not in resp.headers.get("content-type", "")


def test_daily_limit_shared_with_chat_rejected_429(agent_client, session) -> None:
    """daily_question_limit=1（两链路 Settings 同步覆盖）：agent run 消费后，同用户第 2 次
    agent 请求与问答端点双双 429——同实例 DailyCounter 换端点不绕限；429/3002 未开流普通
    JSON。counter 先做同一性断言（同实例是"共享额度"的前提）。"""
    make_user(session, "quotauser", "right-one")
    assert login_as(agent_client, "quotauser", "right-one").status_code == 200
    assert app.state.agent_deps.counter is app.state.chat_deps.counter  # 同一实例
    limited = app.state.agent_deps.settings.model_copy(update={"daily_question_limit": 1})
    app.state.agent_deps.settings = limited
    app.state.chat_deps.settings = limited  # 两链路同一限额口径，消费计数落在同一计数器

    assert post_run(agent_client).status_code == 200
    second = post_run(agent_client)
    chat = agent_client.post("/api/chat/stream", json={"question": "换个端点还想问"})

    assert second.status_code == 429
    assert chat.status_code == 429
    for resp in (second, chat):
        body = resp.json()
        assert body["code"] == ERR_DAILY_LIMITED
        assert body["message"] == "今日提问次数已达上限，请明天再试"
        assert "event-stream" not in resp.headers.get("content-type", "")


# ---------- ④ 失败终态：上游故障 / 意外异常 → RUN_ERROR + failed（不留幽灵） ----------


class _FailingChannel:
    """故障通道替身：complete 即抛指定异常，注测 RUN_ERROR 两条路径。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def complete(self, messages, tools, max_tokens):  # AgentModelChannel 协议面
        raise self._exc


def _assert_failed_tail(agent_client, session, code: int, summary: str) -> None:
    resp = post_run(agent_client)
    events = _parse_sse(resp.text)
    assert events[0][0] == "RUN_STARTED"
    assert events[-1] == (
        "RUN_ERROR",
        {"type": "RUN_ERROR", "message": summary, "code": str(code)},
    )
    run = _run_row(session)
    assert run.status == "failed"
    assert run.error == summary  # 对外脱敏话术落库，不含内部细节
    assert run.finished_at is not None
    assert run.output is None
    assert session.scalars(select(AgentRun).where(AgentRun.status == "running")).all() == []


def test_upstream_failure_marks_failed_and_run_error(agent_client, session, monkeypatch) -> None:
    """上游故障（UpstreamError）：流收尾 RUN_ERROR code=5001 + 行置 failed；脱敏话术与
    问答 _MSG_LLM_UNAVAILABLE 同口径，技术细节只进日志。"""
    make_user(session, "failuser", "right-one")
    assert login_as(agent_client, "failuser", "right-one").status_code == 200
    monkeypatch.setattr(
        app.state.agent_deps, "channel", _FailingChannel(UpstreamError("stub channel down"))
    )

    _assert_failed_tail(agent_client, session, ERR_LLM_UNAVAILABLE, "生成服务暂时不可用，请重试")


def test_unexpected_failure_marks_failed_and_run_error(agent_client, session, monkeypatch) -> None:
    """意外异常（非上游）：流收尾 RUN_ERROR code=5000 + 行置 failed（通用脱敏话术）。"""
    make_user(session, "boomuser", "right-one")
    assert login_as(agent_client, "boomuser", "right-one").status_code == 200
    monkeypatch.setattr(app.state.agent_deps, "channel", _FailingChannel(RuntimeError("意外内伤")))

    _assert_failed_tail(agent_client, session, ERR_INTERNAL, "系统繁忙，请稍后重试")


# ---------- ⑤ 客户端断连：生成器 close（GeneratorExit）→ 兜底置 failed 无幽灵 ----------


def test_generator_close_sweeps_failed_no_ghost(agent_client, session) -> None:
    """流中断裂（客户端断连等价：生成器被 close）：不可再发事件，finally 兜底把 run 置
    failed（独立短会话提交）——任何退出路径不留 status=running 幽灵行。"""
    user = make_user(session, "cutuser", "right-one")
    ctx = RunContext(user_id=user.id, username=user.username, dept_ids=(user.dept_id, user.dept_id))
    run = AgentRun(user_id=user.id, dept_id=user.dept_id, run_type="kb_qa", input={"input": "q"})
    session.add(run)
    session.commit()

    gen = event_stream(app.state.agent_deps, run.id, ctx, "kb_qa", "任意任务", start=0.0)
    assert next(gen)[0] == "RUN_STARTED"
    gen.close()  # 模拟 Starlette 断连时向生成器注入 GeneratorExit

    swept = _run_row(session)
    assert swept.id == run.id
    assert swept.status == "failed"
    assert swept.error == "系统繁忙，请稍后重试"
    assert swept.finished_at is not None
