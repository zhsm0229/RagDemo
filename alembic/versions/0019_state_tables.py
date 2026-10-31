"""0019: 建进程内状态外置三表（工单 19，state_backend=pg 后端）

进程内易失状态（问答精确缓存 / 每日限额+召回计数暂存 / 登录限流+登出黑名单）
外置到 PG：单实例进程内字典 → 跨实例共享表（工单 20 多副本的前置）。

三张表均为**易失状态、非业务实体**：无 is_active 软删（软删对缓存/计数无意义，
偏差记录在案——standards"删除一律软删"针对业务实体，此处不适用）；行随 TTL/
flush/reset 生命周期，备份随卷即可（runbook 补充说明）。

回滚：DROP 三表（可回滚），状态为易失数据、降级不迁移（进程回退到 memory 后端）。
仅追加不改历史迁移。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ① 问答精确缓存（PgExactCache）：键→条目 + 过期时刻，TTL 惰性清除（get 顺手删）
    op.create_table(
        "state_cache_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "cache_key",
            sa.Text(),
            nullable=False,
            comment="缓存键 chat:{dept}[:u{uid}]:{question}，与 ExactCache.make_key 同构",
        ),
        sa.Column(
            "dept_id",
            sa.BigInteger(),
            comment="自键前缀 chat:{dept}: 解析，仅运维索引用；解析失败置 NULL",
        ),
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            comment="缓存条目（answer/citations/answerType，同内存版 entry 结构）",
        ),
        sa.Column(
            "expire_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="过期时刻 = 装配时钟 now + cache_ttl_s；到期 get 返回 None 并惰性删除",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("cache_key", name="state_cache_entries_cache_key_key"),
    )
    op.create_index("idx_state_cache_expire", "state_cache_entries", ["expire_at"])

    # ② 通用计数器（PgDailyCounter / PgHitCounter / 工单 21 令牌桶预留）
    op.create_table(
        "state_counters",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "scope",
            sa.Text(),
            nullable=False,
            comment="计数域：daily=日配额 / hit=召回计数暂存 / bucket=令牌桶（工单 21 预留）",
        ),
        sa.Column(
            "ckey",
            sa.Text(),
            nullable=False,
            comment="域内键：daily=user_id 文本 / hit=chunk_id 文本 / bucket={entry}:{user_id}",
        ),
        sa.Column(
            "n",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
            comment="计数值：daily=当日已用 / hit=待刷增量 / bucket=剩余令牌（x1000 定点，21 预留）",
        ),
        sa.Column(
            "day",
            sa.Date(),
            comment="daily 专用：UTC 归属日（跨日重置判定）；hit/bucket 为 NULL",
        ),
        sa.Column(
            "refill_at",
            sa.DateTime(timezone=True),
            comment="bucket 专用：上次补充时刻（工单 21 落地零迁移）；daily/hit 为 NULL",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("scope", "ckey", name="state_counters_scope_ckey_key"),
    )

    # ③ 登录限流 + 登出黑名单（PgSlidingWindowLimiter / PgJtiBlacklist）
    op.create_table(
        "state_auth_guards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "guard_type",
            sa.Text(),
            nullable=False,
            comment="防线类型：limiter=登录滑动窗口 / jti=登出黑名单",
        ),
        sa.Column(
            "guard_key",
            sa.Text(),
            nullable=False,
            comment="防线键：limiter={ip}|{username} / jti=令牌 jti（uuid4hex）",
        ),
        sa.Column(
            "event_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="事件时刻：limiter=窗口事件发生时刻 / jti=撤销时刻",
        ),
        sa.Column(
            "expire_at",
            sa.DateTime(timezone=True),
            comment="jti 专用 = 原令牌 exp（过期视同未撤销）；limiter 为 NULL",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "guard_type",
            "guard_key",
            "event_at",
            name="state_auth_guards_guard_type_guard_key_event_at_key",
        ),
    )
    op.create_index(
        "idx_state_auth_expire",
        "state_auth_guards",
        ["expire_at"],
        postgresql_where=sa.text("expire_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_state_auth_expire", table_name="state_auth_guards")
    op.drop_table("state_auth_guards")
    op.drop_table("state_counters")
    op.drop_index("idx_state_cache_expire", table_name="state_cache_entries")
    op.drop_table("state_cache_entries")
