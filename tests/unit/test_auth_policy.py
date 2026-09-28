"""鉴权策略单测：锁定判定 evaluate_login、滑动窗口限流器、登录失败路径时序一致性（无 pg）。

evaluate_login 的全值域穷举保证服务层失败路径的处置（提示剩余次数 / 触发持久锁）
可精确推导；限流器用注入 FakeClock 冻结时间验证窗口滑动，不同键互不影响。
未知用户名路径（AUTH-08）用 SQLite 内存库只建 users 表验证 dummy-hash 时序拉平。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.core.errors import ERR_INVALID_CREDENTIALS, ApiError
from app.db.models import User
from app.services import auth
from app.services.auth import (
    _DUMMY_HASH,
    ACTION_ALLOW,
    ACTION_LOCK,
    ACTION_REJECT,
    SlidingWindowLimiter,
    evaluate_login,
    login,
)


@pytest.mark.parametrize(
    "attempts,expected",
    [
        (-1, ACTION_ALLOW),  # 防御负值：视为无失败负担
        (0, ACTION_ALLOW),  # 无失败记录：放行（成功路径计数清零后的恒等语义）
        (1, ACTION_REJECT),  # 首次失败：拒绝并提示剩余次数
        (4, ACTION_REJECT),  # 第 4 次失败：剩余 0，再错即锁定（提示含 "0"）
        (5, ACTION_LOCK),  # 第 5 次失败：触发持久锁（AUTH-03）
        (9, ACTION_LOCK),  # 超阈值（锁定过期后再失败）：维持锁定
    ],
)
def test_evaluate_login_policy_table(attempts: int, expected: str) -> None:
    """锁定判定纯函数：<=0 放行 / 1-4 拒绝并提示剩余 / >=5 触发持久锁。"""
    assert evaluate_login(attempts) == expected


def test_evaluate_login_custom_threshold() -> None:
    """可注入阈值：max_attempts=3 时第 3 次失败即锁定。"""
    assert evaluate_login(2, max_attempts=3) == ACTION_REJECT
    assert evaluate_login(3, max_attempts=3) == ACTION_LOCK


def test_sliding_window_limiter_window_and_keys() -> None:
    """限流器：窗口内第 11 次超限；不同键互不影响；窗口滑出恢复；reset 清空。"""
    clock = FakeClock("2026-10-01T08:00:00+00:00")
    limiter = SlidingWindowLimiter(window_seconds=60, max_events=10, clock=clock)
    assert all(limiter.hit("ip|alice") for _ in range(10))
    assert limiter.hit("ip|bob") is True  # 不同键不受 alice 超限影响
    assert limiter.hit("ip|alice") is False
    clock.advance(seconds=61)  # 全部事件滑出窗口
    assert limiter.hit("ip|alice") is True
    limiter.reset()
    assert limiter.hit("ip|alice") is True  # reset 后从零计数


def test_login_unknown_user_verifies_dummy_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTH-08 时序防探测：未知用户名路径必经一次 bcrypt 校验（作用于 dummy hash），
    且失败语义不变（2001/401、同文案）——用户是否存在无法从响应耗时探测。"""
    engine = create_engine("sqlite://")
    User.__table__.create(engine)  # 只建 users 表：单测无需 pgvector 等 pg 专有类型
    calls: list[tuple[str, str]] = []
    real_verify = auth.verify_password

    def spy(pw: str, hashed: str) -> bool:
        calls.append((pw, hashed))
        return real_verify(pw, hashed)

    monkeypatch.setattr(auth, "verify_password", spy)
    limiter = SlidingWindowLimiter(window_seconds=60, max_events=10, clock=FakeClock())
    try:
        with Session(engine) as db, pytest.raises(ApiError) as ei:
            login(db, "no_such_user", "任意密码", ip="203.0.113.9", limiter=limiter)
    finally:
        engine.dispose()

    assert len(calls) == 1  # 失败路径恰好一次校验：与真实用户验密耗时同量级
    assert calls[0] == ("任意密码", _DUMMY_HASH)  # 作用于 dummy hash：库中并无该用户行
    assert real_verify("任意密码", _DUMMY_HASH) is False  # dummy hash 不匹配任何口令
    assert (ei.value.code, ei.value.http_status) == (ERR_INVALID_CREDENTIALS, 401)
    assert ei.value.message == "用户名或密码错误"  # 与已知用户密码错误同码同文案
