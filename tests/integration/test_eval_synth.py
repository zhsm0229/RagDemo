r"""点踩种子合成通道集成测试（03 票，真 pg + Stub LLM）。

用例：
① 正路：pending 种子（引用可达）→ LLM 产 2 变体 + 期望答案 → 扩充集入 2 题
   （origin=hardcase_synth/tier=extended/source_doc_id 回填），种子 synthesized；
② 引用块缺失（refId 指向不存在/停用块）→ 种子置 no_ground，零出题；
③ LLM 输出非 JSON → 种子保持 pending（可重试），summary 计 skipped；
④ 归一去重：变体与题库既有题（同部门）归一相同 → 该变体跳过；
⑤ 预算：max_per_seed 截单种子变体数；max_total 截全局总量（超额种子保持 pending）。

LLM 走注入 Stub（chat_stream 逐段 yield，服务侧拼接后解析 JSON）——评测自动判分
build_llm(settings, 0.0) 第二实例先例，服务以 llm 参数留测试缝。
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.db.models import Chunk, Department, Document, EvalQuestion, HardcaseSeed, User
from app.services.eval_synth import synthesize_from_seeds

_T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
_CLOCK = FakeClock("2026-10-20T04:00:00+00:00")
_CHUNK_VEC = [0.0] * 1024  # 合成不涉向量运算：零向量直插满足 NOT NULL（W3 手法）


class _StubLLM:
    """固定 payload 的 LLM 替身：记录调用（messages/max_tokens）供断言。"""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[Any] = []

    def chat_stream(self, messages: Any, max_tokens: int) -> Any:
        self.calls.append({"messages": [dict(m) for m in messages], "maxTokens": max_tokens})
        yield self.payload


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
        file_path=f"synth/{hashlib.sha256(title.encode()).hexdigest()}.docx",
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


def _make_chunk(session: Session, doc: Document, content: str) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=content,
        content_chars=len(content),
        position=0,
        embedding=_CHUNK_VEC,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _make_seed(
    session: Session,
    question: str,
    *,
    chunk_id: int | None = None,
    status: str = "pending",
    dept_code: str = "service",
) -> HardcaseSeed:
    citations = [{"n": 1, "refType": "doc", "refId": chunk_id}] if chunk_id is not None else None
    seed = HardcaseSeed(
        message_id=1,
        message_created_at=_T0,
        source_type="feedback_down",
        dept_id=_dept_id(session, dept_code),
        question_text=question,
        normalized_question=question.strip(),
        answer_snapshot="回答原文",
        citations_snapshot=citations,
        status=status,
        created_at=_T0,
        updated_at=_T0,
    )
    session.add(seed)
    session.flush()
    return seed


def _payload(variants: list[str], answer: str) -> str:
    return json.dumps({"variants": variants, "expected_answer": answer}, ensure_ascii=False)


def _questions(session: Session) -> list[EvalQuestion]:
    return list(session.scalars(select(EvalQuestion).order_by(EvalQuestion.id)).all())


def test_点踩种子合成变体入扩充集(session: Session) -> None:
    """① 正路：2 变体 + 期望答案入扩充集，来源/层级/源文档回填，种子收口 synthesized。"""
    doc = _make_doc(session, "报销管理制度")
    chunk = _make_chunk(session, doc, "报销标准：每人每月 200 元，超额部分自理。")
    seed = _make_seed(session, "报销标准是什么？", chunk_id=chunk.id)
    llm = _StubLLM(
        _payload(
            ["报销的上限标准是什么？", "每月报销额度是多少？"], "每人每月 200 元，超额部分自理。"
        )
    )

    summary = synthesize_from_seeds(session, _CLOCK, llm=llm, max_per_seed=2, max_total=10)

    assert summary["synthesized"] == 2
    assert summary["seedsProcessed"] == 1
    questions = _questions(session)
    assert len(questions) == 2
    for item in questions:
        assert item.origin == "hardcase_synth"
        assert item.tier == "extended"
        assert item.qtype == "factual"
        assert item.dept_id == _dept_id(session)
        assert item.expected_answer == "每人每月 200 元，超额部分自理。"
        assert item.source_doc_id == doc.id
        assert "报销" in item.question
    session.expire_all()
    session.refresh(seed)
    assert seed.status == "synthesized"
    assert seed.synth_count == 2
    assert seed.last_synth_at == datetime(2026, 10, 20, 4, 0, tzinfo=UTC)
    # LLM 调用带知识块原文与原问题（grounding 输入），max_tokens 走服务常量
    assert len(llm.calls) == 1
    prompt_text = str(llm.calls[0]["messages"])
    assert "报销标准：每人每月 200 元" in prompt_text
    assert "报销标准是什么？" in prompt_text
    assert llm.calls[0]["maxTokens"] > 0


def test_引用块缺失置no_ground(session: Session) -> None:
    """② refId 指向的块不存在（已删/越界 id）：种子置 no_ground，零出题。"""
    _make_seed(session, "找不到块的问题", chunk_id=999_999)
    llm = _StubLLM(_payload(["变体"], "答案"))

    summary = synthesize_from_seeds(session, _CLOCK, llm=llm, max_per_seed=2, max_total=10)

    assert summary["synthesized"] == 0
    assert summary["noGround"] == 1
    assert _questions(session) == []
    seed = session.scalars(select(HardcaseSeed)).one()
    assert seed.status == "no_ground"


def test_解析失败保持pending(session: Session) -> None:
    """③ LLM 输出非 JSON：种子保持 pending（可重试），summary 计 skipped。"""
    doc = _make_doc(session, "考勤制度")
    chunk = _make_chunk(session, doc, "考勤规则正文。")
    _make_seed(session, "考勤怎么规定？", chunk_id=chunk.id)
    llm = _StubLLM("抱歉，我无法输出 JSON。")

    summary = synthesize_from_seeds(session, _CLOCK, llm=llm, max_per_seed=2, max_total=10)

    assert summary["synthesized"] == 0
    assert summary["skipped"] == 1
    seed = session.scalars(select(HardcaseSeed)).one()
    assert seed.status == "pending"


def test_变体与题库归一去重(session: Session) -> None:
    """④ 变体与题库既有题（同部门）归一相同 → 该变体跳过；其余变体照常入题。"""
    doc = _make_doc(session, "报销管理制度二")
    chunk = _make_chunk(session, doc, "报销标准正文。")
    session.add(
        EvalQuestion(
            dept_id=_dept_id(session),
            question="报销的上限标准是什么？",
            expected_answer="既有答案",
        )
    )
    session.flush()
    _make_seed(session, "报销标准是什么？", chunk_id=chunk.id)
    llm = _StubLLM(
        _payload(["报销的上限标准是什么？", "报销要提前申请吗？"], "答案 B"),
    )

    summary = synthesize_from_seeds(session, _CLOCK, llm=llm, max_per_seed=2, max_total=10)

    assert summary["synthesized"] == 1
    synthesized = [q for q in _questions(session) if q.origin == "hardcase_synth"]
    assert len(synthesized) == 1
    assert synthesized[0].question == "报销要提前申请吗？"


def test_预算截断(session: Session) -> None:
    """⑤ max_per_seed 截单种子变体数；max_total 截全局总量，超额种子保持 pending。"""
    doc = _make_doc(session, "差旅制度")
    chunk = _make_chunk(session, doc, "差旅报销正文。")
    seed_a = _make_seed(session, "差旅报销标准是什么？", chunk_id=chunk.id)
    llm = _StubLLM(_payload(["变体一", "变体二"], "答案"))

    summary = synthesize_from_seeds(session, _CLOCK, llm=llm, max_per_seed=1, max_total=10)
    assert summary["synthesized"] == 1  # max_per_seed 截单种子变体数
    session.expire_all()
    assert session.get(HardcaseSeed, seed_a.id).status == "synthesized"

    # 两个新 pending 种子 + 全局总量 1：首个种子出 1 题后预算耗尽，第二个保持 pending
    seed_c = _make_seed(session, "差旅补贴怎么算？", chunk_id=chunk.id)
    seed_d = _make_seed(session, "差旅审批找谁？", chunk_id=chunk.id)
    llm2 = _StubLLM(_payload(["新变体一", "新变体二"], "答案"))
    summary2 = synthesize_from_seeds(session, _CLOCK, llm=llm2, max_per_seed=2, max_total=1)
    assert summary2["synthesized"] == 1
    session.expire_all()
    assert session.get(HardcaseSeed, seed_c.id).status == "synthesized"
    assert session.get(HardcaseSeed, seed_d.id).status == "pending"  # 总量闸截断保持 pending
