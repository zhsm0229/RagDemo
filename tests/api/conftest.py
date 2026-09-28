"""API 测试基建：`client`（httpx ASGITransport 包 app）与 `fake_clock`（全局时钟替换）。

get_db 依赖覆写是关键：app 的鉴权写库动作与用例直插的用户数据同处 conftest 的
`session`（挂在外层连接事务上，service 的 commit 只释放 savepoint，对本 session
可见，测试结束整体回滚）；若不覆写，app 自建 SessionLocal 走池中其他连接，
看不到未提交的测试用户。限流器是进程内状态，每个用例开始前重置。
"""

import pytest
from fastapi.testclient import TestClient

from app.core.clock import FakeClock, SystemClock, set_clock


@pytest.fixture()
def fake_clock():
    """全局可注入时钟换 FakeClock（token exp/锁定时长均可精确断言），测后还原。"""
    clock = FakeClock("2026-10-01T08:00:00+00:00")
    set_clock(clock)
    yield clock
    set_clock(SystemClock())


@pytest.fixture()
def client(session, fake_clock):
    """httpx 同步客户端：依赖覆写 get_db → 测试事务 session，cookie 自动保持。"""
    from app.api.deps import get_db
    from app.main import app
    from app.services.auth import default_limiter

    default_limiter.reset()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(
        app
    ) as http:  # 同步客户端：cookie 自动保持（httpx 0.28 的 ASGITransport 仅异步）
        yield http
    app.dependency_overrides.pop(get_db, None)
