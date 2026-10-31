r"""进程内状态外置 PG 实现（工单 19）：登录限流滑动窗口 / jti 登出黑名单。

与 state_pg.py 拆分防单文件触 300 行红线。构造统一注入 clock + session_factory
（缺省 SessionLocal）：时刻用注入时钟算好再绑定；自建短会话自行提交。

- 限流 hit：per-key 事务级咨询锁把同键 hit 串行化（READ COMMITTED 下 count→insert
  的 check-then-act 会超放，设计 §5.4 裁决否决 SERIALIZABLE）；窗外事件全局顺手
  清理（容量有界）。**同秒事件消歧**：UNIQUE (guard_type, guard_key, event_at) 与
  SystemClock 秒级精度冲突——同键同秒第二次 hit 会撞唯一键（内存版允许同刻事件
  并存），咨询锁内取本键 max(event_at) 顺延 1µs 消歧（严格递增、永不回退，窗口
  判定偏移 ≤1µs，边界口径见等价性测试注释）。
- 黑名单 revoke：过期行顺手清理 + 条件插入；同 (jti, 撤销时刻) 的重复撤销是行完全
  相同的幂等写，ON CONFLICT DO NOTHING 跳过即与内存版字典覆盖语义一致（uuid4 不
  复用，不同令牌 guard_key 必异）。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from app.core.clock import Clock
from app.db.base import SessionLocal
from app.db.models.state import StateAuthGuard


class PgSlidingWindowLimiter:
    """SlidingWindowLimiter 的 PG 外置实现（协议面 RateLimiterProto）。

    同键窗口内最多 max_events 次：窗口内恰限放行、达限拒绝且不落事件（对齐
    services/auth.py:88-94）、超窗重置；reset 清空全部窗口（测试隔离/进程重启等效）。
    """

    def __init__(
        self,
        window_seconds: int,
        max_events: int,
        clock: Clock,
        session_factory: sessionmaker = SessionLocal,
    ) -> None:
        self._window_seconds = window_seconds
        self._max_events = max_events
        self._clock = clock
        self._session_factory = session_factory

    def hit(self, key: str) -> bool:
        """记录一次事件并判定：窗口内未达上限返回 True（放行），达上限 False。"""
        now = self._clock.now()
        floor = now - timedelta(seconds=self._window_seconds)
        with self._session_factory() as db:
            # per-key 事务级咨询锁：同键串行化，count→insert 的并发窗口不再超放
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('limiter:' || :key))").bindparams(
                    key=key
                )
            )
            # ① 窗外清理（全局顺手扫，容量有界）
            db.execute(
                delete(StateAuthGuard).where(
                    StateAuthGuard.guard_type == "limiter",
                    StateAuthGuard.event_at <= floor,
                )
            )
            # ② 窗口计数 + 本键最近事件时刻（同秒消歧用，模块 docstring）
            recent, last_at = db.execute(
                select(func.count(), func.max(StateAuthGuard.event_at)).where(
                    StateAuthGuard.guard_type == "limiter",
                    StateAuthGuard.guard_key == key,
                    StateAuthGuard.event_at > floor,
                )
            ).one()
            if recent >= self._max_events:
                db.rollback()  # 拒绝不落事件：回滚释放咨询锁（事务内无变更）
                return False
            event_at = now
            if last_at is not None and last_at >= now:
                event_at = last_at + timedelta(microseconds=1)  # 同秒第 N 次事件顺延消歧
            # ③ 仅放行时落事件
            db.execute(
                insert(StateAuthGuard).values(
                    guard_type="limiter", guard_key=key, event_at=event_at
                )
            )
            db.commit()
            return True

    def reset(self) -> None:
        """清空全部窗口（测试隔离用；生产进程重启等效语义，pg 后端为显式清表）。"""
        with self._session_factory() as db:
            db.execute(delete(StateAuthGuard).where(StateAuthGuard.guard_type == "limiter"))
            db.commit()


class PgJtiBlacklist:
    """JtiBlacklist 的 PG 外置实现（协议面 JtiBlacklistProto）。

    条目 TTL=令牌剩余有效期（expire_at=原令牌 exp）：revoked 取"存在未过期行"，
    revoke 时顺手清理过期行（集合规模始终 ≤ 存续令牌数，无需后台清扫，对齐
    core/security.py:57）。
    """

    def __init__(self, clock: Clock, session_factory: sessionmaker = SessionLocal) -> None:
        self._clock = clock
        self._session_factory = session_factory

    def revoke(self, jti: str, exp: float) -> None:
        """把 jti 记入黑名单至原令牌 exp；已过期的令牌无记入意义，忽略。"""
        now = self._clock.now()
        with self._session_factory() as db:
            db.execute(
                delete(StateAuthGuard).where(
                    StateAuthGuard.guard_type == "jti",
                    StateAuthGuard.expire_at <= now,  # 顺手过期治理
                )
            )
            if exp > now.timestamp():
                insert_stmt = pg_insert(StateAuthGuard).values(
                    guard_type="jti",
                    guard_key=jti,
                    event_at=now,
                    expire_at=datetime.fromtimestamp(exp, tz=UTC),
                )
                # 同 (jti, 撤销时刻) 重复撤销行完全相同：跳过即幂等（内存版覆盖语义）
                db.execute(
                    insert_stmt.on_conflict_do_nothing(
                        index_elements=[
                            StateAuthGuard.guard_type,
                            StateAuthGuard.guard_key,
                            StateAuthGuard.event_at,
                        ]
                    )
                )
            db.commit()

    def revoked(self, jti: str) -> bool:
        """jti 是否在有效黑名单内；条目已过原令牌 exp 视同不存在。"""
        now = self._clock.now()
        with self._session_factory() as db:
            found = db.execute(
                select(
                    exists().where(
                        StateAuthGuard.guard_type == "jti",
                        StateAuthGuard.guard_key == jti,
                        StateAuthGuard.expire_at > now,
                    )
                )
            ).scalar_one()
        return bool(found)

    def reset(self) -> None:
        """清空黑名单（测试隔离用；生产进程重启等效语义，pg 后端为显式清表）。"""
        with self._session_factory() as db:
            db.execute(delete(StateAuthGuard).where(StateAuthGuard.guard_type == "jti"))
            db.commit()
