r"""难例采集入池集成测试（01 票，真 pg + 显式时间戳）：collect_hardcase_seeds 的
采集口径、跳过规则与幂等性。

用例：
① 拒答问题入池 → status=no_ground（无引用待补语料）；
② 点踩回答入池 → status=pending（带引用可合成），回答/引用快照落列；
③ 跳过规则：badcase_status='已忽略' 不入池；is_eval=true 的问答不入池；
   中断占位回答（is_active=false）不算拒答；
④ 归一去重：全角/空白差异的同一问题只入池一条；同问题跨通道（点踩优先于拒答，
   因带引用更有合成价值）只入池一条；
⑤ 幂等：二次采集返回 0，池内行不变。

直插消息行（绕开问答链路），created_at 落 2026_10 分区；任务体为纯函数手动调用，
cron 装配在 05 票。
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.db.models import Conversation, HardcaseSeed, Message, User
from app.services.hardcase_mine import collect_hardcase_seeds, normalize_question

_T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)  # 2026-10-15：落 2026_10 分区


def _admin_id(session: Session) -> int:
    return session.scalar(select(User.id).where(User.username == "admin"))


def _dept_id(session: Session) -> int:
    return session.scalar(select(User.dept_id).where(User.username == "admin"))


def _make_conversation(session: Session, title: str) -> Conversation:
    conv = Conversation(
        user_id=_admin_id(session),
        dept_id=_dept_id(session),
        title=title,
    )
    session.add(conv)
    session.flush()
    return conv


def _make_message(
    session: Session,
    conv: Conversation,
    *,
    role: str,
    content: str,
    created_at: datetime,
    is_eval: bool = False,
    is_active: bool = True,
    answer_type: str | None = None,
    feedback: str | None = None,
    badcase_status: str | None = None,
    feedback_category: str | None = None,
    citations: Any | None = None,
) -> Message:
    """直插消息行（保留策略测试同款）：采集口径只关心 role/answer_type/feedback/
    badcase_status/is_eval/is_active 与内容快照。"""
    msg = Message(
        conversation_id=conv.id,
        user_id=conv.user_id,
        dept_id=conv.dept_id,
        role=role,
        content=content,
        is_eval=is_eval,
        is_active=is_active,
        answer_type=answer_type,
        feedback=feedback,
        badcase_status=badcase_status,
        feedback_category=feedback_category,
        citations=citations,
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(msg)
    session.flush()
    return msg


def _seed_rows(session: Session) -> list[HardcaseSeed]:
    return list(session.scalars(select(HardcaseSeed).order_by(HardcaseSeed.id)).all())


def test_拒答问题入池为_no_ground(session: Session) -> None:
    """① 拒答问答对：问题入池，source_type=refuse、status=no_ground（无引用待补语料），
    问题原文快照与来源消息 id 逐字读回。"""
    conv = _make_conversation(session, "拒答采集")
    question = _make_message(
        session, conv, role="user", content="蓝鲸值班的夜班补贴是多少？", created_at=_T0
    )
    _make_message(
        session,
        conv,
        role="assistant",
        content="知识库中未找到相关内容",
        created_at=_T0,
        answer_type="refuse",
    )

    collected = collect_hardcase_seeds(session, FakeClock("2026-10-20T04:00:00+00:00"))

    assert collected == 1
    rows = _seed_rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.source_type == "refuse"
    assert row.status == "no_ground"
    assert row.question_text == "蓝鲸值班的夜班补贴是多少？"
    assert row.normalized_question == normalize_question("蓝鲸值班的夜班补贴是多少？")
    assert row.message_id == question.id
    assert row.message_created_at == _T0
    assert row.answer_snapshot is None
    assert row.citations_snapshot is None
    assert row.dept_id == _dept_id(session)
    # 注入时钟贯穿行时间戳（与审计同口径）
    assert row.created_at == datetime(2026, 10, 20, 4, 0, tzinfo=UTC)


def test_点踩回答入池为_pending并带快照(session: Session) -> None:
    """② 点踩回答：问题入池 status=pending（带引用可合成），回答原文与引用快照落列。"""
    conv = _make_conversation(session, "点踩采集")
    citations: list[dict[str, Any]] = [
        {"n": 1, "refType": "doc", "refId": 123, "title": "值班手册"}
    ]
    _make_message(session, conv, role="user", content="报销标准是什么？", created_at=_T0)
    _make_message(
        session,
        conv,
        role="assistant",
        content="报销标准是每人每月 200 元。",
        created_at=_T0,
        answer_type="rag",
        feedback="down",
        badcase_status="待处理",
        feedback_category="答案错误",
        citations=citations,
    )

    collected = collect_hardcase_seeds(session, FakeClock("2026-10-20T04:00:00+00:00"))

    assert collected == 1
    row = _seed_rows(session)[0]
    assert row.source_type == "feedback_down"
    assert row.status == "pending"
    assert row.question_text == "报销标准是什么？"
    assert row.answer_snapshot == "报销标准是每人每月 200 元。"
    assert row.citations_snapshot == citations


def test_已忽略与评测消息与占位回答不入池(session: Session) -> None:
    """③ 跳过规则：badcase 已忽略（人工治理终态）不入池；is_eval 问答不入池；
    中断占位回答（is_active=false）不算拒答。三条全跳过 → 返回 0 且池空。"""
    conv1 = _make_conversation(session, "已忽略")
    _make_message(session, conv1, role="user", content="已忽略的问题", created_at=_T0)
    _make_message(
        session,
        conv1,
        role="assistant",
        content="已忽略的回答",
        created_at=_T0,
        answer_type="rag",
        feedback="down",
        badcase_status="已忽略",
    )
    conv2 = _make_conversation(session, "评测会话")
    _make_message(session, conv2, role="user", content="评测拒答问题", created_at=_T0, is_eval=True)
    _make_message(
        session,
        conv2,
        role="assistant",
        content="知识库中未找到相关内容",
        created_at=_T0,
        is_eval=True,
        answer_type="refuse",
    )
    conv3 = _make_conversation(session, "中断会话")
    _make_message(session, conv3, role="user", content="中断的问题", created_at=_T0)
    _make_message(
        session,
        conv3,
        role="assistant",
        content="占位行",
        created_at=_T0,
        is_active=False,
        answer_type="refuse",
    )

    collected = collect_hardcase_seeds(session, FakeClock("2026-10-20T04:00:00+00:00"))

    assert collected == 0
    assert _seed_rows(session) == []


def test_归一去重与跨通道优先点踩(session: Session) -> None:
    """④ 全角空白/首尾空白差异的同一问题只入池一条；同问题既被点踩又被拒答时，
    点踩通道优先（带引用更有合成价值，status=pending）。"""
    conv_a = _make_conversation(session, "点踩通道")
    _make_message(session, conv_a, role="user", content="报销标准是什么？", created_at=_T0)
    _make_message(
        session,
        conv_a,
        role="assistant",
        content="回答 A",
        created_at=_T0,
        answer_type="rag",
        feedback="down",
        citations=[{"n": 1, "refType": "doc", "refId": 1}],
    )
    conv_b = _make_conversation(session, "拒答通道全角变体")
    _make_message(session, conv_b, role="user", content="　报销标准是什么？　", created_at=_T0)
    _make_message(
        session,
        conv_b,
        role="assistant",
        content="知识库中未找到相关内容",
        created_at=_T0,
        answer_type="refuse",
    )

    collected = collect_hardcase_seeds(session, FakeClock("2026-10-20T04:00:00+00:00"))

    assert collected == 1
    rows = _seed_rows(session)
    assert len(rows) == 1
    assert rows[0].source_type == "feedback_down"
    assert rows[0].status == "pending"


def test_二次采集幂等(session: Session) -> None:
    """⑤ 同一池子重复采集：第二次返回 0，行数与内容不变（UNIQUE 约束兜底）。"""
    conv = _make_conversation(session, "幂等")
    _make_message(session, conv, role="user", content="蓝鲸值班补贴是多少", created_at=_T0)
    _make_message(
        session,
        conv,
        role="assistant",
        content="知识库中未找到相关内容",
        created_at=_T0,
        answer_type="refuse",
    )
    clock = FakeClock("2026-10-20T04:00:00+00:00")

    first = collect_hardcase_seeds(session, clock)
    second = collect_hardcase_seeds(session, clock)

    assert first == 1
    assert second == 0
    assert len(_seed_rows(session)) == 1


def test_无引用点踩落no_ground(session: Session) -> None:
    """补充：点踩但无引用（如 qa 直答被踩）无法合成 → no_ground 而非 pending。"""
    conv = _make_conversation(session, "无引用点踩")
    _make_message(session, conv, role="user", content="年假有几天", created_at=_T0)
    _make_message(
        session,
        conv,
        role="assistant",
        content="年假 5 天",
        created_at=_T0,
        answer_type="qa",
        feedback="down",
        citations=None,
    )

    assert collect_hardcase_seeds(session, FakeClock("2026-10-20T04:00:00+00:00")) == 1
    assert _seed_rows(session)[0].status == "no_ground"
