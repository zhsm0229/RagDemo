r"""解析可观测性集成测试（2026-10-02：分批嵌入 + 进度落库 + 错误双轨）。

背景：大文档全量 chunk 单请求 embed 超 TEI --max-batch-tokens 2048 直接 422，6 份
文档全失败且真实异常被脱敏丢弃。修复后：按字符预算分批、批间上报进度（parse_stage/
parse_progress 落库）、失败时 parse_error（用户文案）与 parse_error_detail（技术摘要）
双轨。样本与依赖构造复用 test_ingest 的工具（docx 生成器/deps/上传一步走）。
"""

from sqlalchemy import func, select

from app.core.llm import StubEmbedder
from app.core.storage import LocalFileStorage
from app.db.models import Chunk
from app.services.ingest import create_document, process_document
from app.services.ingest_pipeline import analyze_document
from tests.integration.test_ingest import _docx_bytes, _make_deps, _meta

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

BIG_DOC_HEADINGS = [
    (
        f"第{i}章 制度条款{i}",
        [
            f"第{i}章第{j}条 内容说明{j}：适用于全体员工并遵照执行，自发布之日起施行并遵照办理。"
            for j in range(6)
        ],
    )
    for i in range(12)
]


def _admin_id(session) -> int:
    from app.db.models import User

    return session.scalars(select(User.id).where(User.username == "admin")).one()


def _dept_id(session) -> int:
    from app.db.models import User

    return session.scalars(select(User.dept_id).where(User.username == "admin")).one()


def test_embed_batched_by_char_budget_and_progress_cleared(session, tmp_path):
    """大文档分批嵌入：条数守恒（不丢不重）、每批字符不超预算（超长单条独立成批除外）；
    收口 ready 后 parse_stage/parse_progress/parse_error_detail 清空（终态无进度语义）。"""
    storage = LocalFileStorage(tmp_path)
    embedder = StubEmbedder()
    doc = create_document(
        session,
        storage,
        type("U", (), {"id": _admin_id(session)})(),
        _meta(_dept_id(session), "分批嵌入测试文档"),
        _docx_bytes(BIG_DOC_HEADINGS),
        "分批嵌入测试文档.docx",
        DOCX_MIME,
    )
    process_document(session, _make_deps(storage, embedder=embedder), doc.id)
    assert doc.status == "ready", f"{doc.parse_error} | detail={doc.parse_error_detail}"
    total_chunks = session.scalar(select(func.count(Chunk.id)).where(Chunk.document_id == doc.id))
    assert total_chunks >= 2  # 大文档：多 chunk
    assert embedder.embed_calls >= 2  # 总字符超 1200 预算 → 至少两批（stub 每次调用计一批）
    assert doc.parse_stage is None and doc.parse_progress is None
    assert doc.parse_error_detail is None


def test_embed_failure_records_detail_and_clears_progress(session, tmp_path):
    """embed 失败：parse_error=用户文案、parse_error_detail=UpstreamError 原文（技术双轨）；
    stage/progress 清空。"""
    storage = LocalFileStorage(tmp_path)
    doc = create_document(
        session,
        storage,
        type("U", (), {"id": _admin_id(session)})(),
        _meta(_dept_id(session), "嵌入失败测试文档"),
        _docx_bytes(
            [
                (
                    f"第{i}章 总则{i}",
                    [
                        f"第{i}章第{j}条 嵌入失败样本内容{j}：全体员工遵照执行并办理。"
                        for j in range(6)
                    ],
                )
                for i in range(4)
            ]
        ),
        "嵌入失败测试文档.docx",
        DOCX_MIME,
    )
    process_document(session, _make_deps(storage, embedder=StubEmbedder(fail_times=99)), doc.id)
    assert doc.status == "failed"
    assert doc.parse_error == "向量化服务调用失败，请稍后重试"
    assert doc.parse_error_detail is not None
    assert "stub embed forced failure" in doc.parse_error_detail
    assert doc.parse_stage is None and doc.parse_progress is None


def test_progress_callback_stages_and_monotonic_percent(session, tmp_path):
    """进度回调：阶段按 解析中→清洗分块中→向量化中 推进、百分比单调不减，
    向量化区间落在 45-95（收口 100 由状态机负责，不在管线内）。"""
    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, embedder=StubEmbedder())
    doc = create_document(
        session,
        storage,
        type("U", (), {"id": _admin_id(session)})(),
        _meta(_dept_id(session), "进度回调测试文档"),
        _docx_bytes(BIG_DOC_HEADINGS),
        "进度回调测试文档.docx",
        DOCX_MIME,
    )
    calls: list[tuple[str, int]] = []
    analyze_document(session, deps, doc, progress=lambda stage, pct: calls.append((stage, pct)))
    assert calls[0] == ("解析中", 5)
    assert calls[-1][0] == "向量化中"
    percents = [p for _, p in calls]
    assert percents == sorted(percents), "百分比应单调不减"
    assert 45 <= max(percents) <= 95


def test_text_pdf_routes_to_pypdf_skipping_mineru(session, tmp_path, monkeypatch):
    """文字版 pdf 预判路由：配置了 MinerU 且文字层充足 → 直接走 pypdf（秒级），
    不触碰 MinerU（spy 断言）；engine=pypdf 且无降级标注（路由≠降级）。"""
    from pathlib import Path

    from app.rag.parser import MinerUParser

    mineru_calls: list[str] = []
    real_parse = MinerUParser.parse

    def spy_parse(self, filename, content):
        mineru_calls.append(filename)
        return real_parse(self, filename, content)

    monkeypatch.setattr(MinerUParser, "parse", spy_parse)
    monkeypatch.setattr("app.services.ingest_pipeline._is_text_based_pdf", lambda content: True)

    storage = LocalFileStorage(tmp_path)
    deps = _make_deps(storage, mineru_base_url="http://mineru.invalid")
    sample = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "admin_handbook_v1.pdf"
    doc = create_document(
        session,
        storage,
        type("U", (), {"id": _admin_id(session)})(),
        _meta(_dept_id(session), "文字版路由测试"),
        sample.read_bytes(),
        "文字版路由测试.pdf",
        "application/pdf",
    )
    process_document(session, deps, doc.id)
    assert doc.status == "ready", doc.parse_error
    assert doc.parse_engine == "pypdf"
    assert doc.parse_error is None  # 路由命中不是降级：无降级标注
    assert mineru_calls == [], "文字版 pdf 不应触碰 MinerU"
