r"""进程内状态外置并发语义测试（工单 19 设计 §7.2 + 修订 R1/R3）。

隔离口径：同 test_state_pg_equivalence——PG 实现自带短会话 commit，autouse fixture
每 Case 先 TRUNCATE 三张 state_* 表（rag_test 真库），不用 session 回滚隔离；R3 用例
的 chunk 种子为**已提交**数据（flush 走两个独立 engine 的独立连接，回滚罩不住），
finally 幂等清理，禁静默残留。

- 并发限额：同一 quota 的并发 try_consume 恰放行 limit 次（pg 短会话每次独立连接，
  跨连接行锁原子性真库实证；memory 锁语义同口径）。
- R1 共享语义：两个组件实例（=两个副本视角）写读互见随后端变化——pg 互见（外置
  核心价值，工单 20 多副本前提）、memory 互不可见（防误把 memory 当全局）。
- R3 确定性并发：两个独立 engine（各自 NullPool）+ commit 代理插队——flush 会话
  FOR UPDATE 持锁窗口内由另一 engine 发起 add（确定性插队在 commit 前），commit
  后按新行落地，第二轮 flush 带上——复用 test_chat_cache.py「commit 窗口 add」契约
  语义，换真库实证两段增量总和读回。
"""

import hashlib
import threading
from datetime import date

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.clock import FakeClock
from app.db.models import Chunk, Document, User
from app.services.chat_cache import DailyCounter, ExactCache

CLOCK_START = "2026-10-15T09:30:00+00:00"
_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture(params=["memory", "pg"])
def backend(request: pytest.FixtureRequest) -> str:
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


def _counter_for(backend: str, clock: FakeClock, pg):
    if backend == "pg":
        from app.services.state_pg import PgDailyCounter

        return PgDailyCounter(clock, session_factory=sessionmaker(bind=pg, expire_on_commit=False))
    return DailyCounter(clock)


def _cache_for(backend: str, clock: FakeClock, pg):
    if backend == "pg":
        from app.services.state_pg import PgExactCache

        return PgExactCache(
            clock, 300, session_factory=sessionmaker(bind=pg, expire_on_commit=False)
        )
    return ExactCache(clock, 300)


# ---------- 并发限额（§7.2，真库跨连接行锁） ----------


def test_同user并发try_consume恰放行limit次(backend: str, clock: FakeClock, pg) -> None:
    """8 线程 × 10 次（pg 每次调用独立短会话连接）limit=25：放行恰 25、拒绝恰 55——
    单语句 ON CONFLICT...WHERE 在 PK 行锁下的跨连接原子性。"""
    counter = _counter_for(backend, clock, pg)
    workers, calls, limit = 8, 10, 25
    gate = threading.Barrier(workers)
    results: list[bool] = []
    collect = threading.Lock()

    def worker() -> None:
        gate.wait()
        local = [counter.try_consume(42, limit=limit) for _ in range(calls)]
        with collect:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == limit
    assert results.count(False) == workers * calls - limit


# ---------- R1 跨实例共享语义（外置核心价值） ----------


def test_两实例写读互见_共享语义随后端变化(backend: str, clock: FakeClock, pg) -> None:
    """两个组件实例（=两个副本视角）：pg 同 key put/get 互见、日额度共享；
    memory 互不可见、额度各自独立（钉死「共享语义随后端变化」，防误把 memory 当全局）。"""
    cache_a = _cache_for(backend, clock, pg)
    cache_b = _cache_for(backend, clock, pg)
    entry = {"answer": "A", "citations": [], "answer_type": "doc"}
    cache_a.put(cache_a.make_key(3, "年假"), entry)
    if backend == "pg":
        assert cache_b.get(cache_b.make_key(3, "年假")) == entry
    else:
        assert cache_b.get(cache_b.make_key(3, "年假")) is None

    counter_a = _counter_for(backend, clock, pg)
    counter_b = _counter_for(backend, clock, pg)
    assert counter_a.try_consume(7, limit=1) is True
    if backend == "pg":
        assert counter_b.try_consume(7, limit=1) is False  # 额度跨实例共享
    else:
        assert counter_b.try_consume(7, limit=1) is True  # 各实例独立额度


# ---------- R3 flush/add 并发确定性（两个独立 engine + commit 代理插队） ----------


def _cleanup_committed(pg, title: str) -> None:
    """按哨兵标题幂等清理已提交种子（块随文档删）：finally 收尾 + 防前轮失败残留。"""
    with Session(pg) as cleanup, cleanup.begin():
        doc_ids = cleanup.scalars(select(Document.id).where(Document.title == title)).all()
        if doc_ids:
            cleanup.execute(Chunk.__table__.delete().where(Chunk.document_id.in_(doc_ids)))
            cleanup.execute(Document.__table__.delete().where(Document.id.in_(doc_ids)))


def _seed_committed_chunk(pg, title: str) -> int:
    """独立提交事务直插 1 块（flush 走独立 engine 连接，回滚罩不住 → 必须已提交）。"""
    with Session(pg) as setup, setup.begin():
        dept_id = setup.scalar(select(User.dept_id).where(User.username == "admin"))
        admin_id = setup.scalar(select(User.id).where(User.username == "admin"))
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
        setup.add(doc)
        setup.flush()
        chunk = Chunk(
            document_id=doc.id,
            dept_id=dept_id,
            title_path=title,
            content=f"{title}-块0",
            content_chars=8,
            position=0,
            embedding=[0.0] * 1024,
        )
        setup.add(chunk)
        setup.flush()
        return chunk.id  # 会话关闭后实例脱离，id 须在事务内取


def test_flush持锁窗口内另一engine的add计数不丢(pg, clock: FakeClock) -> None:
    """R3：flush 会话 FOR UPDATE 持锁窗口内，另一 engine 发起 add（确定性插队在
    commit 前）——add 的 upsert 阻塞在行锁上，commit 后冲突目标行已删、按新行插入；
    第二轮 flush 搬运该增量，hit_count 读回 == 两段增量总和（2+1=3）。"""
    from app.services.state_pg import PgHitCounter

    title = "客服_并发_flush与add_20260928"
    _cleanup_committed(pg, title)
    # 独立 engine 各自 NullPool：连接零共享，行锁/咨询锁语义为真并发
    db_url = pg.url.render_as_string(hide_password=False)  # str(url) 会把密码打码
    engine_a = create_engine(db_url, poolclass=NullPool)
    engine_b = create_engine(db_url, poolclass=NullPool)
    try:
        chunk_id = _seed_committed_chunk(pg, title)
        flush_counter = PgHitCounter(
            clock, session_factory=sessionmaker(bind=engine_a, expire_on_commit=False)
        )
        add_counter = PgHitCounter(
            clock, session_factory=sessionmaker(bind=engine_b, expire_on_commit=False)
        )
        flush_counter.add([chunk_id, chunk_id])  # 第一段增量 n=2（engine_a 短会话提交）

        class _AddDuringCommitProxy:
            """flush 会话代理：commit 释放锁前由另一 engine 发起 add（阻塞在行锁上）。"""

            def __init__(self, real: Session) -> None:
                self._real = real
                self._adder: threading.Thread | None = None

            def connection(self) -> object:
                # 必须透传真实 Connection（Core 通道）：经 Session.execute 走 ORM
                # 批量 UPDATE 路径会报 InvalidRequestError（chat_cache.py 同坑先例）
                return self._real.connection()

            def execute(self, statement: object, params: object = None) -> object:
                return self._real.execute(statement, params)  # type: ignore[arg-type]

            def commit(self) -> None:
                self._adder = threading.Thread(target=add_counter.add, args=([chunk_id],))
                self._adder.start()  # upsert 阻塞在 flush 持有的 FOR UPDATE 行锁上
                self._real.commit()  # 释放锁 → 冲突目标行已删，add 按新行插入落地
                self._adder.join()

        with Session(engine_a, expire_on_commit=False) as flush_session:
            assert flush_counter.flush(_AddDuringCommitProxy(flush_session)) == 1
        with Session(engine_a, expire_on_commit=False) as second_session:
            assert flush_counter.flush(second_session) == 1  # 第二轮带上 commit 窗口的 add
        with Session(engine_a) as check:
            hit_count = check.scalar(select(Chunk.hit_count).where(Chunk.id == chunk_id))
        assert hit_count == 3  # 两段增量总和：flush 内 n=2 + commit 窗口 add n=1
    finally:
        _cleanup_committed(pg, title)
        engine_a.dispose()
        engine_b.dispose()
