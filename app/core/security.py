"""安全模块：bcrypt 口令哈希 + HS256 JWT 签发/解码 + jti 登出黑名单。

约定：decode_token 对过期、签名不符、格式损坏、缺 claim/坏 claim、已登出（jti
命中黑名单）一律返回 None，绝不向调用方抛异常；签发时间由调用方显式传入 now
（create_token），校验侧经可注入时钟 get_clock() 取当前时间（生产为 SystemClock，
测试可换 FakeClock 拨快）。
"""

import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

import bcrypt
import jwt

from app.core.clock import Clock, get_clock

_ALGORITHM = "HS256"
_BCRYPT_ROUNDS = 12


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """解码后的访问令牌载荷（sub/role/dept/jti；exp 已按可注入时钟校验，iat 仅审计不校验）。

    exp 为令牌过期时刻的 Unix 秒级时间戳，登出黑名单以它计算条目存活期（§5.3）。
    """

    user_id: int
    role: str
    dept_id: int
    jti: str
    exp: float


class JtiBlacklist:
    """进程内 jti 登出黑名单（§5.3）：条目 TTL=令牌剩余有效期，进程重启清空。

    单机 MVP 形态，A 阶段整体换 Redis（§25.3-A）——封装成小服务而非散落全局
    变量，与 services.auth 的 default_limiter 同款装配约定：生产用模块级
    default_blacklist，测试/多实例注入自建实例。线程安全（FastAPI 线程池并发）。
    容量治理：条目随原令牌 exp 自然失效，revoke 时顺手清理过期条目，集合规模
    始终 ≤ 存续令牌数，无需后台清扫。
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock
        self._entries: dict[str, float] = {}  # jti → 原令牌 exp（Unix 秒）
        self._lock = threading.Lock()

    def revoke(self, jti: str, exp: float) -> None:
        """把 jti 记入黑名单至原令牌 exp；已过期的令牌无记入意义，忽略。"""
        now = (self._clock if self._clock is not None else get_clock()).now().timestamp()
        with self._lock:
            self._entries = {j: e for j, e in self._entries.items() if e > now}  # 过期治理
            if exp > now:
                self._entries[jti] = exp

    def revoked(self, jti: str) -> bool:
        """jti 是否在有效黑名单内；条目已过原令牌 exp 视同不存在。"""
        now = (self._clock if self._clock is not None else get_clock()).now().timestamp()
        with self._lock:
            exp = self._entries.get(jti)
        return exp is not None and exp > now

    def reset(self) -> None:
        """清空黑名单（测试隔离用；生产进程重启等效，§5.3）。"""
        with self._lock:
            self._entries.clear()


default_blacklist = JtiBlacklist()  # 默认装配（与 services.auth.default_limiter 同款约定）


def hash_password(pw: str) -> str:
    """bcrypt 哈希口令，返回 ascii 安全的哈希串（内含盐与成本因子）。

    密码不得超过 72 字节（bcrypt 算法上限，超长抛 ValueError），
    该约束由 API 层校验，本模块不截断。
    """
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(pw: str, hashed: str) -> bool:
    """常数时间比对口令与哈希；存储哈希损坏或口令超 72 字节均视作不匹配返回 False。"""
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


def decode_token(
    token: str, secret: str, blacklist: JtiBlacklist | None = None
) -> TokenPayload | None:
    """校验签名与 exp 与登出黑名单；无效/过期/已登出（jti 命中）返回 None。

    exp 以可注入时钟（get_clock().now()）为准，与项目时间约定一致，
    测试可用 FakeClock 拨过 exp；签名与格式仍由 pyjwt 把关。
    iat 不作未来校验（仅审计字段，避免签发方与校验方时钟微差误拒）。
    黑名单缺省用 default_blacklist（登出即时失效对所有调用点默认生效），
    测试可注入自建实例（§5.3：进程内集合，A 阶段整体换 Redis）。
    """
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            options={"verify_exp": False, "verify_iat": False},
        )
        exp = float(claims["exp"])
        if not math.isfinite(exp) or get_clock().now().timestamp() > exp:  # exp < now 即过期
            return None
        payload = TokenPayload(
            user_id=int(claims["sub"]),
            role=str(claims["role"]),
            dept_id=int(claims["dept"]),
            jti=str(claims["jti"]),
            exp=exp,
        )
        effective = blacklist if blacklist is not None else default_blacklist
        if effective.revoked(payload.jti):  # §5.3：已登出令牌与无效令牌同口径
            return None
        return payload
    except jwt.PyJWTError:
        return None
    except (KeyError, ValueError, TypeError):
        return None  # 缺 claim 或 claim 非数值：一律视作无效令牌
