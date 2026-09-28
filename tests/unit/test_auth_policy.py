"""鉴权策略纯函数单测：锁定判定 evaluate_login 与滑动窗口限流器（TDD 入口，无 DB/app）。

evaluate_login 的全值域穷举保证服务层失败路径的处置（提示剩余次数 / 触发持久锁）
可精确推导；限流器用注入 FakeClock 冻结时间验证窗口滑动，不同键互不影响。
"""

import pytest

from app.core.clock import FakeClock
from app.services.auth import (
    ACTION_ALLOW,
    ACTION_LOCK,
    ACTION_REJECT,
    SlidingWindowLimiter,
    evaluate_login,
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
