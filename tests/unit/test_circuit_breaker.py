"""熔断器状态机与 rerank 装饰器单测（retrieval-policy spec §3/§8/§11 裁决 3）。

FakeClock 注入驱动状态机全路径（3 败→open→299s 拒绝→300s half_open→成败两向→
总闸关闭恒 closed→成功清零）；装饰器 CircuitBreakerReranker 断言 fail-fast 不触内层、
失败计 1 次、成功透传。全 Stub 零网络（CPU TEI 基线在跑，严禁真容器故障注入）。
"""

from pathlib import Path

import pytest

from app.core import circuit_breaker
from app.core.circuit_breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitBreakerReranker,
    get_rerank_breaker,
    set_rerank_breaker,
)
from app.core.clock import FakeClock
from app.core.llm import Reranker, StubReranker, UpstreamError


@pytest.fixture(autouse=True)
def _reset_breaker_singleton():
    """单例缝用例间隔离：进出各复位一次，防开路状态泄漏到后续用例。"""
    set_rerank_breaker(None)
    yield
    set_rerank_breaker(None)


def _fail(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        breaker.record_failure()


class TestCircuitBreakerStateMachine:
    def test_initial_state_is_closed_and_allows_requests(self) -> None:
        breaker = CircuitBreaker(FakeClock())
        assert breaker.state == CLOSED
        assert breaker.allow_request() is True

    def test_failures_below_threshold_stay_closed(self) -> None:
        """连续失败不足 3 次不开路：2 败后仍 closed 且放行（spec §3：连续 3 次才开）。"""
        breaker = CircuitBreaker(FakeClock())
        _fail(breaker, 2)
        assert breaker.state == CLOSED
        assert breaker.allow_request() is True

    def test_three_consecutive_failures_open_the_breaker(self) -> None:
        breaker = CircuitBreaker(FakeClock())
        _fail(breaker, 3)
        assert breaker.state == OPEN
        assert breaker.allow_request() is False

    def test_success_resets_consecutive_failure_count(self) -> None:
        """成功重置连续计数（spec §3：成功重置）：2 败 1 成后再 2 败不开路。"""
        breaker = CircuitBreaker(FakeClock())
        _fail(breaker, 2)
        breaker.record_success()
        _fail(breaker, 2)
        assert breaker.state == CLOSED
        assert breaker.allow_request() is True

    def test_open_rejects_before_cooldown_elapsed(self) -> None:
        """开路 299s 仍拒绝（冷却 300s 未满，spec §5 竞态修复口径的后端侧）。"""
        clock = FakeClock()
        breaker = CircuitBreaker(clock)
        _fail(breaker, 3)
        clock.advance(seconds=299)
        assert breaker.state == OPEN
        assert breaker.allow_request() is False

    def test_cooldown_elapsed_turns_half_open_and_allows_single_probe(self) -> None:
        """开路满 300s 转 half_open：单发探测被放行（spec §3 半开单发探测）。"""
        clock = FakeClock()
        breaker = CircuitBreaker(clock)
        _fail(breaker, 3)
        clock.advance(seconds=300)
        assert breaker.state == HALF_OPEN
        assert breaker.allow_request() is True

    def test_half_open_rejects_concurrent_requests_during_probe(self) -> None:
        """探测期其余请求直接拒绝（不并发探测，spec §3/§11 裁决 3）：第二请求即拒。"""
        clock = FakeClock()
        breaker = CircuitBreaker(clock)
        _fail(breaker, 3)
        clock.advance(seconds=300)
        assert breaker.allow_request() is True  # 探测名额被占用
        assert breaker.allow_request() is False  # 其余请求直接 vector_only
        assert breaker.state == HALF_OPEN

    def test_probe_success_returns_to_closed_and_clears_count(self) -> None:
        """探测成功回 closed 且清零（spec §3：成功回 closed 清零计数）：再 2 败不开路。"""
        clock = FakeClock()
        breaker = CircuitBreaker(clock)
        _fail(breaker, 3)
        clock.advance(seconds=300)
        assert breaker.allow_request() is True
        breaker.record_success()
        assert breaker.state == CLOSED
        _fail(breaker, 2)
        assert breaker.state == CLOSED
        assert breaker.allow_request() is True

    def test_probe_failure_reopens_and_restarts_cooldown(self) -> None:
        """探测失败回 open 并重启冷却：新冷却 299s 内拒绝、满 300s 再放行探测。"""
        clock = FakeClock()
        breaker = CircuitBreaker(clock)
        _fail(breaker, 3)
        clock.advance(seconds=300)
        assert breaker.allow_request() is True
        breaker.record_failure()
        assert breaker.state == OPEN
        clock.advance(seconds=299)
        assert breaker.allow_request() is False
        clock.advance(seconds=1)
        assert breaker.state == HALF_OPEN
        assert breaker.allow_request() is True

    def test_disabled_breaker_stays_closed_and_always_allows(self) -> None:
        """总闸关闭恒 closed（spec §3 config 总闸手动关）：3 败后仍 closed 恒放行。"""
        clock = FakeClock()
        breaker = CircuitBreaker(clock, enabled=False)
        _fail(breaker, 3)
        assert breaker.state == CLOSED
        assert breaker.allow_request() is True


class TestCircuitBreakerReranker:
    def test_open_circuit_fails_fast_without_touching_inner_reranker(self) -> None:
        """开路 fail-fast：直接抛 UpstreamError 且内层零调用（不触上游，spec §3）。"""
        breaker = CircuitBreaker(FakeClock())
        _fail(breaker, 3)
        inner = StubReranker()
        decorator = CircuitBreakerReranker(inner, breaker)

        with pytest.raises(UpstreamError):
            decorator.rerank("查询", ["文档"])

        assert inner.rerank_calls == 0

    def test_inner_upstream_failure_counts_one_and_reraises(self) -> None:
        """内层抛 UpstreamError：计 1 次失败并原样上抛（失败口径=一次调用抛错计 1 次）。"""
        clock = FakeClock()
        breaker = CircuitBreaker(clock)
        decorator = CircuitBreakerReranker(StubReranker(fail_times=1), breaker)

        with pytest.raises(UpstreamError):
            decorator.rerank("查询", ["文档"])

        assert breaker.consecutive_failures == 1
        assert breaker.state == CLOSED  # 未达 3 次：仍 closed

    def test_success_passes_scores_through_and_resets_count(self) -> None:
        """成功透传分数并记录成功：连续计数清零（此前失败不再累积）。"""
        breaker = CircuitBreaker(FakeClock())
        breaker.record_failure()
        decorator = CircuitBreakerReranker(StubReranker(weights=[2.0, 1.0]), breaker)

        scores = decorator.rerank("查询", ["甲", "乙"])

        assert len(scores) == 2 and all(0.0 < score < 1.0 for score in scores)
        assert breaker.consecutive_failures == 0
        assert breaker.state == CLOSED

    def test_decorator_satisfies_reranker_protocol(self) -> None:
        """装饰器实现 Reranker 协议（装配层可无缝替换 build_reranker 产物）。"""
        decorator = CircuitBreakerReranker(StubReranker(), CircuitBreaker(FakeClock()))
        assert isinstance(decorator, Reranker)


class TestModuleSeams:
    def test_get_rerank_breaker_returns_process_singleton(self, monkeypatch) -> None:
        """访问器单例语义（clock.get_clock 先例）：set 注入后 get 返回同一实例。"""
        breaker = CircuitBreaker(FakeClock())
        set_rerank_breaker(breaker)
        assert get_rerank_breaker() is breaker

    def test_reset_rebuilds_breaker_from_settings_gate(self, monkeypatch) -> None:
        """复位（None）后按当前 Settings 重建：总闸开关随装配读数生效。"""

        class _StubSettings:
            rerank_breaker_enabled = False

        monkeypatch.setattr(circuit_breaker, "get_settings", lambda: _StubSettings())
        set_rerank_breaker(None)

        rebuilt = get_rerank_breaker()
        assert rebuilt is not None
        assert rebuilt.state == CLOSED  # 总闸关闭：恒 closed
        _fail(rebuilt, 3)
        assert rebuilt.state == CLOSED
        assert rebuilt.allow_request() is True

    def test_core_module_never_imports_services_or_db(self) -> None:
        """模块结构红线（五层单向依赖）：core 纯类域禁 import services/db。"""
        source = Path(circuit_breaker.__file__).read_text(encoding="utf-8")
        assert "app.services" not in source
        assert "app.db" not in source
