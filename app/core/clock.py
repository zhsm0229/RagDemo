"""可注入时钟：业务代码统一经 get_clock().now() 取时间，测试可换 FakeClock 拨动。"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        raise ValueError(f"clock datetime must be timezone-aware: {iso}")
    return dt


class FakeClock:
    """测试用时钟：固定起点，可 set 定位、advance 前进（禁止回拨）。"""

    def __init__(self, start: str = "2026-10-01T08:00:00+00:00") -> None:
        self._current = _parse(start)

    def set(self, iso: str) -> None:
        self._current = _parse(iso)

    def advance(self, **delta: float) -> None:
        step = timedelta(**delta)
        if step < timedelta(0):
            raise ValueError(f"clock cannot move backwards: {step}")
        self._current += step

    def now(self) -> datetime:
        return self._current


class SystemClock:
    """生产时钟：UTC 带时区，微秒归零（秒级精度，便于快照比较）。"""

    def now(self) -> datetime:
        return datetime.now(tz=UTC).replace(microsecond=0)


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> None:
    global _clock
    _clock = clock
