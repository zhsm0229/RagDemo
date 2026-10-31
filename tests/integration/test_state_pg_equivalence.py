r"""进程内状态外置等价性测试（工单 19 设计 §7.1 + 修订 R2）：memory / pg 两后端
参数化，同用例同断言——外置前后行为逐字等价（键形态 / 读回 / TTL / 失效 / 限额 /
计数 / 限流 / 黑名单）。

隔离口径：PG 实现自带短会话 commit（设计 §2 裁决 4，不借请求会话），事务回滚罩
不住——autouse fixture 每 Case 先 TRUNCATE 三张 state_* 表（rag_test 真库），不用
session 回滚隔离；flush 用例的 chunk 种子走 session fixture 事务（flush(db) 收传入
会话，与种子同处一事务、读回一致，测试结束回滚零残留）。

R2（评审修订）：clear_by_ref 的 refId 统一按文本归一——JSONB 侧 c->>'refId' 文本
比较，内存版 _touches 同步 str() 归一（int 7 与 "7" 均命中），两侧一致由本文件
参数化用例钉死。
"""

import hashlib
from datetime import date

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import FakeClock
from app.core.security import JtiBlacklist
from app.db.models import Chunk, Document, User
from app.db.models.state import StateAuthGuard, StateCacheEntry, StateCounter
from app.services.auth import SlidingWindowLimiter
from app.services.chat_cache import DailyCounter, ExactCache, HitCounter

CLOCK_START = "2026-10-15T09:30:00+00:00"
_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(params=["memory", "pg"])
def backend(request: pytest.FixtureRequest) -> str:
    """参数化两后端：同用例分别以进程内实现与 PG 外置实现各跑一遍。"""
    return request.param


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(CLOCK_START)


@pytest.fixture(autouse=True)
def _truncate_state(pg):
    """每 Case 先清三张 state 表：PG 实现自带 commit，回滚式隔离不适用（设计 §7.1）。"""
    with pg.connect() as conn:
        conn.execute(text("TRUNCATE state_cache_entries, state_counters, state_auth_guards"))
        conn.commit()


@pytest.fixture()
def make_component(backend: str, clock: FakeClock, pg):
    """按 backend 产出五组件构造器：kind ∈ cache/counter/hit/limiter/jti。

    pg 实现统一注入测试库会话工厂与 FakeClock（等价性断言可精确对齐注入时钟）；
    memory 实现同注入 FakeClock（与保护面单元测试同口径）。
    """

    def _make(kind: str, *, ttl_s: int = 300):
        if backend == "pg":
            from app.services import state_pg, state_pg_auth

            factory = sessionmaker(bind=pg, expire_on_commit=False)
            if kind == "cache":
                return state_pg.PgExactCache(clock, ttl_s, session_factory=factory)
            if kind == "counter":
                return state_pg.PgDailyCounter(clock, session_factory=factory)
            if kind == "hit":
                return state_pg.PgHitCounter(clock, session_factory=factory)
            if kind == "limiter":
                return state_pg_auth.PgSlidingWindowLimiter(60, 10, clock, session_factory=factory)
            return state_pg_auth.PgJtiBlacklist(clock, session_factory=factory)
        if kind == "cache":
            return ExactCache(clock, ttl_s)
        if kind == "counter":
            return DailyCounter(clock)
        if kind == "hit":
            return HitCounter(clock)
        if kind == "limiter":
            return SlidingWindowLimiter(60, 10, clock)
        return JtiBlacklist(clock)

    return _make


def _cache_rows(pg) -> int:
    with Session(pg) as s:
        return s.scalar(select(func.count()).select_from(StateCacheEntry))


def _jti_rows(pg) -> int:
    with Session(pg) as s:
        return s.scalar(
            select(func.count())
            .select_from(StateAuthGuard)
            .where(StateAuthGuard.guard_type == "jti")
        )


def _daily_n(pg, user_id: int) -> int:
    with Session(pg) as s:
        return s.scalar(
            select(StateCounter.n).where(
                StateCounter.scope == "daily", StateCounter.ckey == str(user_id)
            )
        )


def _seed_chunks(session: Session, title: str) -> list[Chunk]:
    """session 事务内种 1 文档 + 2 块（flush 用例：与传入会话同事务，读回一致）。"""
    dept_id = session.scalar(select(User.dept_id).where(User.username == "admin"))
    admin_id = session.scalar(select(User.id).where(User.username == "admin"))
    doc = Document(
        title=title,
        dept_id=dept_id,
        original_filename=f"{title}.docx",
        file_path=f"seed/{hashlib.sha256(title.encode()).hexdigest()}.docx",
        mime=_DOC_MIME,
        size_bytes=16,
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        is_active=True,
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    chunks = [
        Chunk(
            document_id=doc.id,
            dept_id=doc.dept_id,
            title_path=title,
            content=f"{title}-块{i}",
            content_chars=8,
            position=i,
            embedding=[0.0] * 1024,
        )
        for i in range(2)
    ]
    session.add_all(chunks)
    session.flush()
    return chunks


_ENTRY = {"answer": "年假 5 天", "citations": [{"chunk_id": 7, "page_no": 3}], "answer_type": "doc"}


# ---------- ExactCache 协议面 ----------


def test_make_key_四形态与内存版逐字节一致(backend: str, make_component) -> None:
    """键生成与后端无关（仅钉协议）：部门进键 / user_suffix 插入 / None 缺省同形 /
    同问题不同部门键不同——与 ExactCache.make_key 逐字节一致。"""
    cache = make_component("cache")
    assert cache.make_key(3, "年假有几天") == "chat:3:年假有几天"
    assert cache.make_key(3, "年假有几天", user_suffix="u7") == "chat:3:u7:年假有几天"
    assert cache.make_key(3, "年假有几天", user_suffix=None) == "chat:3:年假有几天"
    assert cache.make_key(1, "q") != cache.make_key(2, "q")


def test_put后get读回entry全字段相等(backend: str, make_component) -> None:
    cache = make_component("cache")
    key = cache.make_key(1, "年假")
    cache.put(key, _ENTRY)
    assert cache.get(key) == _ENTRY


def test_ttl过期get返回None且行被惰性删除(
    backend: str, make_component, clock: FakeClock, pg
) -> None:
    """前拨 ttl+1s：get 返回 None；pg 读回断言过期行已被惰性删除（无后台回收的同口径）。"""
    cache = make_component("cache", ttl_s=300)
    key = cache.make_key(1, "q")
    cache.put(key, _ENTRY)
    clock.advance(seconds=301)
    assert cache.get(key) is None
    if backend == "pg":
        assert _cache_rows(pg) == 0


def test_ttl内仍命中读回原文(backend: str, make_component, clock: FakeClock) -> None:
    cache = make_component("cache", ttl_s=300)
    key = cache.make_key(1, "q")
    cache.put(key, _ENTRY)
    clock.advance(seconds=299)
    assert cache.get(key) == _ENTRY


def test_get返回对象改写不污染缓存(backend: str, make_component) -> None:
    """深拷贝出借（pg 经 JSONB 反序列化天然新对象，memory 显式 deepcopy）。"""
    cache = make_component("cache")
    key = cache.make_key(1, "q")
    cache.put(key, _ENTRY)
    returned = cache.get(key)
    assert returned is not None
    returned["citations"].append("污染")
    returned["answer"] = "改写"
    assert cache.get(key) == _ENTRY


def test_clear整清后再put可用(backend: str, make_component) -> None:
    cache = make_component("cache")
    cache.put(cache.make_key(1, "年假"), _ENTRY)
    cache.put(cache.make_key(2, "病假"), _ENTRY)
    cache.clear()
    assert cache.get(cache.make_key(1, "年假")) is None
    assert cache.get(cache.make_key(2, "病假")) is None
    cache.put(cache.make_key(1, "年假"), {"answer": "新", "citations": [], "answer_type": "doc"})
    assert cache.get(cache.make_key(1, "年假")) == {
        "answer": "新",
        "citations": [],
        "answer_type": "doc",
    }


def test_clear_by_ref_命中删(backend: str, make_component) -> None:
    cache = make_component("cache")
    cache.put("k:doc7", {"answer": "A", "citations": [{"refType": "doc", "refId": 7}]})
    cache.put("k:doc9", {"answer": "B", "citations": [{"refType": "doc", "refId": 9}]})
    assert cache.clear_by_ref("doc", 7) == 1
    assert cache.get("k:doc7") is None
    assert cache.get("k:doc9") == {"answer": "B", "citations": [{"refType": "doc", "refId": 9}]}


def test_clear_by_ref_同条目多引用任一命中即删(backend: str, make_component) -> None:
    cache = make_component("cache")
    cache.put(
        "k:mix", {"citations": [{"refType": "doc", "refId": 7}, {"refType": "qa", "refId": 3}]}
    )
    assert cache.clear_by_ref("qa", 3) == 1
    assert cache.get("k:mix") is None


def test_clear_by_ref_无citations条目保守删除(backend: str, make_component) -> None:
    cache = make_component("cache")
    cache.put("k:odd", {"answer": "X"})
    assert cache.clear_by_ref("doc", 7) == 1
    assert cache.get("k:odd") is None


def test_clear_by_ref_无命中返回零且不影响其余(backend: str, make_component) -> None:
    cache = make_component("cache")
    cache.put("k:doc9", {"citations": [{"refType": "doc", "refId": 9}]})
    assert cache.clear_by_ref("doc", 999) == 0
    assert cache.get("k:doc9") is not None


def test_clear_by_ref_异型refId文本归一(backend: str, make_component) -> None:
    """修订 R2：refId 统一文本比较——int 7 与 "7" 均按 7 命中；按 9 不命中。
    JSONB 侧 c->>'refId' 天然文本归一，内存版 _touches 同步 str() 归一（两侧一致）。"""
    cache = make_component("cache")
    cache.put("k:int7", {"citations": [{"refType": "doc", "refId": 7}]})
    cache.put("k:str7", {"citations": [{"refType": "doc", "refId": "7"}]})
    assert cache.clear_by_ref("doc", 7) == 2
    assert cache.get("k:int7") is None
    assert cache.get("k:str7") is None
    cache.put("k:int9", {"citations": [{"refType": "doc", "refId": 9}]})
    assert cache.clear_by_ref("doc", 9) == 1
    assert cache.get("k:int9") is None


# ---------- DailyCounter 协议面 ----------


def test_try_consume_恰限放行(backend: str, make_component) -> None:
    counter = make_component("counter")
    assert [counter.try_consume(42, limit=3) for _ in range(3)] == [True, True, True]


def test_try_consume_超限拒且不计数读回n(backend: str, make_component, pg) -> None:
    """第 limit+1 次返回 False 且拒绝零写入；pg 读回 n == limit（未越推越满）。"""
    counter = make_component("counter")
    for _ in range(3):
        counter.try_consume(42, limit=3)
    assert counter.try_consume(42, limit=3) is False
    assert counter.try_consume(42, limit=3) is False
    if backend == "pg":
        assert _daily_n(pg, 42) == 3


def test_try_consume_跨日重置(backend: str, make_component, clock: FakeClock) -> None:
    counter = make_component("counter")
    for _ in range(3):
        assert counter.try_consume(42, limit=3) is True
    assert counter.try_consume(42, limit=3) is False
    clock.advance(days=1)
    assert [counter.try_consume(42, limit=3) for _ in range(3)] == [True, True, True]
    assert counter.try_consume(42, limit=3) is False


def test_try_consume_按user隔离(backend: str, make_component) -> None:
    counter = make_component("counter")
    assert counter.try_consume(1, limit=1) is True
    assert counter.try_consume(1, limit=1) is False
    assert counter.try_consume(2, limit=1) is True


# ---------- HitCounter 协议面（flush 用例走 session 事务，种子随测试回滚） ----------


class _ExplodingSession:
    """失败路径替身：execute 即抛（memory 走 connection() 通道 / pg 直走会话，双双覆盖）。"""

    def connection(self) -> "_ExplodingSession":
        return self

    def execute(self, statement: object, params: object = None) -> None:
        raise RuntimeError("pg 不可用")

    def commit(self) -> None:
        raise AssertionError("execute 已失败，不应走到提交")


class _ForbidSQLSession:
    """空聚合红线替身（memory 版契约：零 SQL）；pg 版允许一条 SELECT（设计 §5.3 偏差）。"""

    def connection(self) -> "_ForbidSQLSession":
        return self

    def execute(self, statement: object, params: object = None) -> None:
        raise AssertionError("空聚合 flush 不得发起 SQL")

    def commit(self) -> None:
        raise AssertionError("空聚合 flush 不得提交")


def test_hit_add合并同id_flush读回hit_count与last_hit_at(
    backend: str, make_component, session: Session, clock: FakeClock
) -> None:
    """add([id1,id1,id2]) → flush：读回 chunk1 hit_count==2、chunk2==1，
    last_hit_at == 注入时钟时刻；flush 返回去重 chunk 数 2。"""
    c1, c2 = _seed_chunks(session, "客服_等价_命中计数")
    hit = make_component("hit")
    hit.add([c1.id, c1.id, c2.id])
    flushed = hit.flush(session)
    assert flushed == 2
    rows = session.execute(
        select(Chunk.id, Chunk.hit_count, Chunk.last_hit_at).where(Chunk.id.in_([c1.id, c2.id]))
    ).all()
    assert {rid: (hc, lha) for rid, hc, lha in rows} == {
        c1.id: (2, clock.now()),
        c2.id: (1, clock.now()),
    }


def test_hit_flush后聚合清空_再flush返回零(backend: str, make_component, session: Session) -> None:
    """提交成功后聚合清空：再 flush 返回 0。memory 零 SQL（_ForbidSQLSession 钉死）；
    pg 偏差为一条 SELECT（设计 §5.3，走 session 断言返回值）。"""
    c1, _ = _seed_chunks(session, "客服_等价_聚合清空")
    hit = make_component("hit")
    hit.add([c1.id])
    assert hit.flush(session) == 1
    if backend == "memory":
        assert hit.flush(_ForbidSQLSession()) == 0
    else:
        assert hit.flush(session) == 0


def test_hit_flush失败滚入_再flush带全量(backend: str, make_component, session: Session) -> None:
    """execute 失败原样上抛，聚合/暂存未消费：再 flush 带全量（失败滚入下一周期）。"""
    c1, _ = _seed_chunks(session, "客服_等价_失败滚入")
    hit = make_component("hit")
    hit.add([c1.id, c1.id])
    with pytest.raises(RuntimeError, match="pg 不可用"):
        hit.flush(_ExplodingSession())
    assert hit.flush(session) == 1
    hit_count = session.scalar(select(Chunk.hit_count).where(Chunk.id == c1.id))
    assert hit_count == 2


# ---------- SlidingWindowLimiter 协议面 ----------


def test_limiter_窗口内恰max次放行(backend: str, make_component, clock: FakeClock) -> None:
    """窗口 60s 内恰 10 次放行（逐次前拨 1s 产生独立事件时刻），第 11 次拒绝。"""
    limiter = make_component("limiter")
    for _ in range(10):
        assert limiter.hit("1.2.3.4|alice") is True
        clock.advance(seconds=1)
    assert limiter.hit("1.2.3.4|alice") is False


def test_limiter_超窗重置(backend: str, make_component, clock: FakeClock) -> None:
    limiter = make_component("limiter")
    for _ in range(10):
        assert limiter.hit("1.2.3.4|alice") is True
        clock.advance(seconds=1)
    assert limiter.hit("1.2.3.4|alice") is False
    clock.advance(seconds=61)  # 窗外：事件全部出窗（pg 顺手清理 / memory 过滤）
    assert limiter.hit("1.2.3.4|alice") is True


def test_limiter_reset清空(backend: str, make_component, clock: FakeClock) -> None:
    limiter = make_component("limiter")
    for _ in range(10):
        limiter.hit("1.2.3.4|alice")
    assert limiter.hit("1.2.3.4|alice") is False
    limiter.reset()
    assert limiter.hit("1.2.3.4|alice") is True


# ---------- JtiBlacklist 协议面 ----------


def test_jti_revoke后revoked_True(backend: str, make_component, clock: FakeClock) -> None:
    blacklist = make_component("jti")
    exp = clock.now().timestamp() + 100
    blacklist.revoke("jti-a", exp)
    assert blacklist.revoked("jti-a") is True
    assert blacklist.revoked("jti-b") is False


def test_jti_过期后False(backend: str, make_component, clock: FakeClock) -> None:
    """条目 TTL=令牌剩余有效期：前拨过 exp 即视同不存在（未撤销）。"""
    blacklist = make_component("jti")
    exp = clock.now().timestamp() + 100
    blacklist.revoke("jti-a", exp)
    clock.advance(seconds=101)
    assert blacklist.revoked("jti-a") is False


def test_jti_revoke顺手清过期行(backend: str, make_component, clock: FakeClock, pg) -> None:
    """revoke 顺手清理过期条目（pg 读回行数；memory 断言行为等价）。"""
    blacklist = make_component("jti")
    blacklist.revoke("jti-old", clock.now().timestamp() + 10)
    clock.advance(seconds=11)
    blacklist.revoke("jti-new", clock.now().timestamp() + 100)
    assert blacklist.revoked("jti-old") is False
    assert blacklist.revoked("jti-new") is True
    if backend == "pg":
        assert _jti_rows(pg) == 1  # 过期行已被顺手删除


def test_jti_reset清空(backend: str, make_component, clock: FakeClock) -> None:
    blacklist = make_component("jti")
    blacklist.revoke("jti-a", clock.now().timestamp() + 100)
    blacklist.reset()
    assert blacklist.revoked("jti-a") is False
