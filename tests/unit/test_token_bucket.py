r"""令牌桶分级限流单元测试（工单 21 设计 §5.1，FakeClock 注入，全中文标题）。

断言清单来源 design/21 §5.1 八条：突发放行 / 超突发即拒且拒绝不扣水位 / 懒补充速率
精确 / 水位封顶 burst 不超储 / scope×key 隔离 / reset 恢复满水位 / 并发恰放行
（Barrier + tiny_switch_interval，同 test_chat_cache.py 竞态先例）/ bucket=None 接线
短路（字段缺省兜底：经真实端点函数验证守卫短路后流程继续走到日配额闸）。
"""

import sys
import threading
from dataclasses import replace

import pytest
from fastapi import BackgroundTasks, FastAPI
from starlette.requests import Request

from app.api.chat import ChatStreamRequest, chat_stream
from app.core.clock import FakeClock
from app.core.config import Settings
from app.core.errors import ERR_DAILY_LIMITED, ApiError
from app.db.models import User
from app.main import _build_chat_deps
from app.services.chat import ChatDeps
from app.services.chat_cache import TokenBucketLimiter


def _clock() -> FakeClock:
    return FakeClock("2026-10-15T09:30:00+00:00")


@pytest.fixture()
def tiny_switch_interval():
    """把 GIL 切换间隔压到微秒级放大线程切换频率（同 test_chat_cache.py 竞态先例）。"""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


# ---------- ①-⑥ 桶语义（FakeClock 精确推演） ----------


def test_突发容量内连发全放行() -> None:
    """burst=3 首见键连发 3 次全 True（突发额度即桶深）。"""
    bucket = TokenBucketLimiter(_clock())
    results = [bucket.try_acquire("chat", "7", rate_per_min=30, burst=3) for _ in range(3)]
    assert results == [True, True, True]


def test_超突发即拒且拒绝不扣水位() -> None:
    """第 4 次 False；前拨恰补 1 令牌时长（60/rate 秒）后恰放行 1 次——拒绝轮不消耗水位。"""
    clock = _clock()
    bucket = TokenBucketLimiter(clock)
    for _ in range(3):
        assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=3) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=3) is False
    clock.advance(seconds=1)  # rate=60/min=1/s → 恰补 1 令牌
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=3) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=3) is False


def test_懒补充速率精确() -> None:
    """rate=60/min（1/s）：耗尽后每前拨 1s 恰放行 1 次；前拨 0.5s 放行为 False。"""
    clock = _clock()
    bucket = TokenBucketLimiter(clock)
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is False
    clock.advance(seconds=1)
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is False
    clock.advance(seconds=1)
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is False
    clock.advance(seconds=0.5)  # 半个令牌不足一次消费
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is False
    clock.advance(seconds=0.5)  # 凑满 1 令牌
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True


def test_水位封顶_burst不超储() -> None:
    """耗尽后闲置远超 burst 时长（rate=1/s、burst=3 → 补满仅需 3s，闲置 1000s），
    仍至多连发 burst 次——线性补充封顶，不超储。"""
    clock = _clock()
    bucket = TokenBucketLimiter(clock)
    for _ in range(3):
        assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=3) is True
    clock.advance(seconds=1000)
    results = [bucket.try_acquire("chat", "7", rate_per_min=60, burst=3) for _ in range(4)]
    assert results == [True, True, True, False]


def test_scope隔离_chat耗尽不影响agent与异user() -> None:
    """scope 隔离：chat 耗尽不影响 agent 同 key 首发放行；同 scope 不同 user 隔离。"""
    clock = _clock()
    bucket = TokenBucketLimiter(clock)
    for _ in range(2):
        assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is False
    assert bucket.try_acquire("agent", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "9", rate_per_min=60, burst=2) is True


def test_reset清空后恢复满水位() -> None:
    """reset() 清空全部桶：原已耗尽键恢复满水位可再连发 burst 次。"""
    clock = _clock()
    bucket = TokenBucketLimiter(clock)
    for _ in range(2):
        assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is False
    bucket.reset()
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is True
    assert bucket.try_acquire("chat", "7", rate_per_min=60, burst=2) is False


# ---------- ⑦ 并发恰放行（竞态先例同款） ----------


def test_并发恰放行_burst次(tiny_switch_interval) -> None:
    """8 线程 × 10 次（FakeClock 冻结无补充）burst=10：放行恰 10 次、拒绝恰 70 次。"""
    bucket = TokenBucketLimiter(_clock())
    burst, workers, calls = 10, 8, 10
    gate = threading.Barrier(workers)
    results: list[bool] = []
    collect = threading.Lock()

    def worker() -> None:
        gate.wait()
        local = [
            bucket.try_acquire("chat", "42", rate_per_min=1, burst=burst) for _ in range(calls)
        ]
        with collect:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == burst
    assert results.count(False) == workers * calls - burst


# ---------- ⑧ bucket=None 接线短路（字段缺省兜底） ----------


class _RejectCounter:
    """日配额闸替身：恒拒绝——以它抛出的 ApiError 为墙，证明限流守卫已短路通过。"""

    def try_consume(self, user_id: int, limit: int) -> bool:
        return False


def test_bucket为None时接线短路不抛() -> None:
    """deps.bucket=None（PG 分支/关闸装配形态）+ rate_limiter_enabled=True：端点守卫
    `bucket is not None` 短路——不抛 AttributeError、不产限流 429，流程继续走到日配额
    闸（以 counter 拒绝的 ApiError 3002 为墙，§3 字段缺省 None 契约）。"""
    settings = Settings(jwt_secret="tb-wiring-test", test_mode=True, rate_limiter_enabled=True)

    class _Any:
        """store 槽位占位：本用例在日配额闸即收口，检索件不被触达。"""

    deps: ChatDeps = replace(
        _build_chat_deps(settings), bucket=None, counter=_RejectCounter(), store=_Any()
    )
    app = FastAPI()
    app.state.chat_deps = deps
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/chat/stream",
            "headers": [],
            "query_string": b"",
        }
    )
    user = User(id=7, username="u", dept_id=1, role="member", is_active=True)
    body = ChatStreamRequest(question="你好")

    with pytest.raises(ApiError) as exc_info:
        chat_stream(body, user, None, request, BackgroundTasks())  # type: ignore[arg-type]

    assert exc_info.value.code == ERR_DAILY_LIMITED  # 走到日配额闸而非限流闸/崩溃
