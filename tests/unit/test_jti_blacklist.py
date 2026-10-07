"""jti 登出黑名单测试（§5.3 / findings I4）：登出即失效，条目随令牌 exp 自然失效。

黑名单是进程内小服务（core/security.JtiBlacklist，§25.3-A 阶段可整体换 Redis），
单测一律注入自建实例（配 FakeClock，禁止 sleep），不触碰模块级 default_blacklist
——默认装配的联通性由 API 层登出用例覆盖（tests/api/test_auth.py，AUTH-08）。
"""

from collections.abc import Generator
from datetime import timedelta

import pytest

import app.core.clock as clock_mod
from app.core.clock import FakeClock, SystemClock, set_clock
from app.core.security import JtiBlacklist, create_token, decode_token

SECRET = "unit-test-secret-0123456789abcdef"  # 32 字节，满足 RFC 7518 HS256 推荐长度
USER_ID = 7
ROLE = "engineer"
DEPT_ID = 3


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> Generator[FakeClock, None, None]:
    """把 FakeClock 装入全局时钟：decode_token/黑名单均经 get_clock() 取当前时间。"""
    monkeypatch.setattr(clock_mod, "_clock", SystemClock())  # 测试后还原默认
    clock = FakeClock()
    set_clock(clock)
    yield clock


def _token(clock: FakeClock, expires_min: int = 30) -> str:
    return create_token(
        user_id=USER_ID,
        role=ROLE,
        dept_id=DEPT_ID,
        expires_min=expires_min,
        now=clock.now(),
        secret=SECRET,
    )


class TestJtiBlacklist:
    def test_revoke_then_decode_returns_none(self, fake_clock: FakeClock) -> None:
        """登出即失效：撤销前 decode 正常，撤销后同一黑名单下 decode 返回 None。"""
        blacklist = JtiBlacklist(clock=fake_clock)
        token = _token(fake_clock)
        payload = decode_token(token, SECRET, blacklist)
        assert payload is not None  # 未登出：令牌有效
        blacklist.revoke(payload.jti, payload.exp)
        assert decode_token(token, SECRET, blacklist) is None  # 已登出：同口径视同无效

    def test_revoke_scoped_to_instance(self, fake_clock: FakeClock) -> None:
        """黑名单按实例隔离：撤销只影响所属实例，换新实例（或替换实现）不牵连。"""
        revoked_list = JtiBlacklist(clock=fake_clock)
        token = _token(fake_clock)
        payload = decode_token(token, SECRET, revoked_list)
        assert payload is not None
        revoked_list.revoke(payload.jti, payload.exp)
        fresh = JtiBlacklist(clock=fake_clock)
        assert decode_token(token, SECRET, fresh) is not None  # 别的实例不受影响

    def test_payload_carries_exp_timestamp(self, fake_clock: FakeClock) -> None:
        """TokenPayload.exp 为令牌过期时刻的 Unix 秒级时间戳（黑名单条目 TTL 依据）。"""
        payload = decode_token(_token(fake_clock, expires_min=30), SECRET)
        assert payload is not None
        assert payload.exp == (fake_clock.now() + timedelta(minutes=30)).timestamp()

    def test_entry_expires_with_token_exp(self, fake_clock: FakeClock) -> None:
        """过期治理：条目随原令牌 exp 自然失效（§5.3 TTL=剩余有效期），到期后
        revoked 恢复 False（此时令牌本身也已过期，decode 同样返回 None）。"""
        blacklist = JtiBlacklist(clock=fake_clock)
        token = _token(fake_clock, expires_min=1)
        payload = decode_token(token, SECRET, blacklist)
        assert payload is not None
        blacklist.revoke(payload.jti, payload.exp)
        assert blacklist.revoked(payload.jti) is True
        fake_clock.advance(minutes=2)  # 拨过令牌 exp：条目应视同不存在
        assert blacklist.revoked(payload.jti) is False
        assert decode_token(token, SECRET, blacklist) is None  # 令牌过期兜底仍然有效

    def test_revoke_purges_stale_and_keeps_live(self, fake_clock: FakeClock) -> None:
        """容量治理：revoke 时顺手清理已过期条目，未过期条目不受影响。"""
        blacklist = JtiBlacklist(clock=fake_clock)
        blacklist.revoke("stale-jti", fake_clock.now().timestamp() + 60)
        fake_clock.advance(minutes=2)  # stale-jti 已过其 exp
        live_exp = (fake_clock.now() + timedelta(hours=1)).timestamp()
        blacklist.revoke("live-jti", live_exp)  # 触发清理，同时写入新条目
        assert blacklist.revoked("stale-jti") is False  # 已被清出
        assert blacklist.revoked("live-jti") is True  # 未过期条目保留

    def test_revoke_ignores_already_expired_token(self, fake_clock: FakeClock) -> None:
        """已过期的令牌无记入意义（decode 本就拒绝）：revoke 不产生有效条目。"""
        blacklist = JtiBlacklist(clock=fake_clock)
        blacklist.revoke("dead-jti", fake_clock.now().timestamp() - 1)
        assert blacklist.revoked("dead-jti") is False

    def test_reset_clears_all_entries(self, fake_clock: FakeClock) -> None:
        """reset 清空黑名单（测试隔离用；生产进程重启等效，§5.3）。"""
        blacklist = JtiBlacklist(clock=fake_clock)
        blacklist.revoke("some-jti", fake_clock.now().timestamp() + 60)
        assert blacklist.revoked("some-jti") is True
        blacklist.reset()
        assert blacklist.revoked("some-jti") is False
