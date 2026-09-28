from datetime import UTC, datetime

import pytest

import app.core.clock as clock_mod
from app.core.clock import FakeClock, SystemClock, get_clock, set_clock


class TestFakeClock:
    def test_default_start(self) -> None:
        clock = FakeClock()
        assert clock.now() == datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)

    def test_advance_minutes(self) -> None:
        clock = FakeClock()
        clock.advance(minutes=5)
        assert clock.now() == datetime(2026, 10, 1, 8, 5, 0, tzinfo=UTC)

    def test_set_iso(self) -> None:
        clock = FakeClock()
        clock.set("2026-12-31T23:59:59+08:00")
        assert clock.now() == datetime.fromisoformat("2026-12-31T23:59:59+08:00")

    def test_advance_negative_raises(self) -> None:
        clock = FakeClock()
        with pytest.raises(ValueError):
            clock.advance(minutes=-1)

    def test_set_naive_datetime_raises(self) -> None:
        clock = FakeClock()
        with pytest.raises(ValueError):
            clock.set("2026-10-01T08:00:00")  # 无时区，禁止（避免 aware/naive 混比）


class TestSystemClock:
    def test_now_is_tz_aware_and_second_precision(self) -> None:
        before = datetime.now(tz=UTC).replace(microsecond=0)
        now = SystemClock().now()
        after = datetime.now(tz=UTC).replace(microsecond=0)
        assert before <= now <= after  # 与真实时间一致且落在调用区间内
        assert now.tzinfo is not None
        assert now.microsecond == 0


class TestGlobalClock:
    def test_get_clock_defaults_to_system_clock(self) -> None:
        assert isinstance(get_clock(), SystemClock)

    def test_set_clock_overrides_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(clock_mod, "_clock", SystemClock())  # 测试后还原默认
        fake = FakeClock()
        set_clock(fake)
        assert get_clock() is fake
