"""安全模块测试：bcrypt 口令哈希 + HS256 JWT 签发/解码。

参数化表来源 task-3-brief（AUTH 用例）：过期场景用 FakeClock 拨时间，禁止 sleep。
"""

from collections.abc import Generator
from datetime import datetime

import jwt
import pytest

import app.core.clock as clock_mod
from app.core.clock import FakeClock, SystemClock, set_clock
from app.core.security import (
    TokenPayload,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)

SECRET = "unit-test-secret-0123456789abcdef"  # 32 字节，满足 RFC 7518 HS256 推荐长度
WRONG_SECRET = "another-unit-test-secret-0123456789abcd"
USER_ID = 7
ROLE = "engineer"
DEPT_ID = 3


def _token(clock: FakeClock, *, expires_min: int = 30, user_id: int = USER_ID) -> str:
    return create_token(
        user_id=user_id,
        role=ROLE,
        dept_id=DEPT_ID,
        expires_min=expires_min,
        now=clock.now(),
        secret=SECRET,
    )


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> Generator[FakeClock, None, None]:
    """把 FakeClock 装入全局时钟：decode_token 经 get_clock() 取"当前时间"。"""
    monkeypatch.setattr(clock_mod, "_clock", SystemClock())  # 测试后还原默认
    clock = FakeClock()
    set_clock(clock)
    yield clock


class TestPasswordHashing:
    @pytest.mark.parametrize("password", ["s3cret-Pass", "中文密码#123", "p"])
    def test_verify_correct_password_returns_true(self, password: str) -> None:
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed) is True

    @pytest.mark.parametrize(
        ("password", "wrong"),
        [("s3cret-Pass", "s3cret-pass"), ("中文密码#123", "中文密码#124"), ("p", "q")],
    )
    def test_verify_wrong_password_returns_false(self, password: str, wrong: str) -> None:
        assert verify_password(wrong, hash_password(password)) is False


class TestToken:
    def test_create_then_decode_roundtrip(self, fake_clock: FakeClock) -> None:
        payload = decode_token(_token(fake_clock), SECRET)
        assert isinstance(payload, TokenPayload)
        assert payload.user_id == USER_ID  # 与签发入参逐字段一致
        assert payload.role == ROLE
        assert payload.dept_id == DEPT_ID
        assert isinstance(payload.jti, str) and len(payload.jti) == 32  # uuid4().hex

    def test_expired_token_decodes_to_none(self, fake_clock: FakeClock) -> None:
        token = create_token(
            user_id=USER_ID,
            role=ROLE,
            dept_id=DEPT_ID,
            expires_min=1,
            now=fake_clock.now(),
            secret=SECRET,
        )
        fake_clock.advance(minutes=2)  # exp 已过：签发于 now+1min，现位于 now+2min
        assert decode_token(token, SECRET) is None

    def test_tampered_first_byte_token_decodes_to_none(self, fake_clock: FakeClock) -> None:
        token = _token(fake_clock)
        flipped = ("X" if token[0] != "X" else "Y") + token[1:]
        assert flipped != token
        assert decode_token(flipped, SECRET) is None

    def test_wrong_secret_decodes_to_none(self, fake_clock: FakeClock) -> None:
        assert decode_token(_token(fake_clock), WRONG_SECRET) is None


class TestCreateTokenClaims:
    def test_claims_match_inputs(self) -> None:
        clock = FakeClock()
        now: datetime = clock.now()
        token = create_token(
            user_id=USER_ID, role=ROLE, dept_id=DEPT_ID, expires_min=30, now=now, secret=SECRET
        )
        claims = jwt.decode(
            token,
            SECRET,
            algorithms=["HS256"],
            options={"verify_iat": False},  # FakeClock 起点晚于真实系统时间，iat 未来校验不适用
        )
        assert claims["sub"] == str(USER_ID)
        assert claims["role"] == ROLE
        assert claims["dept"] == str(DEPT_ID)
        assert claims["iat"] == int(now.timestamp())
        assert claims["exp"] == int(now.timestamp()) + 30 * 60
        assert isinstance(claims["jti"], str) and len(claims["jti"]) == 32
