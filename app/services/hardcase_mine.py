r"""难例采集服务（评测集自动更新 01/05 票）：真实拒答/点踩问题 → 难例种子池。

口径与设计要点：
- 两条采集通道，点踩优先：feedback_down（带回答/引用快照，可直接合成）先扫，
  refuse（拒答）后扫；同一归一问题跨通道只入池一条——先到先得使带引用的点踩
  种子天然胜出（合成价值高于无引用的拒答种子）。
- 拒答配对与知识缺口周榜同源（最小 id 配对 + 回答活跃位判定，参照
  services.knowledge_gap._refuse_pair），复制而非 import 私有函数——配对语义是
  运营报表与采集共用的口径钉子，实现漂移必须在两处测试同时变红（golden 回归
  同款纪律）。查询走实体 scalars() 路径（无裸 execute），全文件无 f-string SQL，
  红线「SQL 一律参数绑定」达成。
- 点踩通道两步查询（先取点踩回答、再逐条取其前驱提问）：试点量级（点踩数十行）
  N+1 成本可忽略，换取代码直白；题量增长后可与拒答通道统一为 LATERAL。
- 全量扫 + UNIQUE(dept_id, normalized_question) 兜底：不漏采与天然幂等；窗口
  切分（周游标）列为题量增长后的演进项。
- 跳过：is_eval=true（评测数据不进运营/素材池，W3 口径）、badcase_status='已忽略'
  （人工治理终态不被采集推翻——与 badcase 治理队列的边界，见 09 票文档收口）。
- 状态落位：feedback_down 带 citations → pending（可合成）；feedback_down 无
  citations（qa 直答被踩等）与全部 refuse → no_ground（待补语料清单）。
- 时间戳走注入时钟（W1 审计同口径，测试确定性）；归一函数 normalize_question
  是采集与合成共用的唯一去重口径（纯函数，单测钉死）。
"""

import re
import unicodedata
from datetime import datetime
from typing import Any

from sqlalchemy import select, true
from sqlalchemy.orm import Session, aliased

from app.core.clock import Clock
from app.db.models import HardcaseSeed, Message

# 口径常量（与 errors.py 常量惯例同款：禁止魔法值散落）
_ANSWER_TYPE_REFUSE: str = "refuse"  # 拒答回答的 answer_type
_FEEDBACK_DOWN: str = "down"  # 点踩反馈值
_BADCASE_IGNORED: str = "已忽略"  # badcase 人工治理终态（采集不推翻）
_CITATION_KEY = "refId"  # citations 元素含 refId 即视为带引用（qa 直答无块引用）


def normalize_question(raw: str) -> str:
    """问题归一（采集与合成共用的去重口径）：NFKC 全角转半角 + 连续空白折叠 +
    首尾修剪。只做形态规整不改写表达——中文与中文标点原样保留。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()


def _refuse_questions(session: Session) -> list[Message]:
    """拒答通道：配对回答为「活跃拒答」的用户问题实体。

    配对口径与知识缺口周榜同源（最小 id 配对 + 回答活跃位判定）：不筛回答
    is_active——中断占位行占住次序，其活跃位在 join 条件判定，而非错误地滑向
    下一条回答。"""
    answer = aliased(Message)
    pair = (
        select(
            answer.is_active.label("answer_active"),
            answer.answer_type.label("answer_type"),
        )
        .where(
            answer.conversation_id == Message.conversation_id,
            answer.role == "assistant",
            answer.is_eval.is_(False),
            answer.id > Message.id,
        )
        .order_by(answer.id.asc())
        .limit(1)
        .lateral(name="refuse_pair")
    )
    stmt = (
        select(Message)
        .select_from(Message)
        .join(pair, true())
        .where(
            Message.role == "user",
            Message.is_eval.is_(False),
            pair.c.answer_active.is_(True),
            pair.c.answer_type == _ANSWER_TYPE_REFUSE,
        )
        .order_by(Message.id.asc())
    )
    return list(session.scalars(stmt).all())


def _question_preceding(session: Session, answer: Message) -> Message | None:
    """回答行的前驱提问：同会话 id 小于回答行的最大 user 行（问答次序即 id 序）。"""
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == answer.conversation_id,
            Message.role == "user",
            Message.id < answer.id,
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _feedback_answers(session: Session) -> list[Message]:
    """点踩通道：被点踩的活跃回答实体（已忽略终态不采集）。"""
    stmt = (
        select(Message)
        .where(
            Message.role == "assistant",
            Message.is_eval.is_(False),
            Message.is_active.is_(True),
            Message.feedback == _FEEDBACK_DOWN,
            # 已忽略=人工治理终态，采集不推翻；NULL/待处理/已修语料均可入池
            Message.badcase_status.is_distinct_from(_BADCASE_IGNORED),
        )
        .order_by(Message.id.asc())
    )
    return list(session.scalars(stmt).all())


def _has_citations(citations: Any) -> bool:
    """引用快照非空且至少一个元素带 refId（qa 直答引用为空/无块引用 → 不可合成）。"""
    return bool(citations) and any(_CITATION_KEY in item for item in citations)


def _existing_keys(session: Session) -> set[tuple[int, str]]:
    """池内已有 (dept_id, normalized_question) 键集（幂等预载，池小全载实体）。"""
    return {
        (seed.dept_id, seed.normalized_question)
        for seed in session.scalars(select(HardcaseSeed)).all()
    }


def collect_hardcase_seeds(session: Session, clock: Clock) -> int:
    """采集任务体（cron/runbook/测试共用入口）：拒答+点踩 → 难例种子池。

    全量扫（幂等由 UNIQUE(dept_id, normalized_question) 与池内预载键双保险），
    返回本次新增行数；点踩优先于拒答（模块 docstring「两条采集通道」）。
    """
    now = clock.now()
    seen = _existing_keys(session)
    added = 0

    def _add(
        *,
        message_id: int,
        message_created_at: datetime,
        source_type: str,
        dept_id: int,
        question_text: str,
        answer_snapshot: str | None,
        citations: Any,
        status: str,
    ) -> None:
        nonlocal added
        key = (dept_id, normalize_question(question_text))
        if key in seen:
            return
        seen.add(key)
        session.add(
            HardcaseSeed(
                message_id=message_id,
                message_created_at=message_created_at,
                source_type=source_type,
                dept_id=dept_id,
                question_text=question_text,
                normalized_question=key[1],
                answer_snapshot=answer_snapshot,
                citations_snapshot=citations,
                status=status,
                created_at=now,
                updated_at=now,
            )
        )
        added += 1

    for answer in _feedback_answers(session):
        question = _question_preceding(session, answer)
        if question is None:
            continue  # 无前驱提问的回答（历史脏数据）不入池
        _add(
            message_id=answer.id,
            message_created_at=answer.created_at,
            source_type="feedback_down",
            dept_id=answer.dept_id,
            question_text=question.content,
            answer_snapshot=answer.content,
            citations=answer.citations,
            status="pending" if _has_citations(answer.citations) else "no_ground",
        )
    for question in _refuse_questions(session):
        _add(
            message_id=question.id,
            message_created_at=question.created_at,
            source_type="refuse",
            dept_id=question.dept_id,
            question_text=question.content,
            answer_snapshot=None,
            citations=None,
            status="no_ground",
        )
    session.commit()
    return added


def rescan_no_ground(session: Session, dept_id: int | None = None) -> int:
    """重扫 no_ground 种子（商用就绪工单 16）：有块引用的种子重置失败计数并转回
    pending——合成通道重新走 grounding 判定（块恢复/补语料后重新获得出题机会）；
    无引用种子（拒答/qa 直答被踩）保持 no_ground——它们等的是知识缺口补语料，
    重扫无判据，转回只会被立即打回（noGround 计数空转）。返回转回条数。

    触发点：文档入库成功（ingest 编排 ready 点，按 doc.dept_id）与管理端手动重扫
    （审计 hardcase.rescan）。UNIQUE(dept_id, normalized_question) 幂等不变。
    """
    stmt = select(HardcaseSeed).where(
        HardcaseSeed.status == "no_ground",
        HardcaseSeed.skip_count > 0,  # 仅 skip 累计型（采集直接落 no_ground 的 skip_count=0 不动）
    )
    if dept_id is not None:
        stmt = stmt.where(HardcaseSeed.dept_id == dept_id)
    rescanned = 0
    for seed in session.scalars(stmt).all():
        if not _has_citations(seed.citations_snapshot):
            continue
        seed.status = "pending"
        seed.skip_count = 0
        rescanned += 1
    session.commit()
    return rescanned
