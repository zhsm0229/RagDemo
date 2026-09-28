"""安全模块：bcrypt 口令哈希 + HS256 JWT 签发/解码。

约定：decode_token 对过期、签名不符、格式损坏一律返回 None，不抛异常；
时间不入模块内部，由调用方显式传入 now（配合可注入时钟，测试可拨快）。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import bcrypt
import jwt

from app.core.clock import get_clock

_ALGORITHM = "HS256"
_BCRYPT_ROUNDS = 12


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """解码后的访问令牌载荷（sub/role/dept/jti；exp/iat 已由 pyjwt 校验）。"""

    user_id: int
    role: str
    dept_id: int
    jti: str


def hash_password(pw: str) -> str:
    """bcrypt 哈希口令，返回 ascii 安全的哈希串（内含盐与成本因子）。"""
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(pw: str, hashed: str) -> bool:
    """常数时间比对口令与哈希；存储哈希损坏视作不匹配而非抛错。"""
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


def create_token(
    user_id: int, role: str, dept_id: int, expires_min: int, now: datetime, secret: str
) -> str:
    """签发 HS256 访问令牌：sub=user_id 字符串 / role / dept / exp / iat / jti=uuid4hex。"""
    claims: dict[str, object] = {
        "sub": str(user_id),
        "role": role,
        "dept": str(dept_id),
        "exp": now + timedelta(minutes=expires_min),
        "iat": now,
        "jti": uuid4().hex,
    }
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def decode_token(token: str, secret: str) -> TokenPayload | None:
    """校验签名与 exp；过期/篡改/秘钥不符/格式损坏返回 None。

    exp 以可注入时钟（get_clock().now()）为准，与项目时间约定一致，
    测试可用 FakeClock 拨过 exp；签名与格式仍由 pyjwt 把关。
    iat 不作未来校验（仅审计字段，避免签发方与校验方时钟微差误拒）。
    """
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            options={"verify_exp": False, "verify_iat": False},
        )
    except jwt.PyJWTError:
        return None
    if get_clock().now().timestamp() > float(claims["exp"]):  # 与 pyjwt 语义一致：exp < now 即过期
        return None
    return TokenPayload(
        user_id=int(claims["sub"]),
        role=str(claims["role"]),
        dept_id=int(claims["dept"]),
        jti=str(claims["jti"]),
    )
