r"""文档抽样造题通道集成测试（04 票，真 pg + Stub LLM）。

用例：
① 零召回优先：同部门近召回块 / 30 天前召回块 / 从未召回块并存，抽样 1 块 →
   取从未召回块（零召回口径与概览同源：last_hit_at IS NULL 优先，其后超 30 天）；
② 短块跳过：清洗后 <100 字的块不出题（质量红线同款口径）；
③ 正路：造题入扩充集（origin=doc_synth/tier=extended），source_doc_id 与
   source_ref（含块 id，用于同块去重）回填；
④ 同块去重：上一轮已给该块造过题（source_ref 在册）→ 本轮跳过；
⑤ max_total 全局预算截断（跨部门）。
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.db.models import Chunk, Department, Document, EvalQuestion, User
from app.services.eval_synth import synthesize_from_chunks

_T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
_CLOCK = FakeClock("2026-10-20T04:00:00+00:00")
_CHUNK_VEC = [0.0] * 1024
_LONG = (
    "蓝鲸值班制度规定：每班次四小时，夜班补贴每晚八十元，换班前十五分钟完成工单交接，未完成交接视为迟到，月度累计三次迟到扣发当月全勤奖。"
    * 2
)


class _StubLLM:
    def __init__(self, question: str, answer: str) -> None:
        self.question = question
        self.answer = answer
        self.chunks_seen: list[str] = []

    def chat_stream(self, messages: Any, max_tokens: int) -> Any:
        prompt = str(messages[-1]["content"]) if messages else ""
        self.chunks_seen.append(prompt)
        yield json.dumps({"question": self.question, "answer": self.answer}, ensure_ascii=False)


def _dept_id(session: Session, code: str = "service") -> int:
    return session.scalar(select(Department.id).where(Department.code == code))


def _admin_id(session: Session) -> int:
    return session.scalar(select(User.id).where(User.username == "admin"))


def _make_doc(session: Session, title: str, dept_code: str = "service") -> Document:
    import hashlib

    doc = Document(
        title=title,
        dept_id=_dept_id(session, dept_code),
        original_filename=f"{title}.docx",
        file_path=f"docgen/{hashlib.sha256(title.encode()).hexdigest()}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
        effective_date=_T0.date(),
        owner_name="制度责任人",
        status="ready",
        uploaded_by=_admin_id(session),
    )
    session.add(doc)
    session.flush()
    return doc


def _make_chunk(
    session: Session,
    doc: Document,
    content: str = _LONG,
    *,
    last_hit_at: datetime | None = None,
    hit_count: int = 0,
) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=0,
        embedding=_CHUNK_VEC,
        hit_count=hit_count,
        last_hit_at=last_hit_at,
    )
    session.add(chunk)
    session.flush()
    return chunk


def test_零召回块优先抽样(session: Session) -> None:
    """① 从未召回 > 超 30 天未召回 > 近期召回：抽样 1 块取从未召回者。"""
    doc = _make_doc(session, "值班手册")
    _make_chunk(
        session, doc, last_hit_at=_CLOCK.now() - timedelta(days=1), hit_count=5
    )  # 近期召回块（对照，不应被抽中）
    _make_chunk(
        session,
        doc,
        content="超期未召回块正文。" + _LONG,
        last_hit_at=_CLOCK.now() - timedelta(days=40),
        hit_count=3,
    )  # 超 30 天未召回块（对照，排序次于从未召回）
    never = _make_chunk(session, doc, content="从未召回块正文。" + _LONG)
    llm = _StubLLM("从没被问到的值班问题", "块内答案")

    summary = synthesize_from_chunks(session, _CLOCK, llm=llm, sample_per_dept=1, max_total=10)

    assert summary["synthesized"] == 1
    # 唯一前缀标记判定抽样归属：从未召回块入选，超期/近期块未送 LLM
    assert "从未召回块正文。" in llm.chunks_seen[0]
    assert "超期未召回块正文。" not in llm.chunks_seen[0]
    question = session.scalars(select(EvalQuestion)).one()
    assert question.origin == "doc_synth"
    assert question.tier == "extended"
    assert question.source_doc_id == doc.id
    assert f"块{never.id}" in (question.source_ref or "")


def test_短块跳过(session: Session) -> None:
    """② 清洗后 <100 字的块不出题（质量红线同款口径），计 skipped。"""
    doc = _make_doc(session, "短文档")
    _make_chunk(session, doc, content="太短的块。")
    llm = _StubLLM("问题", "答案")

    summary = synthesize_from_chunks(session, _CLOCK, llm=llm, sample_per_dept=5, max_total=10)

    assert summary["synthesized"] == 0
    assert summary["skipped"] == 1
    assert session.scalars(select(EvalQuestion)).all() == []


def test_同块去重(session: Session) -> None:
    """④ 上一轮已给该块造过题（source_ref 在册）→ 本轮跳过该块。"""
    doc = _make_doc(session, "手册二")
    chunk = _make_chunk(session, doc)
    session.add(
        EvalQuestion(
            dept_id=_dept_id(session),
            question="既有问题",
            expected_answer="既有答案",
            source_ref=f"文档造题·块{chunk.id}",
            source_doc_id=doc.id,
            origin="doc_synth",
            tier="extended",
        )
    )
    session.flush()
    llm = _StubLLM("不该被生成的问题", "答案")

    summary = synthesize_from_chunks(session, _CLOCK, llm=llm, sample_per_dept=5, max_total=10)

    assert summary["synthesized"] == 0
    assert llm.chunks_seen == []  # 块在进 LLM 前就被去重剔除


def test_全局预算跨部门截断(session: Session) -> None:
    """⑤ max_total 全局预算：service 出 1 题后预算耗尽，hr 部门不再造题。"""
    svc_doc = _make_doc(session, "客服文档", "service")
    _make_chunk(session, svc_doc)
    hr_doc = _make_doc(session, "人力文档", "hr")
    _make_chunk(session, hr_doc)
    llm = _StubLLM("跨部门问题", "答案")

    summary = synthesize_from_chunks(session, _CLOCK, llm=llm, sample_per_dept=2, max_total=1)

    assert summary["synthesized"] == 1
    question = session.scalars(select(EvalQuestion)).one()
    assert question.dept_id == _dept_id(session, "service")
