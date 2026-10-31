"""e2e_seed 硬复位与种子账号防呆单测（不触库）：E2E_WIPE 只允许指向 E2E 隔离库。

2026-10-05 试点库被 E2E_WIPE TRUNCATE 清空事故的回归钉子：伪造 DATABASE_URL 指向
非隔离库时 main() 必须非零退出且在触库之前拦截（SessionLocal 打桩验证"未开启会话"）；
指向隔离库（rag_e2e / *_e2e 后缀）时放行。2026-10-31 商用就绪工单 04（评审 P0-2
公开口令管理员后门）追加非隔离库 upsert 防呆：无 wipe 的幂等 upsert 在非隔离库
必须 PILOT_CONFIRM=1 + E2E_PASSWORD 显式设置才放行（禁止公开缺省口令进入试点/业务
库），且已存在账号的口令/首登改密标记不被重置——"改了也会被装回"形态的回归钉子。
get_settings 为 lru_cache：改 env 后必须 cache_clear（config.py 缓存契约），用例
前后各清一次避免污染同进程其他用例。
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


def _set_db(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    wipe: bool,
    *,
    confirm: str | None = None,
    password: str | None = None,
) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    if wipe:
        monkeypatch.setenv("E2E_WIPE", "1")
    else:
        monkeypatch.delenv("E2E_WIPE", raising=False)
    if confirm is None:
        monkeypatch.delenv("PILOT_CONFIRM", raising=False)
    else:
        monkeypatch.setenv("PILOT_CONFIRM", confirm)
    if password is None:
        monkeypatch.delenv("E2E_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("E2E_PASSWORD", password)
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
    """无 E2E_WIPE + 试点库 + 未确认：拒绝（工单 04 新防呆，改写自旧"不受库名限制"口径）。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg:5432/rag", wipe=False)
    with pytest.raises(SystemExit) as exc:
        e2e_seed.main()
    assert exc.value.code != 0
    assert "open" not in calls  # 拒绝发生在触库之前


def test_no_wipe_pilot_db_requires_pilot_confirm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """非隔离库 upsert 无 PILOT_CONFIRM：拒绝且不触库（公开口令管理员后门防呆）。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg:5432/rag", wipe=False)
    with pytest.raises(SystemExit) as exc:
        e2e_seed.main()
    assert exc.value.code != 0
    assert "open" not in calls
    assert "PILOT_CONFIRM" in capsys.readouterr().err


def test_no_wipe_pilot_db_rejects_default_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """非隔离库确认放行但 E2E_PASSWORD 未显式设置：拒绝——公开缺省口令禁止进入试点/业务库。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg:5432/rag", wipe=False, confirm="1")
    with pytest.raises(SystemExit) as exc:
        e2e_seed.main()
    assert exc.value.code != 0
    assert "open" not in calls
    assert "E2E_PASSWORD" in capsys.readouterr().err


def test_no_wipe_pilot_db_confirm_with_explicit_password_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """非隔离库 PILOT_CONFIRM=1 + 显式 E2E_PASSWORD：放行走完种子（试点装配正道）。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(
        monkeypatch,
        "postgresql+psycopg://rag:secret@pg:5432/rag",
        wipe=False,
        confirm="1",
        password="试点专用口令",
    )
    e2e_seed.main()
    assert "open" in calls
    assert calls.count("upsert") == 5
    assert "[e2e-seed] ok" in capsys.readouterr().out


class _First:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _UpsertSession:
    """_upsert_user 直接单测的最小 session 替身：scalars().first() 按预置返回。"""

    def __init__(self, existing: object | None, dept_id: int = 2) -> None:
        self._existing = existing
        self._dept_id = dept_id
        self.added: list[object] = []

    def scalars(self, stmt: object) -> _First:
        stmt_text = str(stmt)
        if "Department" in stmt_text or "departments" in stmt_text:
            return _First([self._dept_id])
        return _First([self._existing] if self._existing is not None else [])

    def add(self, obj: object) -> None:
        self.added.append(obj)


class _ExistingUser:
    """已存在账号替身：人工已改密（哈希与种子口令不匹配）形态。"""

    def __init__(self) -> None:
        self.username = "e2e-admin"
        self.password_hash = "人工改密后的哈希"
        self.must_change_password = False
        self.is_active = False


def test_upsert_preserves_changed_password_in_pilot_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """试点模式 upsert 已存在账号：口令与首登改密标记不回写（"改了也会被装回"回归钉子）。"""
    monkeypatch.setattr(e2e_seed, "hash_password", lambda pw: f"种子哈希({pw})")
    session = _UpsertSession(_ExistingUser())
    e2e_seed._upsert_user(
        session, "e2e-admin", "admin", "all", must_change=True, preserve_credentials=True
    )
    user = session.scalars("User").first()
    assert user is not None
    assert user.password_hash == "人工改密后的哈希"  # 口令不被重置
    assert user.must_change_password is False  # 已改密用户不再被拉回强制改密
    assert user.is_active is True  # 仅同步激活态


def test_upsert_resets_password_in_test_db_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试库模式 upsert 已存在账号：口令照旧重置为种子口令（e2e 可重复性不回退）。"""
    monkeypatch.setattr(e2e_seed, "hash_password", lambda pw: f"种子哈希({pw})")
    session = _UpsertSession(_ExistingUser())
    e2e_seed._upsert_user(
        session, "e2e-admin", "admin", "all", must_change=True, preserve_credentials=False
    )
    user = session.scalars("User").first()
    assert user is not None
    assert user.password_hash.startswith("种子哈希(")  # 口令被重置为种子口令哈希


def test_wipe_guard_rejects_missing_db_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E_WIPE + 无库名的连接串：不能默认放行，同样拒绝。"""
    calls: list[str] = []
    _stub_db_layer(monkeypatch, calls)
    _set_db(monkeypatch, "postgresql+psycopg://rag:secret@pg:5432", wipe=True)
    with pytest.raises(SystemExit) as exc:
        e2e_seed.main()
    assert exc.value.code != 0
    assert "open" not in calls
