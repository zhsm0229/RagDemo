"""API 测试基建：`client`（httpx ASGITransport 包 app）与 `fake_clock`（全局时钟替换）。

get_db 依赖覆写是关键：app 的鉴权写库动作与用例直插的用户数据同处 conftest 的
`session`（挂在外层连接事务上，service 的 commit 只释放 savepoint，对本 session
可见，测试结束整体回滚）；若不覆写，app 自建 SessionLocal 走池中其他连接，
看不到未提交的测试用户。限流器是进程内状态，每个用例开始前重置。
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.clock import FakeClock, SystemClock, set_clock


@pytest.fixture()
def fake_clock():
    """全局可注入时钟换 FakeClock（token exp/锁定时长均可精确断言），测后还原。"""
    clock = FakeClock("2026-10-01T08:00:00+00:00")
    set_clock(clock)
    yield clock
    set_clock(SystemClock())


@pytest.fixture(autouse=True)
def _llm_endpoint_for_assembly(monkeypatch):
    """W2 起 lifespan 装配 chat_deps：非 test_mode 且 llm_base_url 空 → §6.4 拒启。

    API 测试进程统一给占位端点（OpenAICompat 客户端延迟建连，装配不发起任何网络
    请求，问答用例走 test_mode Stub 不经过它）；环境已设置则不覆盖。LLM_MODEL 一律
    钉空：.env 的真实模型名会漏进 settings，断言 stub 落库名的用例（test_chat_stream
    等）在开发机即红；setenv 后清缓存使 get_settings 重读钉值，monkeypatch 还原。"""
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_MODEL", "")
    get_settings.cache_clear()
    saved = os.environ.get("LLM_BASE_URL")
    if saved is not None:
        yield
        get_settings.cache_clear()
        return
    os.environ["LLM_BASE_URL"] = "http://llm.unavailable.invalid"
    get_settings.cache_clear()
    yield
    os.environ.pop("LLM_BASE_URL", None)
    get_settings.cache_clear()


@pytest.fixture()
def test_mode():
    """开启 settings.test_mode：lifespan 以 Stub 三件套装配 chat_deps（chat_client 用）。"""
    from app.core.config import get_settings

    saved = os.environ.get("TEST_MODE")
    os.environ["TEST_MODE"] = "true"
    get_settings.cache_clear()
    yield
    if saved is None:
        os.environ.pop("TEST_MODE", None)
    else:
        os.environ["TEST_MODE"] = saved
    get_settings.cache_clear()


@pytest.fixture()
def client(session, fake_clock):
    """httpx 同步客户端：依赖覆写 get_db → 测试事务 session，cookie 自动保持。"""
    from app.api.deps import get_db
    from app.core.security import default_blacklist
    from app.main import app
    from app.services.auth import default_limiter

    default_limiter.reset()
    default_blacklist.reset()  # 登出黑名单同为进程内状态，用例间隔离（jti 虽不撞，防积留）
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(
        app
    ) as http:  # 同步客户端：cookie 自动保持（httpx 0.28 的 ASGITransport 仅异步）
        yield http
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def chat_client(session, fake_clock, test_mode, monkeypatch):
    """SSE 问答测试客户端：TEST_MODE 下 TestClient 启动即触发 lifespan 自建 Stub
    chat_deps 挂 app.state；get_db 覆写为测试事务 session，另把 services.chat_stream
    的 SessionLocal（生成器短会话的会话工厂）与 services.memory_extract 的
    SessionLocal（长期记忆抽取后台任务体，F-210 §3.2——TestClient 在响应发完后同步
    跑 BackgroundTasks）指向同一连接——短会话与用例读回同处测试事务（commit 只释放
    savepoint，对本 session 可见），测试结束整体回滚不触真库其他连接；抽取任务体里
    build_llm 走 test_mode Stub（默认产出非 JSON，解析失败静默跳过，不落数据）。"""
    from app.api.deps import get_db
    from app.main import app
    from app.services import chat_stream, memory_extract
    from app.services.auth import default_limiter

    factory = sessionmaker(bind=session.connection(), expire_on_commit=False)
    monkeypatch.setattr(chat_stream, "SessionLocal", factory)
    monkeypatch.setattr(memory_extract, "SessionLocal", factory)
    default_limiter.reset()
    app.dependency_overrides[get_db] = lambda: session
    # raise_server_exceptions=False：prepare 阶段上游故障的 500 兜底响应可被用例断言
    # （ServerErrorMiddleware 发送 500 后仍重抛异常，True 会让异常直接上抛测试进程）
    with TestClient(app, raise_server_exceptions=False) as http:
        yield http
    app.dependency_overrides.pop(get_db, None)
