r"""点踩种子合成（03 票拆分，工单 14）：难例种子 → LLM 变体评测题。

口径与设计要点（自 eval_synth 拆出，语义零变化——既有测试 test_eval_synth 为回归钉）：
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
  pending 留待下一轮）；单种子 LLM 异常捕获计 skipped 不炸批次。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.db.models import Chunk, EvalQuestion, HardcaseSeed

_SEED_SKIP_LIMIT: int = 3  # pending 种子连续失败上限（超过转 no_ground，工单 16）

from app.services.eval_synth_common import (
    _MAX_ANSWER_CHARS,
    _MAX_LLM_TOKENS,
    _MAX_QUESTION_CHARS,
    _PROMPT_SYSTEM,
    _PROMPT_USER_TEMPLATE,
    extract_json,
)
from app.services.hardcase_mine import normalize_question


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


def _register_skip(seed: HardcaseSeed, summary: dict[str, int]) -> None:
    """合成失败计数（工单 16）：skip_count 累计，达上限转 no_ground（summary 计
    noGround——转待补语料语义；病态种子不再每周重试空耗 LLM 预算）；引用缺失
    转移（grounding 判定）不经过此路径、不累计。"""
    seed.skip_count = (seed.skip_count or 0) + 1
    if seed.skip_count >= _SEED_SKIP_LIMIT:
        seed.status = "no_ground"
        summary["noGround"] += 1
    else:
        summary["skipped"] += 1


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
            _register_skip(seed, summary)
            continue
        data = extract_json(raw)
        variants = data.get("variants") if isinstance(data, dict) else None
        answer = data.get("expected_answer") if isinstance(data, dict) else None
        if not isinstance(variants, list) or not isinstance(answer, str) or not answer.strip():
            _register_skip(seed, summary)
            continue
        answer = answer.strip()
        if len(answer) > _MAX_ANSWER_CHARS:
            _register_skip(seed, summary)
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
            _register_skip(seed, summary)  # 全部变体被去重/截断 → 计失败（超限转 no_ground）
    session.commit()
    return summary
