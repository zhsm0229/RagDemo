r"""召回计数聚合器集成测试 + flush 调度装配（Task 7，真 pg 读回，§15.4）。

brief 用例 ④：HitCounter.add([id1,id1,id2]) → flush 后真 pg 读回 chunk1
hit_count==2、chunk2==1 且 last_hit_at == 注入时钟时刻；空聚合 flush 返回 0 且
不动库（不发 SQL 的红线由单元测试 _ForbidSQLSession 把关，本文件断言返回值与库态）。
调度段：add_hit_flush_job 以 IntervalTrigger(every cache_ttl_s) 注册到 W1 装配的
调度器（test_mode 装配不自启，不依赖真实等待）；任务体 run_hit_flush 经独立会话
刷库——app.db.base 的模块级 engine 在收集期即钉住 dev URL，故测试以 monkeypatch
把 SessionLocal 指向 pg fixture 的测试引擎，种子与清理用独立提交事务（该路径
不走 session fixture 的回滚保护，必须自行清理，禁静默残留）。
"""

import hashlib
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Self

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

import app.services.scheduler as scheduler_mod
from app.core.clock import FakeClock
from app.core.config import Settings
from app.db.models import Chunk, Document, User
from app.services.chat_cache import HitCounter
from app.services.scheduler import add_hit_flush_job, build_scheduler, run_hit_flush

FLUSH_AT = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
CHUNK_VEC = [0.0] * 1024  # 命中计数不涉向量运算：零向量直插即可满足 NOT NULL


def _dept_id(session: Session) -> int:
    return session.scalar(select(User.dept_id).where(User.username == "admin"))


def _admin_id(session: Session) -> int:
    return session.scalar(select(User.id).where(User.username == "admin"))


def _make_doc(session: Session, title: str) -> Document:
    doc = Document(
        title=title,
        dept_id=_dept_id(session),
        original_filename=f"{title}.docx",
        file_path=f"seed/{hashlib.sha256(title.encode()).hexdigest()}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        is_active=True,
        uploaded_by=_admin_id(session),
    )
    session.add(doc)
    session.flush()
    return doc


def _add_chunk(session: Session, doc: Document, content: str, *, position: int = 0) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=position,
        embedding=CHUNK_VEC,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _hit_rows(session: Session, chunk_ids: list[int]) -> dict[int, tuple[int, datetime | None]]:
    """按 id 读回 (hit_count, last_hit_at)，与 ORM 身份态解耦，纯库态断言。"""
    rows = session.execute(
        select(Chunk.id, Chunk.hit_count, Chunk.last_hit_at).where(Chunk.id.in_(chunk_ids))
    ).all()
    return {chunk_id: (hit_count, last_hit_at) for chunk_id, hit_count, last_hit_at in rows}


def _seed_committed(pg: object, title: str) -> tuple[int, list[int]]:
    """独立提交事务直插 1 文档 + 2 块（run_hit_flush 走独立会话，回滚保护罩不到）。"""
    with Session(pg) as setup, setup.begin():  # type: ignore[arg-type]
        doc = _make_doc(setup, title)
        chunk_ids = [_add_chunk(setup, doc, f"{title}-块{i}", position=i).id for i in range(2)]
        doc_id = doc.id  # 会话关闭后实例已脱离，id 须在事务内取
    return doc_id, chunk_ids


def _cleanup_committed(pg: object, titles: list[str]) -> None:
    """按哨兵标题幂等清理（块随文档删）：既作 finally 收尾，也防前轮失败残留——
    种子是已提交数据，清理若只挂在 finally，种子自身失败时会静默残留污染后轮。"""
    with Session(pg) as cleanup, cleanup.begin():  # type: ignore[arg-type]
        doc_ids = cleanup.scalars(select(Document.id).where(Document.title.in_(titles))).all()
        cleanup.execute(delete(Chunk).where(Chunk.document_id.in_(doc_ids)))
        cleanup.execute(delete(Document).where(Document.id.in_(doc_ids)))


# ---------- ④ flush 真 pg 读回（§15.4）----------


def test_flush_persists_aggregated_hits_and_last_hit_at(session: Session) -> None:
    """add([id1,id1,id2]) → flush：真 pg 读回 chunk1 hit_count==2、chunk2==1，
    last_hit_at 均为注入时钟时刻（同批刷库统一置 now）。"""
    doc = _make_doc(session, "客服_命中计数_20260928")
    c1 = _add_chunk(session, doc, "年假天数说明块", position=0)
    c2 = _add_chunk(session, doc, "调休流程说明块", position=1)

    counter = HitCounter(FakeClock("2026-10-15T09:30:00+00:00"))
    counter.add([c1.id, c1.id, c2.id])
    flushed = counter.flush(session)

    assert flushed == 2  # 去重后的 chunk 数
    assert _hit_rows(session, [c1.id, c2.id]) == {
        c1.id: (2, FLUSH_AT),
        c2.id: (1, FLUSH_AT),
    }


def test_flush_with_empty_aggregation_returns_zero_and_leaves_db_untouched(
    session: Session,
) -> None:
    """空聚合 flush：返回 0，库内 hit_count 保持 0、last_hit_at 保持 NULL。"""
    doc = _make_doc(session, "客服_命中计数_空聚合_20260928")
    chunk = _add_chunk(session, doc, "空聚合不写库", position=0)

    counter = HitCounter(FakeClock("2026-10-15T09:30:00+00:00"))
    flushed = counter.flush(session)

    assert flushed == 0
    assert _hit_rows(session, [chunk.id]) == {chunk.id: (0, None)}


# ---------- flush 任务体：独立会话 + 失败保留聚合 ----------


def test_run_hit_flush_flushes_via_independent_session(monkeypatch: pytest.MonkeyPatch, pg) -> None:
    """任务体经 monkeypatch 到测试库的独立 SessionLocal 刷库：真 pg 读回命中计数。"""
    title = "客服_命中计数_任务体_20260928"
    _cleanup_committed(pg, [title])
    try:
        _seed_committed(pg, title)
        with Session(pg) as known:  # type: ignore[arg-type]
            chunk_ids = list(
                known.scalars(
                    select(Chunk.id).where(Chunk.title_path == title).order_by(Chunk.position)
                ).all()
            )
        assert len(chunk_ids) == 2
        counter = HitCounter(FakeClock("2026-10-15T09:30:00+00:00"))
        counter.add([chunk_ids[0], chunk_ids[1]])
        monkeypatch.setattr(
            scheduler_mod, "SessionLocal", sessionmaker(bind=pg, expire_on_commit=False)
        )
        run_hit_flush(counter)
        with Session(pg) as check:  # type: ignore[arg-type]
            assert _hit_rows(check, chunk_ids) == {
                chunk_ids[0]: (1, FLUSH_AT),
                chunk_ids[1]: (1, FLUSH_AT),
            }
    finally:
        _cleanup_committed(pg, [title])


def test_run_hit_flush_swallows_pg_error_and_keeps_aggregation(
    monkeypatch: pytest.MonkeyPatch, pg, caplog: pytest.LogCaptureFixture
) -> None:
    """pg 异常轮次：logger.exception 吞掉不上抛（调度线程不被打垮），聚合保留，
    下一轮恢复后真 pg 读回计数落库（保留聚合滚入下一周期的端到端证据）。"""

    class _BrokenSession:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("pg 不可用")

    title = "客服_命中计数_失败重试_20260928"
    _cleanup_committed(pg, [title])
    try:
        _seed_committed(pg, title)
        with Session(pg) as known:  # type: ignore[arg-type]
            chunk_ids = list(
                known.scalars(
                    select(Chunk.id).where(Chunk.title_path == title).order_by(Chunk.position)
                ).all()
            )
        counter = HitCounter(FakeClock("2026-10-15T09:30:00+00:00"))
        counter.add([chunk_ids[0]])
        monkeypatch.setattr(scheduler_mod, "SessionLocal", _BrokenSession)
        with caplog.at_level(logging.ERROR):
            run_hit_flush(counter)  # 不上抛
        assert any(
            record.levelname == "ERROR" and "召回计数刷库失败" in record.getMessage()
            for record in caplog.records
        )

        monkeypatch.setattr(
            scheduler_mod, "SessionLocal", sessionmaker(bind=pg, expire_on_commit=False)
        )
        run_hit_flush(counter)  # 下一轮：保留的 [id] 聚合成功落库
        with Session(pg) as check:  # type: ignore[arg-type]
            assert _hit_rows(check, chunk_ids) == {
                chunk_ids[0]: (1, FLUSH_AT),
                chunk_ids[1]: (0, None),
            }
    finally:
        _cleanup_committed(pg, [title])


# ---------- flush 调度装配 ----------


def test_add_hit_flush_job_registers_interval_flush_job() -> None:
    """W1 装配的调度器上挂 flush 任务：IntervalTrigger 周期 == 传入的 cache_ttl_s。"""
    settings = Settings(jwt_secret="chat-cache-test", test_mode=True)
    scheduler = build_scheduler(settings)
    add_hit_flush_job(
        scheduler,
        HitCounter(FakeClock("2026-10-15T09:30:00+00:00")),
        interval_s=settings.cache_ttl_s,
    )
    job = scheduler.get_job("flush_hit_counter")
    assert job is not None
    assert job.trigger.interval == timedelta(seconds=300)
