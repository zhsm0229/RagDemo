r"""调度器开关与抢主装配单元测试（工单 20 设计 §7，不触库）。

- 开关缺省 true：非 test_mode 下启动（现状回归）——R4 修订（design/20 §10）：抢主以
  monkeypatch try_acquire_leader 恒 True 隔离，选举语义由
  tests/integration/test_scheduler_leader.py 真库承载。
- scheduler_enabled=false：任务装配完整但 running=False、is_leader=False，且不触抢锁。
- test_mode=true：恒不启动（既有语义回归，短路顺序不变）。
- catchup leader-gate（§3.2）：main.lifespan 仅在 scheduler.is_leader 为真时起补偿线程，
  属性缺席按 False 兜底。以直调 lifespan + monkeypatch 装配函数的单元形态验证——
  test_mode 的 API 测试栈恒 is_leader=False，无法覆盖 leader=True 分支，故不落
  tests/api（design/20 §7 原列 tests/api/test_lifespan_catchup.py，归属调整理由见
  本 docstring，行为契约不变）。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

import app.main as main_module
from app.core.clock import FakeClock
from app.core.config import Settings
from app.main import _startup_catchup, lifespan
from app.services.chat_cache import HitCounter
from app.services.scheduler import build_scheduler


def _settings(**overrides: object) -> Settings:
    return Settings(jwt_secret="scheduler-flag-test", **overrides)  # type: ignore[arg-type]


# ---------- §7 三条：启动闸 ----------


def test_开关缺省true_非test_mode下build_scheduler启动(monkeypatch) -> None:
    """scheduler_enabled 缺省 True：非 test_mode 启动且 is_leader=True（启动语义现状
    回归；选举以 monkeypatch 恒 True 隔离，R4）。"""
    monkeypatch.setattr("app.services.scheduler.try_acquire_leader", lambda settings: True)
    scheduler = build_scheduler(_settings(test_mode=False))
    try:
        assert scheduler.running is True
        assert scheduler.is_leader is True
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_enabled_false_任务装配完整但不启动且不抢锁(monkeypatch) -> None:
    """开关关：任务注册表完整（开关只影响 start）、running=False、is_leader=False，
    抢锁函数不被触达（booby-trap 钉死短路顺序）。"""

    def _boom(settings: Settings) -> bool:
        raise AssertionError("scheduler_enabled=false 不得触达抢锁")

    monkeypatch.setattr("app.services.scheduler.try_acquire_leader", _boom)
    scheduler = build_scheduler(_settings(test_mode=False, scheduler_enabled=False))
    assert scheduler.running is False
    assert scheduler.is_leader is False
    assert {job.id for job in scheduler.get_jobs()} == {
        "run_due_sweeps",
        "message_retention_cleanup",
        "ensure_month_partitions",
        "agent_run_stale_sweep",
        "eval_run_stale_sweep",
        "eval_auto_synth",
        "eval_auto_run",
    }


def test_test_mode_true_恒不start() -> None:
    """test_mode 短路顺序不变：恒不启动、is_leader=False（测试进程不起后台线程，
    也不触库）。"""
    scheduler = build_scheduler(_settings(test_mode=True))
    assert scheduler.running is False
    assert scheduler.is_leader is False


# ---------- §3.2 catchup leader-gate（直调 lifespan） ----------


class _StubScheduler:
    """build_scheduler 替身：is_leader 可缺席（模拟属性兜底路径），不真跑任务。"""

    def __init__(self, is_leader: bool | None) -> None:
        if is_leader is not None:
            self.is_leader = is_leader
        self.running = False

    def add_job(self, *args: object, **kwargs: object) -> None:
        return None

    def shutdown(self, wait: bool = False) -> None:
        self.running = False


def _drive_lifespan(monkeypatch: pytest.MonkeyPatch, scheduler: _StubScheduler) -> list[tuple]:
    """直调 main.lifespan：装配函数全换无网络/无库桩，记录 Thread 派生（target/args/name）。"""
    settings = _settings(test_mode=False)
    spawned: list[tuple] = []

    class _FakeThread:
        def __init__(self, *, target=None, args=(), daemon=None, name=None) -> None:
            spawned.append((target, args, name or ""))

        def start(self) -> None:
            return None

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_module,
        "_build_chat_deps",
        lambda settings, state=None: SimpleNamespace(
            hit_counter=HitCounter(FakeClock("2026-10-31T08:00:00+00:00"))
        ),
    )
    monkeypatch.setattr(main_module, "_build_agent_deps", lambda settings, chat_deps: object())
    monkeypatch.setattr(main_module, "build_scheduler", lambda settings: scheduler)
    monkeypatch.setattr(main_module, "threading", SimpleNamespace(Thread=_FakeThread))

    async def _run() -> None:
        async with lifespan(FastAPI()):
            pass

    asyncio.run(_run())
    return spawned


def test_catchup线程仅在leader启动(monkeypatch) -> None:
    """leader：恰起一条补偿线程（target=_startup_catchup、name=eval-catchup）；
    非 leader：零线程（N worker 各跑一遍 = N 倍 LLM 账单的收敛裁决，§3.2）。"""
    leader_spawned = _drive_lifespan(monkeypatch, _StubScheduler(is_leader=True))
    assert len(leader_spawned) == 1
    target, _args, name = leader_spawned[0]
    assert target is _startup_catchup
    assert name == "eval-catchup"

    non_leader_spawned = _drive_lifespan(monkeypatch, _StubScheduler(is_leader=False))
    assert non_leader_spawned == []


def test_catchup线程_is_leader属性缺席按False兜底(monkeypatch) -> None:
    """scheduler 未挂 is_leader 属性（getattr 缺省 False）：不起补偿线程——启动闸与
    选举未运行的形态（如 test_mode 短路）零副作用。"""
    spawned = _drive_lifespan(monkeypatch, _StubScheduler(is_leader=None))
    assert spawned == []
