"""鉴权服务：登录（失败计数/锁定/审计）、首登改密、登录限流、用户查询。

锁定策略（standards §5.4）：连续失败 5 次触发持久锁（users.failed_attempts /
locked_until，进程重启不丢），锁 15 分钟，锁定窗口内正确密码同样拒绝，锁定写
审计；限流为进程内滑动窗口（IP+用户名双键），进程重启只丢窗口不丢锁定状态。
未知用户名路径同样执行一次 bcrypt 校验（AUTH-08，作用于模块级 _DUMMY_HASH）：
拉平与真实用户路径的响应耗时，防用户名枚举时序探测。
处置判定收敛为纯函数 evaluate_login 便于 TDD；时间一律经可注入 Clock。
"""

import threading
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.config import get_settings
from app.core.errors import (
    ERR_ACCOUNT_LOCKED,
    ERR_INVALID_CREDENTIALS,
    ERR_PARAM_INVALID,
    ERR_RATE_LIMITED,
    ApiError,
)
from app.core.security import create_token, hash_password, verify_password
from app.db.models import User
from app.services.audit import record_audit

MAX_LOGIN_ATTEMPTS: int = 5  # 连续失败上限，第 5 次失败即锁定
LOCK_DURATION: timedelta = timedelta(minutes=15)

ACTION_ALLOW: str = "allow"  # 无失败负担，放行
ACTION_REJECT: str = "reject"  # 计数未达上限：拒绝本次并提示剩余次数
ACTION_LOCK: str = "lock"  # 达上限：写 locked_until 持久锁

_MSG_BAD_CREDENTIALS = "用户名或密码错误"
_MSG_LOCKED = "账号已锁定，请 15 分钟后重试或联系管理员解锁"

# AUTH-08 时序拉平基准：未知用户名路径也跑一次同成本 bcrypt 校验（bcrypt 12 轮），
# 使"用户不存在"与"密码错误"的响应耗时同量级，无法从耗时差枚举用户名。
_DUMMY_HASH: str = hash_password("dummy-password-for-timing")


def evaluate_login(attempts: int, max_attempts: int = MAX_LOGIN_ATTEMPTS) -> str:
    """纯函数：按最新一次失败后的累计连续失败次数判定处置动作。

    attempts 为已计入本次失败的计数：<=0 → allow（无失败负担，成功路径计数
    清零后的恒等语义）；1..<max → reject（拒绝并提示剩余次数 max-1-attempts，
    即再错几次将触发锁定）；>=max → lock（写 locked_until 持久锁并拒绝）。
    """
    if attempts <= 0:
        return ACTION_ALLOW
    if attempts >= max_attempts:
        return ACTION_LOCK
    return ACTION_REJECT


class SlidingWindowLimiter:
    """进程内滑动窗口限流器：同一键在 window_seconds 内最多 max_events 次。

    线程安全（FastAPI 线程池并发登录）；时间经可注入 Clock，测试可冻结拨动；
    进程重启窗口清空（锁定状态在库里，不受影响，§5.4）。
    """

    def __init__(self, window_seconds: int, max_events: int, clock: Clock | None = None) -> None:
        self._window_seconds = window_seconds
        self._max_events = max_events
        self._clock = clock
        self._events: dict[str, list[datetime]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """记录一次事件并判定：窗口内未达上限返回 True（放行），达上限 False。"""
        now = (self._clock if self._clock is not None else get_clock()).now()
        floor = now - timedelta(seconds=self._window_seconds)
        with self._lock:
            recent = [t for t in self._events.get(key, []) if t > floor]
            self._events[key] = recent
            if len(recent) >= self._max_events:
                return False
            recent.append(now)
            return True

    def reset(self) -> None:
        """清空全部窗口（测试隔离用；生产进程重启等效）。"""
        with self._lock:
            self._events.clear()


default_limiter = SlidingWindowLimiter(window_seconds=60, max_events=10)


def get_user_by_id(session: Session, user_id: int) -> User | None:
    """供依赖注入查库使用：api 层不直查表（standards §1.6）。"""
    return session.get(User, user_id)


def login(
    session: Session,
    username: str,
    password: str,
    ip: str | None,
    limiter: SlidingWindowLimiter | None = None,
    max_attempts: int = MAX_LOGIN_ATTEMPTS,
    clock: Clock | None = None,
) -> tuple[User, str]:
    """登录：限流 → 72 字节上限 → 查用户 → 锁定门 → 验密 → 签发 JWT。

    成功清零计数并签发 access_token_expire_minutes 有效期令牌（本函数 commit）；
    失败路径先落计数/锁定与审计再抛 ApiError，保证状态与留痕一致。未知用户名
    与密码错误同码同文案且同验密耗时（AUTH-08：前者作用于 _DUMMY_HASH）；
    计数只对真实存在的账号累计。
    """
    effective_clock = clock if clock is not None else get_clock()
    now = effective_clock.now()
    window = limiter if limiter is not None else default_limiter
    if not window.hit(f"{ip}|{username}"):
        raise ApiError(ERR_RATE_LIMITED, "登录尝试过于频繁，请稍后再试", 429)
    if len(password.encode("utf-8")) > 72:  # bcrypt 算法上限，API 边界校验（T3 审查）
        raise ApiError(ERR_PARAM_INVALID, "密码长度不能超过 72 字节", 422)
    user = session.scalars(select(User).where(User.username == username)).first()
    if user is None:
        verify_password(password, _DUMMY_HASH)  # AUTH-08：结果必为假，只为拉平验密耗时
        raise ApiError(ERR_INVALID_CREDENTIALS, _MSG_BAD_CREDENTIALS, 401)
    if user.locked_until is not None and user.locked_until > now:
        raise ApiError(ERR_ACCOUNT_LOCKED, _MSG_LOCKED, 401, data=_locked_payload(user))
    if not verify_password(password, user.password_hash):
        _register_failure(session, user, ip, max_attempts, now, effective_clock)
    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    user.updated_at = now
    settings = get_settings()
    token = create_token(
        user.id,
        user.role,
        user.dept_id,
        settings.access_token_expire_minutes,
        now,
        settings.jwt_secret,
    )
    session.commit()
    return user, token


def change_password(
    session: Session, user: User, old: str, new: str, clock: Clock | None = None
) -> User:
    """改密（含首登强制）：新密码 ≥8 位且 ≤72 字节 → 验旧密码 → 落库清 must_change。

    验旧密码即身份证明：成功后同时清零失败计数并解除锁定，写审计并 commit。
    """
    effective_clock = clock if clock is not None else get_clock()
    if len(new) < 8:
        raise ApiError(ERR_PARAM_INVALID, "新密码长度不能少于 8 位", 422)
    if len(new.encode("utf-8")) > 72:
        raise ApiError(ERR_PARAM_INVALID, "新密码长度不能超过 72 字节", 422)
    if not verify_password(old, user.password_hash):
        raise ApiError(ERR_INVALID_CREDENTIALS, "原密码不正确")
    now = effective_clock.now()
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.failed_attempts = 0
    user.locked_until = None
    user.updated_at = now
    record_audit(
        session,
        actor_user_id=user.id,
        action="user.change_password",
        object_type="user",
        object_id=user.id,
        detail={"after": {"mustChangePassword": False}},
        ip=None,
        clock=effective_clock,
    )
    session.commit()
    return user


def _locked_payload(user: User) -> dict:
    """Rider T7（评审移交）：锁定 401 的 data 载荷——lockedUntil 为 ISO 时刻，
    前端据此渲染锁定倒计时；两个 2002 抛出点（触发锁定/窗口内重试）共用。"""
    return {"lockedUntil": user.locked_until.isoformat()}


def _register_failure(
    session: Session,
    user: User,
    ip: str | None,
    max_attempts: int,
    now: datetime,
    clock: Clock,
) -> None:
    """密码校验失败：自增计数，达上限置锁并写审计；两条路径均以 ApiError 结束。"""
    user.failed_attempts += 1
    user.updated_at = now
    if evaluate_login(user.failed_attempts, max_attempts) == ACTION_LOCK:
        user.locked_until = now + LOCK_DURATION
        record_audit(
            session,
            actor_user_id=user.id,
            action="user.lock",
            object_type="user",
            object_id=user.id,
            detail={
                "after": {
                    "failedAttempts": user.failed_attempts,
                    "lockedUntil": user.locked_until.isoformat(),
                }
            },
            ip=ip,
            clock=clock,
        )
        session.commit()
        raise ApiError(
            ERR_ACCOUNT_LOCKED,
            f"密码连续错误 {max_attempts} 次，账号已锁定 15 分钟",
            401,
            data=_locked_payload(user),
        )
    session.commit()
    remaining = max_attempts - 1 - user.failed_attempts
    raise ApiError(
        ERR_INVALID_CREDENTIALS, f"{_MSG_BAD_CREDENTIALS}，剩余尝试次数 {remaining}", 401
    )
