r"""no_ground 种子闭环测试（商用就绪工单 16）：重试上限 + 语料更新后重扫。

口径：
- 重试上限：pending 种子合成失败（解析失败/全变体无效）累计 skip_count，超 3 次
  转 no_ground——病态种子不再每周空耗 LLM 预算；预算耗尽的 break 不是种子问题，
  不累计。
- 重扫：no_ground 且**有块引用**的种子（skip 累计型/块曾停用型）重置 skip_count
  并转回 pending（合成通道重新走 grounding 判定）；无引用种子（拒答/qa 直答被踩）
  保持 no_ground——它们等的是知识缺口补语料，重扫无判据，转回只会被立即打回。
- 文档入库成功（ingest 编排 ready 点）按 doc.dept_id 触发重扫——补完语料自动获得
  重新出题机会（挂钩一行；rescan 语义由本文件用例钉死，ingest 回归由既有套件兜底）。
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import FakeClock
from app.db.models import AuditLog, Chunk, Department, Document, HardcaseSeed, User
from app.services.hardcase_mine import rescan_no_ground

_CLOCK = FakeClock("2026-10-31T04:00:00+00:00")


def _admin_id(session: Session) -> int:
    return session.scalar(select(User.id).where(User.username == "admin"))


def _dept_id(session: Session, code: str = "service") -> int:
    return session.scalar(select(Department.id).where(Department.code == code))


def _embed() -> list[float]:
    from app.core.llm import StubEmbedder

    return StubEmbedder().embed(["差旅报销"])[0]


def _real_chunk(session: Session) -> int:
    """真实活跃块 id（grounding 回查可达——合成通道需要真块才走 skip 分支）。"""
    doc = Document(
        title="重扫语料",
        dept_id=_dept_id(session),
        original_filename="a.docx",
        file_path="seed/a.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=f"rescan-{_dept_id(session)}",
        effective_date=datetime(2026, 10, 1).date(),
        owner_name="责任人",
        status="ready",
        is_active=True,
        uploaded_by=_admin_id(session),
    )
    session.add(doc)
    session.flush()
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content="差旅报销：交通费与住宿费凭票据实报销，十个工作日内提交。",
        content_chars=30,
        position=0,
        embedding=_embed(),
    )
    session.add(chunk)
    session.flush()
    return chunk.id


def _make_seed(
    session: Session,
    question: str,
    *,
    status: str = "pending",
    skip_count: int = 0,
    citations: list | None = None,
    chunk_id: int | None = None,
) -> HardcaseSeed:
    if citations is None and chunk_id is not None:
        citations = [{"refType": "doc", "refId": chunk_id}]
    seed = HardcaseSeed(
        message_id=1,
        message_created_at=datetime(2026, 10, 30, tzinfo=UTC),
        source_type="feedback_down",
        dept_id=_dept_id(session),
        question_text=question,
        normalized_question=question,
        answer_snapshot="答案快照",
        citations_snapshot=citations if citations is not None else [{"refType": "doc", "refId": 1}],
        status=status,
        skip_count=skip_count,
    )
    session.add(seed)
    session.flush()
    return seed


def test_合成失败累计skip_count超限转no_ground(session: Session) -> None:
    """解析失败累计 3 次 → 第 3 次后转 no_ground（不再每周重试）；预算耗尽的种子
    不累计（保持 pending 留待下一轮）。"""
    from app.services.eval_synth import synthesize_from_seeds

    class _Bad:
        def chat_stream(self, messages, max_tokens):
            yield "不是 JSON"

    chunk_id = _real_chunk(session)
    seeds = [
        _make_seed(session, f"病态问题{i}", skip_count=i, chunk_id=chunk_id) for i in range(2)
    ]  # skip_count=0 与 1 两颗：第一次合成后分别 1、2（未超限）；第二次后 2、3（超限转）
    summary1 = synthesize_from_seeds(session, _CLOCK, llm=_Bad(), max_per_seed=2, max_total=100)
    assert summary1["skipped"] == 2
    assert all(s.status == "pending" for s in seeds)

    summary2 = synthesize_from_seeds(session, _CLOCK, llm=_Bad(), max_per_seed=2, max_total=100)
    assert summary2["skipped"] == 1 and summary2["noGround"] == 1
    session.expire_all()
    states = {s.question_text: (s.status, s.skip_count) for s in seeds}
    assert states["病态问题0"] == ("pending", 2)  # 未达上限
    assert states["病态问题1"] == ("no_ground", 3)  # 达 3 次上限转 no_ground


def test_预算耗尽不累计skip_count(session: Session) -> None:
    """max_total=0：循环入口即预算耗尽，种子保持 pending 且 skip_count 不变（预算
    是运营约束不是种子缺陷）。"""
    from app.services.eval_synth import synthesize_from_seeds

    seed = _make_seed(session, "预算耗尽问题", skip_count=2)

    class _Bad:
        def chat_stream(self, messages, max_tokens):  # pragma: no cover - 不会被调用
            raise AssertionError("预算耗尽不应触达 LLM")

    synthesize_from_seeds(session, _CLOCK, llm=_Bad(), max_per_seed=2, max_total=0)
    session.expire_all()
    assert seed.status == "pending"
    assert seed.skip_count == 2


def test_重扫只转回有引用种子(session: Session) -> None:
    """rescan_no_ground：有引用的 no_ground（skip 累计型）→ pending + skip_count=0；
    无引用（refuse 采集形态 citations=None）保持 no_ground。返回转回条数。"""
    cited = _make_seed(session, "有引用的待补种子", status="no_ground", skip_count=3)
    _make_seed(session, "无引用的拒答种子", status="no_ground", citations=None)

    rescanned = rescan_no_ground(session)

    assert rescanned == 1
    session.expire_all()
    assert cited.status == "pending"
    assert cited.skip_count == 0


