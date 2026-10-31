r"""文档造题通道（04 票拆分，工单 14）：抽样知识块 → LLM 正向评测题入扩充集。

口径（自 eval_synth 拆出，语义零变化——既有测试 test_doc_synth 为回归钉）：
- 零召回优先抽样：从未召回 > 超 30 天 > 近期（概览零召回榜同款口径）；只取
  ready+active 文档的活跃块；清洗后 <100 字剔除（短块不烧 LLM）；历史同块去重
  （source_ref 在册即剔除，LLM 调用前拦截）。
- grounding 红线同种子通道：答案必须逐字出自知识块原文，无据不出题。
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.models import Chunk, Department, Document, EvalQuestion
from app.rag.clean import clean_text
from app.services.eval_synth_common import (
    _DOCGEN_MIN_CLEAN_CHARS,
    _DOCGEN_PROMPT_SYSTEM,
    _DOCGEN_PROMPT_USER_TEMPLATE,
    _DOCGEN_ZERO_RECALL_DAYS,
    _MAX_ANSWER_CHARS,
    _MAX_LLM_TOKENS,
    _MAX_QUESTION_CHARS,
    extract_json,
)
from app.services.hardcase_mine import normalize_question


def _docgen_candidates(
    session: Session, clock: Clock, *, sample_per_dept: int
) -> tuple[list[Chunk], int]:
    """按部门抽样造题候选块：零召回优先（从未召回 > 超 30 天 > 近期），只取
    ready+active 文档的活跃块；清洗后 <100 字剔除（计入 skipped，短块不烧 LLM）；
    历史同块去重（source_ref 在册即剔除，LLM 调用前拦截）。返回 (样本块, 跳过数)。"""
    cutoff = clock.now() - timedelta(days=_DOCGEN_ZERO_RECALL_DAYS)
    used_refs = set(
        session.scalars(
            select(EvalQuestion.source_ref).where(EvalQuestion.origin == "doc_synth")
        ).all()
    )
    sampled: list[Chunk] = []
    skipped = 0
    for dept in session.scalars(select(Department).order_by(Department.id)).all():
        stmt = (
            select(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.dept_id == dept.id,
                Chunk.is_active.is_(True),
                Document.status == "ready",
                Document.is_active.is_(True),
            )
        )
        candidates = list(session.scalars(stmt).all())

        def _rank(chunk: Chunk) -> tuple[int, int]:
            if chunk.last_hit_at is None:
                return (0, chunk.id)  # 从未召回最优先
            if chunk.last_hit_at < cutoff:
                return (1, chunk.id)  # 超 30 天未召回次之
            return (2, chunk.id)  # 近期召回最后

        eligible: list[Chunk] = []
        for chunk in candidates:
            if len(clean_text(chunk.content).strip()) < _DOCGEN_MIN_CLEAN_CHARS:
                skipped += 1
                continue
            if any(ref is not None and ref.endswith(f"块{chunk.id}") for ref in used_refs):
                skipped += 1
                continue
            eligible.append(chunk)
        eligible.sort(key=_rank)
        sampled.extend(eligible[:sample_per_dept])
    return sampled, skipped


def synthesize_from_chunks(
    session: Session,
    clock: Clock,
    *,
    llm: Any,
    sample_per_dept: int,
    max_total: int,
) -> dict[str, int]:
    """文档造题任务体（手动端点/周任务共用入口）：抽样块 → 正向评测题入扩充集。

    返回 summary：chunksSampled（实际送 LLM 的块数）/ synthesized（新增题数）/
    skipped（短块/历史同块/解析失败等被跳过计数）。
    """
    seen: set[tuple[int, str]] = {
        (question.dept_id, normalize_question(question.question))
        for question in session.scalars(select(EvalQuestion)).all()
    }
    chunks, pre_skipped = _docgen_candidates(session, clock, sample_per_dept=sample_per_dept)
    summary: dict[str, int] = {
        "chunksSampled": len(chunks),
        "synthesized": 0,
        "skipped": pre_skipped,
    }
    for chunk in chunks:
        if summary["synthesized"] >= max_total:
            break  # 全局预算耗尽：剩余抽样块放弃（下一轮重新抽样）
        messages = [
            {"role": "system", "content": _DOCGEN_PROMPT_SYSTEM},
            {
                "role": "user",
                "content": _DOCGEN_PROMPT_USER_TEMPLATE.format(
                    title_path=chunk.title_path, context=chunk.content
                ),
            },
        ]
        try:
            raw = "".join(llm.chat_stream(messages, _MAX_LLM_TOKENS))
        except Exception:  # noqa: BLE001 — 上游故障该块放弃，不炸批次
            summary["skipped"] += 1
            continue
        data = extract_json(raw)
        question = data.get("question") if isinstance(data, dict) else None
        answer = data.get("answer") if isinstance(data, dict) else None
        if (
            not isinstance(question, str)
            or not question.strip()
            or not isinstance(answer, str)
            or not answer.strip()
        ):
            summary["skipped"] += 1  # 无据不出题（grounding 红线）
            continue
        question = question.strip()
        answer = answer.strip()
        if len(question) > _MAX_QUESTION_CHARS or len(answer) > _MAX_ANSWER_CHARS:
            summary["skipped"] += 1
            continue
        key = (chunk.dept_id, normalize_question(question))
        if key in seen:
            summary["skipped"] += 1
            continue
        seen.add(key)
        session.add(
            EvalQuestion(
                dept_id=chunk.dept_id,
                question=question,
                expected_answer=answer,
                source_ref=f"文档造题·块{chunk.id}",
                source_doc_id=chunk.document_id,
                origin="doc_synth",
                tier="extended",
            )
        )
        summary["synthesized"] += 1
    session.commit()
    return summary
