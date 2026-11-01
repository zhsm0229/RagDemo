r"""进程内状态外置 PG 实现（工单 19）：精确缓存 / 每日限额 / 召回计数暂存搬运。

- 拆分边界：本模块承载 chat_cache 三协议（ChatCacheProto/DailyCounterProto/
  HitCounterProto）的 PG 实现；限流与黑名单（RateLimiterProto/JtiBlacklistProto）
  在 state_pg_auth.py——两文件拆分防单文件触 300 行红线（standards §1.4-4）。
- 构造统一注入 clock + session_factory（缺省 SessionLocal）：所有时刻/日期用注入
  时钟算好再绑定（不用 SQL now()），FakeClock 等价性测试才可精确对齐；自建短会话
  （不借请求会话，调度任务体先例 scheduler.py）自行提交。
- 并发语义：READ COMMITTED 下依赖 PK/唯一键行锁——每日限额单语句 ON CONFLICT
  原子（拒绝零写入）；hit 暂存 upsert 行锁串行 + flush FOR UPDATE 快照搬运（不丢
  不滚入论证见 PgHitCounter.flush）；SQL 全参数绑定（表达式方言 / text+bindparams）。
"""

from datetime import timedelta

from sqlalchemy import Integer, bindparam, case, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import Clock
from app.db.base import SessionLocal
from app.db.models import Chunk
from app.db.models.state import StateCacheEntry, StateCounter


def _parse_dept(key: str) -> int | None:
    """自键前缀 ``chat:{dept}:`` 解析部门 id（仅运维索引用）；解析失败置 None。"""
    if not key.startswith("chat:"):
        return None
    dept_text = key[len("chat:") :].split(":", 1)[0]
    if not dept_text.isdigit():
        return None
    return int(dept_text)


class PgExactCache:
    """ExactCache 的 PG 外置实现（协议面 ChatCacheProto）：键→条目 + 过期时刻。

    命中要求 expire_at > now；到期 get 返回 None 并惰性删除（无后台回收，与内存版
    同口径）。JSONB 反序列化每次 fetch 产出全新对象图——深拷贝出借语义天然成立
    （调用方原地改写不污染缓存条目）。
    """

    def __init__(
        self, clock: Clock, ttl_s: int, session_factory: sessionmaker = SessionLocal
    ) -> None:
        self._clock = clock
        self._ttl_s = ttl_s
        self._session_factory = session_factory

    def make_key(
        self, dept_id: int, question_normalized: str, user_suffix: str | None = None
    ) -> str:
        """键生成与后端无关：形态必须与 ExactCache.make_key 逐字节一致（键收敛一处
        的约束同样适用于本副本——两侧漂移即缓存跨后端互不命中，等价性测试钉死）。"""
        if user_suffix:
            return f"chat:{dept_id}:{user_suffix}:{question_normalized}"
        return f"chat:{dept_id}:{question_normalized}"

    def get(self, key: str) -> dict | None:
        now = self._clock.now()
        with self._session_factory() as db:
            row = db.execute(
                select(StateCacheEntry.payload, StateCacheEntry.expire_at).where(
                    StateCacheEntry.cache_key == key
                )
            ).first()
            if row is None:
                return None
            payload, expire_at = row
            if now >= expire_at:  # 到期即失效（含恰等边界，对齐 chat_cache.py:67）
                db.execute(
                    delete(StateCacheEntry).where(
                        StateCacheEntry.cache_key == key,
                        StateCacheEntry.expire_at <= now,
                    )
                )
                db.commit()
                return None
            return payload  # JSONB 反序列化新对象：深拷贝出借语义（模块 docstring）

    def put(self, key: str, entry: dict) -> None:
        now = self._clock.now()
        insert_stmt = pg_insert(StateCacheEntry).values(
            cache_key=key,
            dept_id=_parse_dept(key),
            payload=entry,
            expire_at=now + timedelta(seconds=self._ttl_s),
            updated_at=now,
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=[StateCacheEntry.cache_key],
            set_={
                "payload": insert_stmt.excluded.payload,
                "expire_at": insert_stmt.excluded.expire_at,
                "updated_at": insert_stmt.excluded.updated_at,
            },
        )
        with self._session_factory() as db:
            db.execute(stmt)
            db.commit()

    def clear_by_ref(self, ref_type: str, ref_id: int) -> int:
        """按引用精准失效：citations 缺席/空/非数组保守删除（fail-safe），任一元素
        (refType, refId) 文本命中即删。refId 用文本比较不 cast int：畸形数据不抛错、
        走保守删除，int/str 归一语义与内存版 _touches（R2 修订）一致。返回删除条数。"""
        stmt = text(
            "DELETE FROM state_cache_entries WHERE "
            "payload->'citations' IS NULL "
            "OR jsonb_typeof(payload->'citations') <> 'array' "
            "OR jsonb_array_length(payload->'citations') = 0 "
            "OR EXISTS (SELECT 1 FROM jsonb_array_elements(payload->'citations') c "
            "WHERE c->>'refType' = :ref_type AND c->>'refId' = :ref_id_text)"
        ).bindparams(ref_type=ref_type, ref_id_text=str(ref_id))
        with self._session_factory() as db:
            removed = db.execute(stmt).rowcount
            db.commit()
            return int(removed or 0)

    def clear(self) -> None:
        with self._session_factory() as db:
            db.execute(delete(StateCacheEntry))
            db.commit()


class PgDailyCounter:
    """DailyCounter 的 PG 外置实现（协议面 DailyCounterProto）。

    try_consume 单语句 ON CONFLICT ... WHERE ... RETURNING（设计 §5.2 裁决 4）：
    唯一键 (scope, ckey) 行锁下天然原子（READ COMMITTED 足够），跨日重置 / 达限
    拒写 / 恰限放行三语义收进一句；WHERE 不满足时不返回行、零写入（拒绝不计数）。
    """

    def __init__(self, clock: Clock, session_factory: sessionmaker = SessionLocal) -> None:
        self._clock = clock
        self._session_factory = session_factory

    def try_consume(self, user_id: int, limit: int) -> bool:
        now = self._clock.now()
        insert_stmt = pg_insert(StateCounter).values(
            scope="daily", ckey=str(user_id), n=1, day=now.date(), updated_at=now
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=[StateCounter.scope, StateCounter.ckey],
            set_={
                # 跨日（存量 day 与当日 DISTINCT）→ 从 1 重计；同日 → 累加
                "n": case(
                    (StateCounter.day.is_distinct_from(insert_stmt.excluded.day), 1),
                    else_=StateCounter.n + 1,
                ),
                "day": insert_stmt.excluded.day,
                "updated_at": insert_stmt.excluded.updated_at,
            },
            # 跨日或未达限才更新；达限（n >= limit）不满足 WHERE → 零写入零返回
            where=or_(
                StateCounter.day.is_distinct_from(insert_stmt.excluded.day),
                StateCounter.n < limit,
            ),
        ).returning(StateCounter.n)
        with self._session_factory() as db:
            row = db.execute(stmt).first()
            db.commit()
            return row is not None

    def release(self, user_id: int) -> None:
        """回补 1 次消费（工单 24）：首 token 前中断的轮次对用户零成本，重试不双扣。
        仅当日行可回补（WHERE day == 当日）——跨日行今日计数已重置，回补 no-op；
        无行零写入（不造负账）；GREATEST(n-1, 0) 防重复回补越下界。与 try_consume
        同键 (scope='daily', ckey=str(user_id))，READ COMMITTED 行锁原子。"""
        now = self._clock.now()
        stmt = (
            update(StateCounter)
            .where(
                StateCounter.scope == "daily",
                StateCounter.ckey == str(user_id),
                StateCounter.day == now.date(),
            )
            .values(n=func.greatest(StateCounter.n - 1, 0), updated_at=now)
            .returning(StateCounter.n)
        )
        with self._session_factory() as db:
            db.execute(stmt)
            db.commit()


class PgHitCounter:
    """HitCounter 的 PG 外置实现（协议面 HitCounterProto）：暂存-搬运两段式。

    add 把召回增量 upsert 进 state_counters（hit 域，应用内先合并同 id），flush 走
    传入会话 FOR UPDATE 快照 → 一条 UPDATE 搬运进 chunks → 删除已消费暂存行，同
    事务提交。直写 chunks 会把每轮 N 条增量变成 N 次热行写（设计 §8 裁决 5），暂存
    表把写压力摊散且 flush 两语句保崩溃安全。
    """

    def __init__(self, clock: Clock, session_factory: sessionmaker = SessionLocal) -> None:
        self._clock = clock
        self._session_factory = session_factory

    def add(self, chunk_ids: list[int]) -> None:
        """累加本轮召回命中的 chunk（同 id 多次出现按出现次数累加，对齐内存版）。"""
        counts: dict[int, int] = {}
        for chunk_id in chunk_ids:
            counts[chunk_id] = counts.get(chunk_id, 0) + 1
        now = self._clock.now()
        rows = [
            {"ckey": str(chunk_id), "n": n, "updated_at": now}
            for chunk_id, n in sorted(counts.items())
        ]
        insert_stmt = pg_insert(StateCounter)
        stmt = insert_stmt.values(
            scope="hit",
            ckey=bindparam("ckey"),
            n=bindparam("n"),
            updated_at=bindparam("updated_at"),
        ).on_conflict_do_update(
            index_elements=[StateCounter.scope, StateCounter.ckey],
            set_={
                "n": StateCounter.n + insert_stmt.excluded.n,
                "updated_at": insert_stmt.excluded.updated_at,
            },
        )
        # 内部短会话自行提交：add 时机在 finalize 落库之后，与请求事务无纠缠
        with self._session_factory() as db:
            db.execute(stmt, rows)
            db.commit()

    def flush(self, db: Session) -> int:
        """快照-搬运-回收（设计 §5.3）：返回本次覆盖的去重 chunk 数。

        不丢/滚入论证：FOR UPDATE 行锁使并发 add 的 upsert 阻塞到本事务提交，提交
        后其 ON CONFLICT 目标行已被删除 → 按新行插入、增量保留；UPDATE 与 DELETE
        同事务，任一失败整体回滚、快照未消费 → 滚入下一轮（调度任务体吞异常重试，
        scheduler.run_hit_flush）。消失 chunk 的暂存行：UPDATE 不命中、DELETE 照删
        ——与内存版 executemany 静默跳过缺失 id 后照常回收一致。偏差：空快照发一条
        SELECT（内存版零 SQL），契约断言按实现分组（设计 §7.1）。
        """
        now = self._clock.now()
        snapshot = db.execute(
            select(StateCounter.ckey, StateCounter.n)
            .where(StateCounter.scope == "hit")
            .order_by(StateCounter.ckey)
            .with_for_update()
        ).all()
        if not snapshot:
            # 零写入，但必须释放 ① 的 FOR UPDATE 锁：用 commit 而非 rollback——
            # commit 对"借入的外部事务"（测试 fixture 形态）是 savepoint release，
            # rollback 则会把外部事务一并终止（实测 SAWarning 脱管）
            db.commit()
            return 0
        rows = [{"chunk_id": int(ckey), "n": n, "now": now} for ckey, n in snapshot]
        stmt = (
            update(Chunk)
            .where(Chunk.id == bindparam("chunk_id"))
            .values(
                hit_count=Chunk.hit_count + bindparam("n", type_=Integer),
                last_hit_at=bindparam("now"),
            )
        )
        # Core 通道（会话当前连接/事务）：与内存版同一语句形态，executemany 参数绑定
        db.connection().execute(stmt, rows)
        db.execute(
            delete(StateCounter).where(
                StateCounter.scope == "hit",
                StateCounter.ckey.in_([ckey for ckey, _ in snapshot]),
            )
        )
        db.commit()
        return len(rows)
