r"""令牌桶分级限流 API 测试（工单 21 设计 §5.2 + 评审修订 R7 agent 对称断言）。

覆盖：chat 桶拒绝 429/2005 + data.retryAfterSec（未开流普通 JSON）；桶拒绝不烧日配额
（memory 实例 _usage 断言，memory 专属）；agent 入口对称断言（429 + retryAfterSec、
不落占位行、不烧配额、次序钉死：并发闸→桶→日配额）；总闸关闭行为不变（缺省关同负载
不 429、日配额仍生效）。桶实例随 TestClient lifespan 每用例重建（_build_state 每次新造
TokenBucketLimiter），无跨用例污染；生产桶走 SystemClock 直连（不随 fake_clock 拨动），
分档压到 burst=1、rate=1/min 使测试窗口内重补量 <1 令牌，断言确定。
"""

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import ERR_DAILY_LIMITED, ERR_RATE_LIMITED
from app.db.models import AgentRun, Department, User
from app.main import app
from tests.conftest import login_as

QUESTION = "公司年假有多少天"
MSG_RATE_LIMITED = "操作过于频繁，请稍后重试"
MSG_CONCURRENT_LIMITED = "已有生成任务进行中，请等待完成或停止后再试"


def make_user(session: Session, username: str, password: str) -> User:
    """直插测试用户（口径同 test_chat_stream/test_agent_runs），挂全员部门。"""
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


def post_chat(client: TestClient, question: str) -> Response:
    return client.post("/api/chat/stream", json={"question": question})


def post_run(client: TestClient) -> Response:
    return client.post("/api/agent/runs", json={"taskType": "kb_qa", "input": "公司年假有多少天"})


def _enable_limiter(**extra: object) -> None:
    """开总闸并把分档压到最小（burst=1、rate=1/min：测试窗口内重补 <1 令牌，确定性）；
    两链路 Settings 同步覆盖（同 test_agent_runs 每日限额先例）。extra 透传补充键。"""
    limited = app.state.chat_deps.settings.model_copy(
        update={
            "rate_limiter_enabled": True,
            "rate_chat_per_min": 1,
            "rate_chat_burst": 1,
            "rate_agent_per_min": 1,
            "rate_agent_burst": 1,
            **extra,
        }
    )
    app.state.chat_deps.settings = limited
    app.state.agent_deps.settings = limited


@pytest.fixture()
def agent_client(session, fake_clock, test_mode, monkeypatch):
    """AG-UI agent 测试客户端（本地镜像 test_agent_runs.agent_client，不触碰并行在途
    文件）：services.agent_runs / agent_bridge / agent_steps 三处 SessionLocal 指向测试
    连接——短会话与用例读回同处测试事务。"""
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


# ---------- ① chat 桶拒绝：429/2005 + retryAfterSec（未开流普通 JSON） ----------


def test_chat_桶拒绝返回未开流429带retryAfterSec(chat_client, session) -> None:
    """burst=1 首发放行、第 2 次 429/2005：未开流普通 JSON、桶文案、data.retryAfterSec
    =ceil(60/rate)≥1（配置推导的保守提示，design/21 §4.2/§7.3）。"""
    make_user(session, "bucketuser", "right-one")
    assert login_as(chat_client, "bucketuser", "right-one").status_code == 200
    _enable_limiter()
    assert app.state.chat_deps.bucket is not None  # memory 分支已装配桶
    assert app.state.agent_deps.bucket is app.state.chat_deps.bucket  # 共享同一实例

    assert post_chat(chat_client, QUESTION).status_code == 200  # burst=1 首发放行
    resp = post_chat(chat_client, QUESTION)

    assert resp.status_code == 429
    assert "event-stream" not in resp.headers.get("content-type", "")
    body = resp.json()
    assert body["code"] == ERR_RATE_LIMITED
    assert body["message"] == MSG_RATE_LIMITED
    assert body["data"]["retryAfterSec"] >= 1
    assert body["data"]["retryAfterSec"] == 60  # ceil(60/1)：rate=1/min 的最小等待


# ---------- ② chat 桶拒绝不烧日配额（memory 专属 _usage 断言） ----------


def test_chat_桶拒绝不烧日配额(chat_client, session) -> None:
    """三层独立判定（design/21 §6）：桶拒绝路径不触达 DailyCounter——成功轮计 1 次，
    429 轮后 _usage 原值不变。"""
    user = make_user(session, "noburnuser", "right-one")
    assert login_as(chat_client, "noburnuser", "right-one").status_code == 200
    _enable_limiter()

    assert post_chat(chat_client, QUESTION).status_code == 200
    after_first = app.state.chat_deps.counter._usage[user.id]
    assert after_first[1] == 1  # 成功轮恰计 1 次

    assert post_chat(chat_client, QUESTION).status_code == 429  # 桶拒绝

    assert app.state.chat_deps.counter._usage[user.id] == after_first  # 拒绝不计数


# ---------- ③ agent 入口对称断言（R7）：429 + retryAfterSec、不落占位行、不烧配额 ----------


def test_agent_桶拒绝429带retryAfterSec且不落占位行不烧配额(agent_client, session) -> None:
    """agent 桶拒绝与 chat 侧对称：429/2005 未开流普通 JSON + retryAfterSec；agent_runs
    表无新行（拒绝发生在占位落库前）、DailyCounter 未计数。"""
    user = make_user(session, "agentbucket", "right-one")
    assert login_as(agent_client, "agentbucket", "right-one").status_code == 200
    _enable_limiter()

    assert post_run(agent_client).status_code == 200  # burst=1 首发放行
    after_first = app.state.chat_deps.counter._usage[user.id]
    assert after_first[1] == 1

    resp = post_run(agent_client)

    assert resp.status_code == 429
    assert "event-stream" not in resp.headers.get("content-type", "")
    body = resp.json()
    assert body["code"] == ERR_RATE_LIMITED
    assert body["message"] == MSG_RATE_LIMITED
    assert body["data"]["retryAfterSec"] >= 1
    assert len(session.scalars(select(AgentRun)).all()) == 1  # 首跑在册、拒绝轮未落占位行
    assert app.state.chat_deps.counter._usage[user.id] == after_first  # 拒绝不烧配额


# ---------- ④ 次序钉死：并发闸→桶→日配额（design/21 §6 + R7） ----------


def test_agent_次序钉死_并发闸先于桶_桶先于日配额(agent_client, session) -> None:
    """两道次序各用一用户钉死：A（并发闸先于桶）桶已耗尽且有 2 个 running 占位行 →
    429 文案为并发文案（若桶在前会误报桶文案）；B（桶先于日配额）桶与配额同时耗尽 →
    429 文案为桶文案（若配额在前会误报日配额文案）。"""
    user_a = make_user(session, "ordercap", "right-one")
    assert login_as(agent_client, "ordercap", "right-one").status_code == 200
    _enable_limiter(daily_question_limit=1)
    assert post_run(agent_client).status_code == 200  # 消耗 agent 桶 burst=1
    for _ in range(2):
        session.add(
            AgentRun(
                user_id=user_a.id,
                dept_id=user_a.dept_id,
                run_type="kb_qa",
                input={"taskType": "kb_qa", "input": "占位"},
                status="running",
            )
        )
    session.flush()

    concurrent = post_run(agent_client)

    assert concurrent.status_code == 429
    assert concurrent.json()["message"] == MSG_CONCURRENT_LIMITED  # 并发闸先于桶

    user_b = make_user(session, "orderquota", "right-one")
    assert login_as(agent_client, "orderquota", "right-one").status_code == 200
    assert post_run(agent_client).status_code == 200  # 桶 1→0、配额 1/1 同轮耗尽

    both_exhausted = post_run(agent_client)

    assert both_exhausted.status_code == 429
    assert both_exhausted.json()["message"] == MSG_RATE_LIMITED  # 桶先于日配额
    runs_b = [r for r in session.scalars(select(AgentRun)).all() if r.user_id == user_b.id]
    assert len(runs_b) == 1  # 拒绝轮未落占位行


# ---------- ⑤ 总闸关闭行为不变（缺省 False 回归） ----------


def test_总闸关闭行为不变(chat_client, session) -> None:
    """rate_limiter_enabled 缺省 False：分档压到 burst=1 放大信号——若接线误启用则第 2
    次必 429/2005；实际第 2 次 429/3002 来自日配额（limit=1），桶零介入、配额仍生效。"""
    make_user(session, "gateoff", "right-one")
    assert login_as(chat_client, "gateoff", "right-one").status_code == 200
    limited = app.state.chat_deps.settings.model_copy(
        update={"rate_chat_per_min": 1, "rate_chat_burst": 1, "daily_question_limit": 1}
    )
    app.state.chat_deps.settings = limited
    app.state.agent_deps.settings = limited

    assert post_chat(chat_client, QUESTION).status_code == 200
    second = post_chat(chat_client, QUESTION)

    assert second.status_code == 429
    assert "event-stream" not in second.headers.get("content-type", "")
    body = second.json()
    assert body["code"] == ERR_DAILY_LIMITED  # 429 归因日配额而非桶
    assert body["message"] == "今日提问次数已达上限，请明天再试"
