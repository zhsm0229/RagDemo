r"""点踩种子合成服务（03 票，评测集自动更新）：难例种子 → LLM 变体评测题。

口径与设计要点：
- 只处理 status='pending' 且引用可达的种子：citations_snapshot 的 doc 引用逐个回查
  chunks（停用/缺失块剔除），全部不可达 → 种子置 no_ground（待补语料，不出题）。
- grounding 红线（拒答红线在合成侧的镜像）：期望答案必须出自知识块原文——prompt
  强约束 + 无据返回空串约定，解析后答案非空校验；解析失败/无据/超长 → 种子保持
  pending（可重试），计 skipped，绝不带病出题。
- 产物：origin=hardcase_synth、tier=extended（自动题不进发版门禁口径）、qtype=factual、
  source_doc_id 回填首块所属文档（报告 Context Recall 指标依赖该列）、source_ref 标注
  来源消息（可溯源到提问详情）。
- 去重：与题库既有题 + 本批产物按 (dept_id, normalize_question) 归一去重——归一口径
  单一事实源在 hardcase_mine.normalize_question（采集与合成共用）。
- 预算：max_per_seed 截单种子变体数、max_total 截全局总量（超出即停，种子保持
  pending 留待下一轮）；LLM 走注入参数（测试缝，生产侧 build_llm(settings, 0.0)
  第二实例先例——与人工评测判分同款 temperature=0 口径）。
- LLM 输出 JSON 解析容忍 ``` 围栏（eval_judge._parse_verdict 同款口径）；单种子
  LLM 异常捕获计 skipped 不炸批次。
"""

import json
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.models import Chunk, Department, Document, EvalQuestion, HardcaseSeed
from app.rag.clean import clean_text
from app.services.hardcase_mine import normalize_question

_MAX_QUESTION_CHARS: int = 2000  # 与题库导入行级校验同口径（eval_service）
_MAX_ANSWER_CHARS: int = 10000  # 同上
_MAX_LLM_TOKENS: int = 1024  # 变体+答案 的输出预算（JSON 结构小文本）
_DOCGEN_MIN_CLEAN_CHARS: int = 100  # 短块闸：清洗后 <100 字不出题（质量红线 §10.3 同款）
_DOCGEN_ZERO_RECALL_DAYS: int = 30  # 零召回口径：last_hit_at 为空或早于 30 天（概览同款）

_PROMPT_SYSTEM: str = (
    "你是企业知识库的评测题生成助手。只输出一个 JSON 对象，"
    '形如 {"variants": ["问题1", "问题2"], "expected_answer": "答案"}，'
    "不输出任何其他文字、解释或代码围栏。"
)
_PROMPT_USER_TEMPLATE: str = (
    "请基于给定知识块，为原问题生成 {count} 个评测变体问题，并给出期望答案。\n"
    "要求：\n"
    "1. 变体保持与原问题相同的问意与考察点，变换句式、称谓或提问粒度；\n"
    "2. expected_answer 必须逐字出自知识块原文（可拼接多处原文，不得自行编造或补充）——"
    "知识块不足以支撑答案时，expected_answer 返回空字符串；\n"
    "3. 变体问题不得出现知识块之外的专有名词。\n\n"
    "原问题：{question}\n\n知识块：\n{context}"
)
_DOCGEN_PROMPT_SYSTEM: str = (
    "你是企业知识库的评测题生成助手。只输出一个 JSON 对象，"
    '形如 {"question": "问题", "answer": "答案"}，'
    "不输出任何其他文字、解释或代码围栏。"
)
_DOCGEN_PROMPT_USER_TEMPLATE: str = (
    "请基于给定知识块出一道评测问答题（考察该块的核心事实）。\n"
    "要求：\n"
    "1. question 是员工会真实问出的问题（口语化、不带文档标题词）；\n"
    "2. answer 必须逐字出自知识块原文（可拼接多处原文，不得自行编造或补充）——"
    "知识块不足以支撑答案时，question 与 answer 均返回空字符串；\n\n"
    "知识块标题路径：{title_path}\n知识块正文：\n{context}"
)


def _extract_json(text: str) -> dict[str, Any] | None:
    """解析 LLM 输出 JSON：容忍 ```json 围栏（eval_judge 同款口径），非 dict 即失败。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _grounding_chunks(session: Session, seed: HardcaseSeed) -> list[Chunk]:
    """种子引用指向的活跃知识块（doc 引用回查；停用/缺失块剔除）。"""
    citations = seed.citations_snapshot or []
    chunk_ids = [
        int(item["refId"])
        for item in citations
        if isinstance(item, dict) and item.get("refType") == "doc" and item.get("refId") is not None
    ]
    if not chunk_ids:
        return []
    stmt = select(Chunk).where(Chunk.id.in_(chunk_ids), Chunk.is_active.is_(True))
    return list(session.scalars(stmt).all())


def synthesize_from_seeds(
    session: Session,
    clock: Clock,
    *,
    llm: Any,
    max_per_seed: int,
    max_total: int,
) -> dict[str, int]:
    """合成任务体（手动端点/周任务共用入口）：pending 种子 → 变体题入扩充集。

    返回 summary：seedsProcessed（产出变体的种子数）/ synthesized（新增题数）/
    noGround（引用缺失转 no_ground）/ skipped（解析失败或全变体无效，保持 pending）。
    """
    now = clock.now()
    seen: set[tuple[int, str]] = {
        (question.dept_id, normalize_question(question.question))
        for question in session.scalars(select(EvalQuestion)).all()
    }
    pending = list(
        session.scalars(
            select(HardcaseSeed).where(HardcaseSeed.status == "pending").order_by(HardcaseSeed.id)
        ).all()
    )
    summary: dict[str, int] = {"seedsProcessed": 0, "synthesized": 0, "noGround": 0, "skipped": 0}

    for seed in pending:
        if summary["synthesized"] >= max_total:
            break  # 全局预算耗尽：剩余种子保持 pending 留待下一轮
        chunks = _grounding_chunks(session, seed)
        if not chunks:
            seed.status = "no_ground"
            summary["noGround"] += 1
            continue
        context = "\n\n".join(f"{chunk.title_path}\n{chunk.content}" for chunk in chunks)
        messages = [
            {"role": "system", "content": _PROMPT_SYSTEM},
            {
                "role": "user",
                "content": _PROMPT_USER_TEMPLATE.format(
                    question=seed.question_text, context=context, count=max_per_seed
                ),
            },
        ]
        try:
            raw = "".join(llm.chat_stream(messages, _MAX_LLM_TOKENS))
        except Exception:  # noqa: BLE001 — 上游故障种子保持 pending，不炸批次
            summary["skipped"] += 1
            continue
        data = _extract_json(raw)
        variants = data.get("variants") if isinstance(data, dict) else None
        answer = data.get("expected_answer") if isinstance(data, dict) else None
        if not isinstance(variants, list) or not isinstance(answer, str) or not answer.strip():
            summary["skipped"] += 1
            continue
        answer = answer.strip()
        if len(answer) > _MAX_ANSWER_CHARS:
            summary["skipped"] += 1
            continue

        source_doc_id = chunks[0].document_id
        added_for_seed = 0
        for variant in variants:
            if added_for_seed >= max_per_seed or summary["synthesized"] >= max_total:
                break
            if not isinstance(variant, str):
                continue
            variant = variant.strip()
            if not variant or len(variant) > _MAX_QUESTION_CHARS:
                continue
            key = (seed.dept_id, normalize_question(variant))
            if key in seen:
                continue
            seen.add(key)
            session.add(
                EvalQuestion(
                    dept_id=seed.dept_id,
                    question=variant,
                    expected_answer=answer,
                    source_ref=f"难例合成·消息{seed.message_id}",
                    source_doc_id=source_doc_id,
                    origin="hardcase_synth",
                    tier="extended",
                )
            )
            summary["synthesized"] += 1
            added_for_seed += 1

        if added_for_seed:
            seed.status = "synthesized"
            seed.synth_count = (seed.synth_count or 0) + added_for_seed
            seed.last_synth_at = now
            summary["seedsProcessed"] += 1
        else:
            summary["skipped"] += 1  # 全部变体被去重/截断 → 保持 pending 可重试
    session.commit()
    return summary


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
        data = _extract_json(raw)
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
