r"""熔断器状态机（core 纯类）+ rerank 协议装饰器（retrieval-policy spec §3/§11 裁决 3）。

分层裁决：状态机在 core（clock.py 同款纯类 + Clock 注入，无 IO 零业务依赖）；装配层
装饰器 CircuitBreakerReranker 同文件（协议装饰器无业务语义），在 main._build_chat_deps
包装 build_reranker 产物。状态机：closed --连续 3 次失败--> open --冷却 300s 满-->
half_open --单发探测成功回 closed 清零 / 失败回 open 重启冷却--> ...；half_open 探测期
其余请求直接拒绝（不并发探测）。失败口径=一次 rerank 调用抛 UpstreamError（tei 内部
重试后）计 1 次；成功重置连续计数。config 总闸 rerank_breaker_enabled 手动关闭时恒
closed（不记失败、恒放行）。开路拒绝不抛错——由装饰器转 UpstreamError，检索侧
rerank_and_fold 统一捕走向量序兜底（调用方零分支）。get_rerank_breaker() 进程级单例
（clock.get_clock 先例），services_health 与装配层共享同一状态。
"""

from datetime import datetime

from app.core.clock import Clock, SystemClock
from app.core.config import get_settings
from app.core.llm import Reranker, UpstreamError

CLOSED: str = "closed"
OPEN: str = "open"
HALF_OPEN: str = "half_open"

FAIL_THRESHOLD: int = 3  # 连续失败次数阈值 → open（spec §3：连续 3 次）
COOLDOWN_S: float = 300.0  # open 持续秒数 → half_open 单发探测（spec §3：300s）


class CircuitBreaker:
    """rerank 上游熔断三态机（进程内存，spec §3 参数：连续 3 次 → 开路 300s → 半开单发探测）。

    open→half_open 的冷却满转换在 state 读取侧统一发生（观测与放行同口径，免定时器）；
    half_open 只有一个探测名额（allow_request 占用），探测期其余请求直接拒绝。
    """

    def __init__(
        self,
        clock: Clock,
        *,
        fail_threshold: int = FAIL_THRESHOLD,
        cooldown_s: float = COOLDOWN_S,
        enabled: bool = True,
    ) -> None:
        self._clock = clock
        self._fail_threshold = fail_threshold
        self._cooldown_s = cooldown_s
        self._enabled = enabled
        self._state: str = CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: datetime | None = None
        self._probe_in_flight: bool = False

    @property
    def state(self) -> str:
        """当前三态（closed/open/half_open）；总闸关闭恒 closed（services_health 展示口径）。"""
        if not self._enabled:
            return CLOSED
        if self._state == OPEN and self._cooldown_elapsed():
            self._state = HALF_OPEN
            self._probe_in_flight = False
        return self._state

    @property
    def consecutive_failures(self) -> int:
        """当前连续失败计数（观测缝：测试与排障用，不参与状态判定入口）。"""
        return self._consecutive_failures

    def allow_request(self) -> bool:
        """是否放行一次真实 rerank 调用；False=调用方 fail-fast（装饰器转 UpstreamError）。"""
        if not self._enabled:
            return True
        if self.state == OPEN:
            return False
        if self.state == HALF_OPEN:
            if self._probe_in_flight:
                return False  # 探测期其余请求不并发探测（spec §3）
            self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        """真实调用成功：回 closed 并清零连续计数（仅真实放行的调用才回写）。"""
        if not self._enabled:
            return
        self._consecutive_failures = 0
        self._state = CLOSED
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        """真实调用失败：closed 态累加连续计数达阈值即 open；half_open 探测败回 open 重启冷却。"""
        if not self._enabled:
            return
        if self.state == HALF_OPEN:
            self._state = OPEN
            self._opened_at = self._clock.now()
            self._probe_in_flight = False
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._fail_threshold:
            self._state = OPEN
            self._opened_at = self._clock.now()

    def _cooldown_elapsed(self) -> bool:
        assert self._opened_at is not None  # open 态必然已记录开路时刻
        return (self._clock.now() - self._opened_at).total_seconds() >= self._cooldown_s


class CircuitBreakerReranker:
    """Reranker 协议装饰器（装配层选择，实现类只在装配层出现——standards §1.2）。

    开路（allow_request=False）直接抛 UpstreamError fail-fast，不触内层上游；放行则调
    内层并回写熔断成败——UpstreamError 计 1 次失败后原样上抛（检索侧 rerank_and_fold
    统一兜底），成功透传分数并清零计数。总闸关闭时 allow_request 恒真，装饰器退化为
    纯透传。
    """

    def __init__(self, inner: Reranker, breaker: CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        if not self._breaker.allow_request():
            raise UpstreamError("rerank 熔断开路，fail-fast 跳过上游（检索侧向量序兜底）")
        try:
            scores = self._inner.rerank(query, docs)
        except UpstreamError:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        return scores


_rerank_breaker: CircuitBreaker | None = None


def get_rerank_breaker() -> CircuitBreaker:
    """进程级单例（clock.get_clock 先例）：参数取模块常量，总闸读 Settings。"""
    global _rerank_breaker
    if _rerank_breaker is None:
        _rerank_breaker = CircuitBreaker(
            SystemClock(), enabled=get_settings().rerank_breaker_enabled
        )
    return _rerank_breaker


def set_rerank_breaker(breaker: CircuitBreaker | None) -> None:
    """测试/装配缝：None=复位为按当前 Settings 重建（clock.set_clock 先例）。"""
    global _rerank_breaker
    _rerank_breaker = breaker
