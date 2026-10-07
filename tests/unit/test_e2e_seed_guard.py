"""e2e_seed 硬复位防呆单测（不触库）：E2E_WIPE 只允许指向 E2E 隔离库。

2026-10-05 试点库被 E2E_WIPE TRUNCATE 清空事故的回归钉子：伪造 DATABASE_URL 指向
非隔离库时 main() 必须非零退出且在触库之前拦截（SessionLocal 打桩验证"未开启会话"）；
指向隔离库（rag_e2e / *_e2e 后缀）时放行；无 E2E_WIPE 的幂等 upsert 路径不受库名
限制（startup-check 试点冒烟是有意设计）。get_settings 为 lru_cache：改 env 后必须
cache_clear（config.py 缓存契约），用例前后各清一次避免污染同进程其他用例。
"""

import contextlib

import pytest

from app.core.config import get_settings
from app.db import e2e_seed


@pytest.fixture(autouse=True)
def _settings_cache_hygiene():
    """用例前后清 Settings 缓存：env 覆盖只在本用例生效，不外泄。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeTx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def begin(self) -> _FakeTx:
        return _FakeTx()

    def flush(self, *args: object, **kwargs: object) -> None:
        return None

    def execute(self, *args: object, **kwargs: object) -> None:
        return None


def _stub_db_layer(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """把 SessionLocal 与全部库内操作打桩：隔离断言通过后的主流程照常走完，不真连库。"""

    @contextlib.contextmanager
    def fake_session_local():
        calls.append("open")
        yield _FakeSession()

    monkeypatch.setattr(e2e_seed, "SessionLocal", fake_session_local)
    monkeypatch.setattr(e2e_seed, "_reset_lock_state", lambda s: calls.append("reset"))
    monkeypatch.setattr(e2e_seed, "_retire_previous_docs", lambda s: calls.append("retire"))
    monkeypatch.setattr(
        e2e_seed, "_upsert_user", lambda *a, **k: calls.append("upsert"), raising=False
    )
    monkeypatch.setattr(e2e_seed, "_upsert_seed_qa", lambda s: calls.append("qa"))


def _set_db(monkeypatch: pytest.MonkeyPatch, url: str, wipe: bool) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    if wipe:
        monkeypatch.setenv("E2E_WIPE", "1")
    else:
        monkeypatch.delenv("E2E_WIPE", raising=False)
    get_settings.cache_clear()


def test_wipe_rejects_pilot_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """E2E_WIPE + 试点库 rag：非零退出，且防呆在开启任何数据库会话之前生效。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg:5432/rag", wipe=True)
    with pytest.raises(SystemExit) as exc:
        e2e_seed.main()
    assert exc.value.code != 0
    assert "open" not in calls  # 关键：拒绝发生在触库之前
    err = capsys.readouterr().err
    assert "拒绝" in err and "rag" in err and "rag_e2e" in err


def test_wipe_accepts_isolated_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E_WIPE + 隔离库 rag_e2e：放行并走完种子主流程。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg-e2e:5432/rag_e2e", wipe=True)
    e2e_seed.main()
    assert "open" in calls
    assert calls.count("upsert") == 5  # admin/fresh/hr/svc/lock 五账号


def test_wipe_accepts_e2e_suffix_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E_WIPE + *_e2e 后缀库（如 rag_test_e2e）：白名单之外的合法隔离形态，放行。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg-e2e:5432/rag_test_e2e", wipe=True)
    e2e_seed.main()
    assert "open" in calls


def test_no_wipe_unaffected_on_pilot_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """无 E2E_WIPE + 试点库：幂等 upsert 不受库名限制（startup-check 试点冒烟口径）。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg:5432/rag", wipe=False)
    e2e_seed.main()
    assert "open" in calls
    assert "[e2e-seed] ok" in capsys.readouterr().out


def test_wipe_guard_rejects_missing_db_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E_WIPE + 无库名的连接串：不能默认放行，同样拒绝。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg:5432", wipe=True)
    with pytest.raises(SystemExit) as exc:
        e2e_seed.main()
    assert exc.value.code != 0
    assert "open" not in calls
