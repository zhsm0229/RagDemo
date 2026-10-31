"""进程状态域模型：state_cache_entries / state_counters / state_auth_guards（0019，工单 19）。

进程内易失状态外置到 PG 的三张表（state_backend=pg 后端），由 app/services/state_pg*.py
经 SQLAlchemy 表达式方言读写，不挂业务 ORM 查询路径。均为**易失状态、非业务实体**：
无 is_active 软删（软删对缓存/计数无意义）、行随 TTL/flush/reset 生命周期。列 comment
与迁移 0019 逐字一致（autogenerate 零差异守卫依赖，同 doc_type 先例）。
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.types import _ts


class StateCacheEntry(Base):
    """state_cache_entries：问答精确缓存条目（PgExactCache）。

    键→条目 + 过期时刻；命中要求 expire_at > now，到期由 get 惰性删除
    （无后台回收，与内存版同口径）。
    """

    __tablename__ = "state_cache_entries"
    __table_args__ = (
        UniqueConstraint("cache_key", name="state_cache_entries_cache_key_key"),
        Index("idx_state_cache_expire", "expire_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(
        Text, comment="缓存键 chat:{dept}[:u{uid}]:{question}，与 ExactCache.make_key 同构"
    )
    dept_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="自键前缀 chat:{dept}: 解析，仅运维索引用；解析失败置 NULL"
    )
    payload: Mapped[Any] = mapped_column(
        JSONB, comment="缓存条目（answer/citations/answerType，同内存版 entry 结构）"
    )
    expire_at: Mapped[datetime] = mapped_column(
        _ts(),
        comment="过期时刻 = 装配时钟 now + cache_ttl_s；到期 get 返回 None 并惰性删除",
    )
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class StateCounter(Base):
    """state_counters：通用计数器（PgDailyCounter / PgHitCounter / 工单 21 令牌桶预留）。

    (scope, ckey) 唯一；daily 域跨日重置靠 day 列比较，hit 域由 flush 搬运进 chunks
    后删除暂存行。
    """

    __tablename__ = "state_counters"
    __table_args__ = (UniqueConstraint("scope", "ckey", name="state_counters_scope_ckey_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(
        Text,
        comment="计数域：daily=日配额 / hit=召回计数暂存 / bucket=令牌桶（工单 21 预留）",
    )
    ckey: Mapped[str] = mapped_column(
        Text, comment="域内键：daily=user_id 文本 / hit=chunk_id 文本 / bucket={entry}:{user_id}"
    )
    n: Mapped[int] = mapped_column(
        BigInteger,
        server_default=text("0"),
        comment="计数值：daily=当日已用 / hit=待刷增量 / bucket=剩余令牌（x1000 定点，21 预留）",
    )
    day: Mapped[date | None] = mapped_column(
        Date, comment="daily 专用：UTC 归属日（跨日重置判定）；hit/bucket 为 NULL"
    )
    refill_at: Mapped[datetime | None] = mapped_column(
        _ts(), comment="bucket 专用：上次补充时刻（工单 21 落地零迁移）；daily/hit 为 NULL"
    )
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))


class StateAuthGuard(Base):
    """state_auth_guards：登录限流事件 + jti 登出黑名单（PgSlidingWindowLimiter / PgJtiBlacklist）。

    limiter 行按 event_at 滑窗计数（窗口外顺手清理，容量有界）；jti 行按 expire_at
    判存活，revoke 时顺手清理过期行。
    """

    __tablename__ = "state_auth_guards"
    __table_args__ = (
        UniqueConstraint(
            "guard_type",
            "guard_key",
            "event_at",
            name="state_auth_guards_guard_type_guard_key_event_at_key",
        ),
        Index("idx_state_auth_expire", "expire_at", postgresql_where=text("expire_at IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    guard_type: Mapped[str] = mapped_column(
        Text, comment="防线类型：limiter=登录滑动窗口 / jti=登出黑名单"
    )
    guard_key: Mapped[str] = mapped_column(
        Text, comment="防线键：limiter={ip}|{username} / jti=令牌 jti（uuid4hex）"
    )
    event_at: Mapped[datetime] = mapped_column(
        _ts(), comment="事件时刻：limiter=窗口事件发生时刻 / jti=撤销时刻"
    )
    expire_at: Mapped[datetime | None] = mapped_column(
        _ts(), comment="jti 专用 = 原令牌 exp（过期视同未撤销）；limiter 为 NULL"
    )
    created_at: Mapped[datetime] = mapped_column(_ts(), server_default=text("now()"))
