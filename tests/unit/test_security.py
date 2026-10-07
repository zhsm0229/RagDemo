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


class TestPassword72ByteLimit:
    """bcrypt 算法上限 72 字节：现状固化（hash 抛 ValueError / verify 返回 False），API 层负责校验。"""

    def test_exactly_72_bytes_hashes_and_verifies(self) -> None:
        pw = "a" * 72
        assert verify_password(pw, hash_password(pw)) is True

    def test_73_bytes_hash_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            hash_password("a" * 73)

    def test_73_bytes_verify_returns_false_not_raise(self) -> None:
        hashed = hash_password("a" * 72)
        assert verify_password("a" * 73, hashed) is False


def _signed(claims: dict[str, object]) -> str:
    """用测试秘钥签发任意 claims（签名合法），用于构造缺 claim/坏 claim 的令牌。"""
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _full_claims() -> dict[str, object]:
    iat = int(FakeClock().now().timestamp())
    return {
        "sub": str(USER_ID),
        "role": ROLE,
        "dept": str(DEPT_ID),
        "exp": iat + 30 * 60,
        "iat": iat,
        "jti": "f" * 32,
    }


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

    @pytest.mark.parametrize("missing", ["exp", "sub", "role", "dept", "jti"])
    def test_missing_claim_token_decodes_to_none(self, fake_clock: FakeClock, missing: str) -> None:
        claims = _full_claims()
        del claims[missing]  # 签名仍合法：签名对象是完整字节串，缺 claim 不影响签名校验
        assert decode_token(_signed(claims), SECRET) is None

    @pytest.mark.parametrize(
        ("claim", "bad_value"),
        [("sub", "abc"), ("dept", "xyz"), ("exp", "not-a-number")],
    )
    def test_malformed_claim_token_decodes_to_none(
        self, fake_clock: FakeClock, claim: str, bad_value: str
    ) -> None:
        claims = _full_claims() | {claim: bad_value}  # 签名合法但 claim 非数值
        assert decode_token(_signed(claims), SECRET) is None


class TestCreateTokenClaims:
    def test_claims_match_inputs(self) -> None:
        """断言 create_token 把入参逐字段落为 claims（sub/role/dept/iat/exp/jti）。

        根因记录（findings I28，"Signature has expired" 时效型 flake）：
        签发侧 now 取 FakeClock 固定起点 2026-10-01T08:00:00+00:00（见 FakeClock.__init__），
        exp 被固定为 2026-10-01T08:30Z；而校验侧曾只关 verify_iat，verify_exp 仍走
        PyJWT 默认的真实墙钟。真实时间越过 2026-10-01T08:30Z 之前必然通过、越过之后
        必然过期报错——两轮评审恰好跨过该时刻，才呈现"间歇失败"。属测试侧校验时钟
        与签发时钟不一致，decode_token 生产路径（经可注入时钟）无此问题。
        修复：本用例只验证 claim 内容等于签发入参，exp 正确性由下方数值断言覆盖，
        时效行为已由 TestToken.test_expired_token_decodes_to_none 经注入时钟覆盖，
        故 verify_exp 一并关闭，校验侧不再触碰真实墙钟（签名校验仍生效）。
        """
        clock = FakeClock()
        now: datetime = clock.now()
        token = create_token(
            user_id=USER_ID, role=ROLE, dept_id=DEPT_ID, expires_min=30, now=now, secret=SECRET
        )
        claims = jwt.decode(
            token,
            SECRET,
            algorithms=["HS256"],
            # iat/exp 时效校验依赖真实墙钟，与 FakeClock 固定起点必然错位，见 docstring 根因记录
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["sub"] == str(USER_ID)
        assert claims["role"] == ROLE
        assert claims["dept"] == str(DEPT_ID)
        assert claims["iat"] == int(now.timestamp())
        assert claims["exp"] == int(now.timestamp()) + 30 * 60
        assert isinstance(claims["jti"], str) and len(claims["jti"]) == 32
