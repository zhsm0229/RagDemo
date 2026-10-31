r"""入库状态机集成测试：create/process/confirm/expire 编排（真 pg + StubEmbedder + FakeClock）。

brief 用例表 11 行全覆盖（DOC-01/06/10/11/12/13/20/26 + 并发）：每条先走同步段
create_document（check_sync→SHA-256 查重→落盘→登记 parsing→审计），再 process_document
异步段（解析→红线→清洗→乱码/敏感/simhash→分块→批量 embed→事务写 chunks+旧版归档）。
样本程序化构造 bytes：docx 段落需 ≥100 有效字符过质量红线（红线=max(100,原始×10%)）；
pdf 用本文件的最小构造器（长 ASCII 文本，同因）；含身份证样本用过校验码的真实格式号。
向量写入走 ORM：psycopg3 将 list[float] 适配后可直插 vector 列（已对 pg-test 实证）。
"""

import hashlib
import importlib.util
import io
import threading
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest
from docx import Document as DocxDocument
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.clock import Clock, FakeClock
from app.core.config import Settings
from app.core.errors import ERR_DUPLICATE_DOC, ERR_PARAM_INVALID, ApiError
from app.core.llm import StubEmbedder
from app.core.storage import LocalFileStorage
from app.db.models import AuditLog, Chunk, Document, User
from app.rag.chunker import split
from app.rag.parser import DocxParser
from app.services.ingest import (
    IngestDeps,
    confirm_document,
    create_document,
    expire_due_documents,
    process_document,
)
from app.services.ingest_pipeline import analyze_document

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"
TOOLS = Path(__file__).resolve().parents[1] / "fixtures" / "tools"
MINERU_DOWN = "http://127.0.0.1:9"  # 无监听端口：连接立即被拒，触发 UpstreamError


def _tool(name: str) -> ModuleType:
    """按文件路径加载 fixtures/tools 下的生成工具（无包结构，spec 加载，同 unit 层用法）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _filler(tag: str, lines: int) -> str:
    """长正文构造：每行唯一（含序号，避开页眉/页码清洗规则），行数×约34字；不含"第X条"
    字样——条款分型由专属用例覆盖，存量用例维持 general 档基线（两节 ≥1500 字过信号3）。"""
    return "\n".join(
        f"{tag}要点{i}：适用于全体员工并遵照执行，具体细则参见配套操作指引全文。"
        for i in range(lines)
    )


def _clause_text(no: int, sentences: int, per: int = 36) -> str:
    """单条制度条款：条号开头、句号收尾（len = 4 + sentences×per；4 句 = 148 字 ≥120 碎条线）。"""
    return f"第{no}条 " + "".join("款" * (per - 1) + "。" for _ in range(sentences))


def _docx_bytes(headings: list[tuple[str, list[str]]]) -> bytes:
    """构造单级标题 docx：每个标题节一段长正文（各自 ≥300 字成独立块）。"""
    document = DocxDocument()
    document.core_properties.title = "员工手册"
    for heading, paragraphs in headings:
        document.add_heading(heading, level=1)
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf_bytes(pages: list[str]) -> bytes:
    """最小多页 PDF（Helvetica/ASCII，结构同 fixtures/tools/make_sample_pdf，文本可参数化）。"""
    return _pdf_bytes_lines([[page] for page in pages])


def _pdf_bytes_lines(pages: list[list[str]]) -> bytes:
    """多行多页 PDF：每页多行文本（T* 换行指令，pypdf 按行提取为 \\n 分隔，可过行级清洗）。"""
    count = len(pages)
    kids = " ".join(f"{3 + i} 0 R" for i in range(count))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("ascii"),
    ]
    for i in range(count):  # 页对象在前：3..2+count，内容流紧随：3+count..2+2*count
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
            f"{3 + count + i} 0 R /Resources << /Font << /F1 {3 + 2 * count} 0 R >> >> >>".encode(
                "ascii"
            )
        )
    for lines in pages:
        parts = [f"BT /F1 12 Tf 14 TL 72 720 Td ({lines[0]}) Tj"]
        parts += [f"T* ({line}) Tj" for line in lines[1:]]
        stream = "\n".join([*parts, "ET"]).encode("ascii")
        objs.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode("ascii") + b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)


def _meta(dept_id: int, title: str, expiry: date | None = None) -> dict:
    return {
        "dept_id": dept_id,
        "title": title,
        "effective_date": date(2026, 9, 1),
        "expiry_date": expiry,
        "owner_name": "制度责任人",
    }


def _make_deps(
    storage: LocalFileStorage,
    embedder: StubEmbedder | None = None,
    clock: Clock | None = None,
    mineru_base_url: str = "",
    converter: object | None = None,
) -> IngestDeps:
    """构造可替身依赖包：parser_settings 的 storage_dir 与 LocalFileStorage 根对齐。"""
    return IngestDeps(
        parser_settings=Settings(
            jwt_secret="ingest-test",
            storage_dir=str(storage.root_dir),
            mineru_base_url=mineru_base_url,
        ),
        embedder=embedder if embedder is not None else StubEmbedder(),
        clock=clock if clock is not None else FakeClock(),
        storage=storage,
        converter=converter,
    )


def _upload(
    session: Session,
    storage: LocalFileStorage,
    meta: dict,
    content: bytes,
    filename: str,
    mime: str,
    **dep_kwargs,
) -> Document:
    """同步段 + 异步段一次走完，返回登记行（状态以 process 后为准）。"""
    admin = session.scalars(select(User).where(User.username == "admin")).one()
    doc = create_document(session, storage, admin, meta, content, filename, mime)
    process_document(session, _make_deps(storage, **dep_kwargs), doc.id)
    return doc


def _dept_id(session: Session) -> int:
    return session.scalars(select(User.dept_id).where(User.username == "admin")).one()


def _audits(session: Session, doc_id: int) -> list[AuditLog]:
    """按 object 精确读回该文档的审计（不依赖表空前提，按 object_type+object_id 过滤）。"""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.object_type == "document", AuditLog.object_id == doc_id)
            .order_by(AuditLog.id)
        ).all()
    )


def _chunks(session: Session, doc_id: int) -> list[Chunk]:
    return list(
        session.scalars(
            select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.position)
        ).all()
    )


def test_upload_ready_writes_chunks_and_audits(session: Session, tmp_path) -> None:
    """正常 docx 入库（general 档基线）：ready；chunks 逐块等于分块器输出（含 title_path 前缀）；
    doc_type=general 落库；审计 upload/ready。"""
    storage = LocalFileStorage(tmp_path)
    content = _docx_bytes(
        [("第一章 总则", [_filler("总则", 24)]), ("第二章 考勤", [_filler("考勤", 24)])]
    )
    filename = " handbook_20260901.docx".strip()
    doc = _upload(
        session,
        storage,
        _meta(_dept_id(session), "客服_员工手册_20260901"),
        content,
        filename,
        DOCX_MIME,
    )

    assert doc.status == "ready"
    assert doc.is_active is True
    assert doc.parse_engine == "docx"
    assert doc.parse_error is None
    assert doc.doc_type == "general"  # 两节共 ~1650 字、无条款/表格信号：兜底档
    expected = split(DocxParser().parse(filename, content))  # 分块器输出为期望基线
    assert len(expected) == 2
    rows = _chunks(session, doc.id)
    assert [row.position for row in rows] == [chunk.position for chunk in expected]
    for row, chunk in zip(rows, expected, strict=True):  # 逐块读回精确相等
        assert row.content == chunk.content  # 含 `title_path｜` 前缀
        assert row.title_path == chunk.title_path
        assert row.content_chars == len(chunk.content)
        assert row.page_no == chunk.page  # docx 无页码概念：page_no 随块保持 NULL
        assert row.dept_id == doc.dept_id
        assert row.is_active is True
    assert all(row.page_no is None for row in rows)
    dims = (
        session.execute(
            select(func.vector_dims(Chunk.embedding)).where(Chunk.document_id == doc.id)
        )
        .scalars()
        .all()
    )
    assert dims == [1024, 1024]
    assert [audit.action for audit in _audits(session, doc.id)] == [
        "document.upload",
        "document.ready",
    ]


def test_policy_document_chunked_by_clause_and_doc_type_persisted(
    session: Session, tmp_path
) -> None:
    """制度条款文档入库（policy_clause 档）：doc_type 落库读回；分块整条成块不拦腰断——
    前 4 条各自成块逐字等于完整条款（含段间换行），碎条尾条（<120）并入前条，标题路径取所在节。"""
    storage = LocalFileStorage(tmp_path)
    clauses = [_clause_text(i, 4) for i in range(1, 6)]  # 各 148 字：整条成块
    fragment = _clause_text(6, 1)  # 40 字 <120：尾块并入前条
    doc = _upload(
        session,
        storage,
        _meta(_dept_id(session), "客服_考勤制度_20260901"),
        _docx_bytes([("第一章 总则", [*clauses, fragment])]),
        "policy.docx",
        DOCX_MIME,
    )

    assert doc.status == "ready"
    assert doc.doc_type == "policy_clause"  # 六处条款编号 ≥5：分型落库读回
    rows = _chunks(session, doc.id)
    bodies = [
        *(clause + "\n" for clause in clauses[:4]),  # 段界换行随段保留（零丢字口径）
        clauses[4] + "\n" + fragment,  # 尾块碎条并入：同块相邻直连逐字无损
    ]
    assert [row.position for row in rows] == [0, 1, 2, 3, 4]
    for row, body in zip(rows, bodies, strict=True):
        assert row.content == f"第一章 总则｜{body}"  # 整条逐字（含并入），绝不拦腰截断
        assert row.title_path == "第一章 总则"
        assert row.content_chars == len(row.content)
        assert row.dept_id == doc.dept_id


def test_missing_metadata_rejected(session: Session, tmp_path) -> None:
    """元数据卡口（F-301 服务侧兜底）：缺责任人 → 422 且消息点名字段，不入库。"""
    meta = _meta(_dept_id(session), "客服_缺责任人_20260901")
    del meta["owner_name"]
    with pytest.raises(ApiError) as ei:
        create_document(
            session,
            LocalFileStorage(tmp_path),
            session.scalars(select(User)).first(),
            meta,
            _docx_bytes([("第一章 总则", [_filler("总则", 6)])]),
            "no_owner.docx",
            DOCX_MIME,
        )
    assert ei.value.http_status == 422
    assert "owner_name" in ei.value.message
    assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_garbled_text_failed_with_encoding_reason(session: Session, tmp_path) -> None:
    """乱码检测（F-1005）：替换符占比超阈值 → failed 原因含"编码异常"。"""
    storage = LocalFileStorage(tmp_path)
    garbled = _filler("总则", 6) + "\ufffd" * 60  # 约 27% 替换符 > 5% 阈值
    doc = _upload(
        session,
        storage,
        _meta(_dept_id(session), "客服_乱码样本_20260901"),
        _docx_bytes([("第一章 总则", [garbled])]),
        "garbled.docx",
        DOCX_MIME,
    )
    assert doc.status == "failed"
    assert "编码异常" in doc.parse_error


def test_confirm_missing_document_404(session: Session) -> None:
    """confirm 指向不存在的文档：抛 404 ApiError。"""
    admin_id = session.scalars(select(User.id).where(User.username == "admin")).one()
    with pytest.raises(ApiError) as ei:
        confirm_document(session, 987654321, actor_user_id=admin_id)
    assert ei.value.http_status == 404


def test_double_confirm_second_rejected_single_audit(session: Session) -> None:
    """B3 TOCTOU：confirm 为条件迁移（仅 is_active=False 可激活）——已生效文档重复确认
    422/1003，document.confirm 审计恰一条（双管理员并发确认同构：第二个 0 行迁移即
    422，不产生第二条审计、不重复留痕）；不存在的文档仍 404（上一用例口径不变）。"""
    admin_id = session.scalars(select(User.id).where(User.username == "admin")).one()
    dept_id = session.scalar(select(User.dept_id).where(User.username == "admin"))
    digest = hashlib.sha256(b"double-confirm-probe").hexdigest()
    doc = Document(
        title="客服_重复确认样本",
        dept_id=dept_id,
        original_filename="double_confirm.md",
        file_path=f"seed/{digest}.md",
        mime="text/markdown",
        size_bytes=16,
        content_hash=digest,
        version=1,
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        is_active=False,
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()

    confirmed = confirm_document(session, doc.id, actor_user_id=admin_id)
    assert confirmed.is_active is True
    with pytest.raises(ApiError) as ei:
        confirm_document(session, doc.id, actor_user_id=admin_id)
    assert ei.value.http_status == 422
    assert ei.value.code == ERR_PARAM_INVALID
    audits = session.scalars(
        select(AuditLog).where(
            AuditLog.object_type == "document",
            AuditLog.object_id == doc.id,
            AuditLog.action == "document.confirm",
        )
    ).all()
    assert len(audits) == 1


def test_exact_duplicate_rejected_409_pointing_to_existing(session: Session, tmp_path) -> None:
    """完全重复：create_document 抛 409 ApiError 且消息指向已有文档 id；库内该 hash 文档数不变。"""
    storage = LocalFileStorage(tmp_path)
    content = _docx_bytes([("第一章 总则", [_filler("总则", 18)])])
    dept = _dept_id(session)
    existing = _upload(
        session,
        storage,
        _meta(dept, "客服_查重样本_20260901"),
        content,
        "dup_probe.docx",
        DOCX_MIME,
    )
    before = session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.content_hash == existing.content_hash)
    )

    with pytest.raises(ApiError) as ei:
        create_document(
            session,
            storage,
            session.get(User, existing.uploaded_by),
            _meta(dept, "客服_查重样本_20260901"),
            content,
            "dup_probe_2.docx",
            DOCX_MIME,
        )
    assert ei.value.http_status == 409
    assert ei.value.code == ERR_DUPLICATE_DOC
    assert str(existing.id) in ei.value.message
    after = session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.content_hash == existing.content_hash)
    )
    assert after == before == 1


def _rival_document(dept_id: int, uploader_id: int, sha256: str, size: int) -> Document:
    """并发对手行：同 hash 异标题（唯一冲突点是 uq_doc_content_hash），parsing 未生效。"""
    return Document(
        title="客服_并发对手_20260901",
        dept_id=dept_id,
        original_filename="rival.docx",
        file_path="rivals/rival.docx",
        mime=DOCX_MIME,
        size_bytes=size,
        content_hash=sha256,
        version=1,
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="parsing",
        is_active=False,
        uploaded_by=uploader_id,
    )


def test_concurrent_duplicate_commit_maps_to_409(pg, tmp_path, monkeypatch) -> None:
    """DOC-06 并发半边：同步查重通过后、提交前对手抢注同 hash → 提交段唯一索引冲突转 409。"""
    storage = LocalFileStorage(tmp_path)
    content = _docx_bytes([("第一章 总则", [_filler("总则", 18)])])
    sha256 = hashlib.sha256(content).hexdigest()
    rival_ids: list[int] = []
    racer = Session(pg)  # 上传方独立会话：提交期冲突的降级状态不污染 fixture 会话
    real_flush = Session.flush  # 打补丁前留存真方法：实例属性遮蔽后须显式调用真 flush

    def commit_after_rival() -> None:
        """模拟 commit 期冲突：对手连接抢先提交同 hash 行，上传方延迟至此才真实落库。"""
        with Session(pg) as rival, rival.begin():
            row = _rival_document(dept, admin.id, sha256, len(content))
            rival.add(row)
            rival.flush()
            rival_ids.append(row.id)
        real_flush(racer)  # 对手已提交：此处 INSERT 真实撞 uq_doc_content_hash 唯一索引

    try:
        admin = racer.scalars(select(User).where(User.username == "admin")).one()
        dept = admin.dept_id
        monkeypatch.setattr(racer, "flush", lambda *args, **kwargs: None)
        monkeypatch.setattr(racer, "commit", commit_after_rival)
        with pytest.raises(ApiError) as ei:
            create_document(
                racer,
                storage,
                admin,
                _meta(dept, "客服_并发抢注_20260901"),
                content,
                "race.docx",
                DOCX_MIME,
            )
        survivors = racer.scalars(  # 读回：库内该 hash 仅剩对手一行，上传方行未落库
            select(Document).where(Document.content_hash == sha256)
        ).all()
        assert [(row.id, row.status) for row in survivors] == [(rival_ids[0], "parsing")]
        assert ei.value.code == ERR_DUPLICATE_DOC
        assert ei.value.http_status == 409
        assert "重复" in ei.value.message
    finally:
        racer.rollback()  # 上传方事务全程未提交：显式丢弃并归还连接
        racer.close()
        with pg.begin() as conn:  # 对手行是真实提交：显式清场，防污染后续用例
            conn.execute(text("DELETE FROM documents WHERE id = ANY(:ids)"), {"ids": rival_ids})


def test_sync_gate_rejects_unsupported_extension(session: Session, tmp_path) -> None:
    """同步白名单闸门（编排职责）：非白名单扩展名在 create_document 即拒收，不入库。
    2026-10-05 格式扩展后 txt/md 等已进白名单，改用 .exe 验证拒收闸门仍在。"""
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(ApiError) as ei:
        create_document(
            session,
            storage,
            session.scalars(select(User)).first(),
            _meta(_dept_id(session), "客服_非法格式_20260901"),
            b"MZ binary",
            "note.exe",
            "application/octet-stream",
        )
    assert ei.value.http_status == 422
    assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_soft_deleted_reupload_allowed(session: Session, tmp_path) -> None:
    """软删后重传：旧文档 deleted_at 置位后同内容 create 成功（部分唯一索引语义）。"""
    storage = LocalFileStorage(tmp_path)
    content = _docx_bytes([("第一章 总则", [_filler("总则", 18)])])
    dept = _dept_id(session)
    old = _upload(
        session, storage, _meta(dept, "客服_重传样本_20260901"), content, "reupload.docx", DOCX_MIME
    )
    old.deleted_at = FakeClock().now()
    session.commit()

    fresh = create_document(
        session,
        storage,
        session.get(User, old.uploaded_by),
        _meta(dept, "客服_重传样本_20260901"),
        content,
        "reupload_v2.docx",
        DOCX_MIME,
    )
    assert fresh.id != old.id
    assert fresh.status == "parsing"
    assert (
        session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.content_hash == old.content_hash)
        )
        == 2
    )  # 软删行不占 hash：新旧两行并存


def test_scan_redline_failed_and_retry_still_failed(session: Session, tmp_path) -> None:
    """扫描件红线：空文本 docx 判 failed 且原因含"疑似扫描件或空文档"；重试仍 failed；无块残留。"""
    storage = LocalFileStorage(tmp_path)
    buffer = io.BytesIO()
    DocxDocument().save(buffer)  # 零段落零标题：解析产物为空
    doc = _upload(
        session,
        storage,
        _meta(_dept_id(session), "客服_扫描件_20260901"),
        buffer.getvalue(),
        "empty.docx",
        DOCX_MIME,
    )

    assert doc.status == "failed"
    assert "疑似扫描件或空文档" in doc.parse_error
    assert _chunks(session, doc.id) == []
    process_document(session, _make_deps(storage), doc.id)  # 重试：再次失败
    assert doc.status == "failed"
    assert "疑似扫描件或空文档" in doc.parse_error
    assert [audit.action for audit in _audits(session, doc.id)] == [
        "document.upload",
        "document.failed",
        "document.failed",
    ]


def test_corrupt_docx_failed_with_chinese_parser_reason(session: Session, tmp_path) -> None:
    """包体非法 docx：解析器 ApiError 中文原因透传为 parse_error（失败可重试）。"""
    storage = LocalFileStorage(tmp_path)
    doc = _upload(
        session,
        storage,
        _meta(_dept_id(session), "客服_损坏包_20260901"),
        b"PK\x03\x04truncated",
        "broken.docx",
        DOCX_MIME,
    )
    assert doc.status == "failed"
    assert "docx" in doc.parse_error


def test_pdf_without_mineru_uses_pypdf_engine(session: Session, tmp_path) -> None:
    """降级（配置路径）：mineru_base_url="" 时 pdf 走 pypdf，parse_engine="pypdf" 且非降级标注。"""
    storage = LocalFileStorage(tmp_path)
    text = "Section one policy overview with enough content for red line. " * 16  # 每页 ~992 字
    doc = _upload(
        session,
        storage,
        _meta(_dept_id(session), "客服_政策文本_20260901"),
        _pdf_bytes([text, text]),
        "policy_2026.pdf",
        PDF_MIME,
    )
    assert doc.status == "ready"
    assert doc.parse_engine == "pypdf"
    assert doc.parse_error is None  # 配置缺省路径不是故障降级，不打降级标注
    rows = _chunks(session, doc.id)
    assert len(rows) == 2  # 两页各 ~992 字 ≥notice min 800：每页独立成块（句切尾块并回不混页）
    assert [row.position for row in rows] == [0, 1]
    assert [row.page_no for row in rows] == [1, 2]  # 页码入库：块所在页（1 起）回填 page_no
    for row in rows:  # PDF 无标题层级：title_path 取文件名主干
        assert row.title_path == "policy_2026"
        assert row.content.startswith("policy_2026｜")


def test_mineru_outage_degrades_to_pypdf_annotated(session: Session, tmp_path, monkeypatch) -> None:
    """降级（故障路径）：MinerU 不可用自动降级 pypdf，标注"降级解析"且内容可检索。"""
    storage = LocalFileStorage(tmp_path)
    # 扫描件场景模拟：屏蔽文字版预判（测试 pdf 为文字版，真实扫描件无文字层）
    monkeypatch.setattr("app.services.ingest_pipeline._is_text_based_pdf", lambda content: False)
    storage = LocalFileStorage(tmp_path)
    text = "Section one policy overview with enough content for red line. " * 10
    doc = _upload(
        session,
        storage,
        _meta(_dept_id(session), "客服_降级解析_20260901"),
        _pdf_bytes([text, text]),
        "degraded.pdf",
        PDF_MIME,
        mineru_base_url=MINERU_DOWN,
    )
    assert doc.status == "ready"
    assert doc.parse_engine == "pypdf"
    assert doc.parse_error is not None and "降级解析" in doc.parse_error
    ready_audit = _audits(session, doc.id)[-1]
    assert ready_audit.action == "document.ready"
    assert ready_audit.detail["after"]["degraded"] is True
    assert _chunks(session, doc.id) != []


def test_pdf_header_line_aggregated_across_page_blocks(session: Session, tmp_path) -> None:
    """跨页页眉聚合（task-6b 修复轮）：3 页共享同一 ≤30 字页眉行不得泄漏进任何 chunk；
    仅在 1 个块出现 1 次的正常短行不得被聚合误删。走完整 analyze_document 管线。"""
    storage = LocalFileStorage(tmp_path)
    header = "Quarterly Report Internal"  # 25 字：每页块内仅 1 次，只有跨块聚合才可达 >=3
    unique_short = "Unique short line ok"  # 20 字：全文仅出现 1 次，聚合后仍不得误删
    body = "Section policy overview with enough content for red line."
    pages = [
        [header, *(f"{body} page {page} item {item}" for item in range(12))] for page in range(1, 4)
    ]
    pages[0].insert(1, unique_short)
    content = _pdf_bytes_lines(pages)
    doc = create_document(
        session,
        storage,
        session.scalars(select(User).where(User.username == "admin")).one(),
        _meta(_dept_id(session), "客服_页眉聚合_20260901"),
        content,
        "header_report.pdf",
        PDF_MIME,
    )

    outcome = analyze_document(session, _make_deps(storage), doc)  # 只读分析：不写库不收口

    assert len(outcome.chunks) == 3  # 每页正文 >300 字各自成块：页眉在每块内仅出现 1 次
    for chunk in outcome.chunks:  # 跨页页眉：文档级 >=3 次，任何块都不得携带
        assert header not in chunk.content
    joined = "\n".join(chunk.content for chunk in outcome.chunks)
    assert unique_short in joined  # 仅 1 个块出现 1 次的正常短行：聚合口径不误删


def test_version_switch_archives_old_active_version(session: Session, tmp_path) -> None:
    """版本切换：同标题第二版 ready 后 v1 归档（is_active=False 且 deleted_at 为空）；检索只命中 v2。"""
    storage = LocalFileStorage(tmp_path)
    dept = _dept_id(session)
    title = "客服_考勤制度_20260901"
    v1 = _upload(
        session,
        storage,
        _meta(dept, title),
        _docx_bytes([("第一章 总则", [_filler("总则", 18)])]),
        "v1.docx",
        DOCX_MIME,
    )
    assert (v1.version, v1.is_active) == (1, True)
    v2 = _upload(
        session,
        storage,
        _meta(dept, title),
        _docx_bytes([("第一章 总则", [_filler("考勤修订", 19)])]),
        "v2.docx",
        DOCX_MIME,
    )

    assert v2.version == 2
    assert v1.is_active is False
    assert v1.deleted_at is None  # 归档≠软删
    assert v2.is_active is True
    active = session.scalars(
        select(Document.id).where(
            Document.dept_id == dept, Document.title == title, Document.is_active.is_(True)
        )
    ).all()
    assert active == [v2.id]  # 检索过滤（is_active）只命中 v2


def test_sensitive_hit_pends_until_confirm(session: Session, tmp_path) -> None:
    """待确认：含身份证文本命中敏感扫描→is_active=False；confirm 后激活可被 is_active 检索命中。"""
    storage = LocalFileStorage(tmp_path)
    dept = _dept_id(session)
    title = "客服_人事数据_20260901"
    content = _docx_bytes(
        [
            (
                "人事数据",
                # 正文 ≤400 字：notice 档 child_len=400 下仍单块（切分策略 B，勿再加大）
                ["员工张三身份证号 11010519491231002X 由人事部专人保管。", _filler("附则", 9)],
            )
        ]
    )
    doc = _upload(session, storage, _meta(dept, title), content, "hr_data.docx", DOCX_MIME)

    assert doc.status == "ready"  # 状态机无"待确认"枚举：以 is_active=False 表达
    assert doc.is_active is False
    assert len(_chunks(session, doc.id)) == 1  # 块已写库，等待确认后可检索
    ready_audit = _audits(session, doc.id)[-1]
    assert ready_audit.detail["after"]["sensitiveHits"] >= 1
    assert (
        session.scalars(
            select(Document.id).where(
                Document.dept_id == dept, Document.title == title, Document.is_active.is_(True)
            )
        ).all()
        == []
    )

    # 确认者是另一名管理员（≠上传者）：审计 actor 必须记真实操作者（T13 W2 移交项）
    confirmer = User(
        username="confirmer",
        password_hash="-",  # 服务层直调不做鉴权，哈希占位即可
        dept_id=dept,
        role="admin",
    )
    session.add(confirmer)
    session.flush()
    confirmed = confirm_document(session, doc.id, actor_user_id=confirmer.id)
    assert confirmed.is_active is True
    assert session.scalars(
        select(Document.id).where(
            Document.dept_id == dept, Document.title == title, Document.is_active.is_(True)
        )
    ).all() == [doc.id]
    assert "document.confirm" in [audit.action for audit in _audits(session, doc.id)]
    confirm_audit = [a for a in _audits(session, doc.id) if a.action == "document.confirm"][-1]
    assert confirmer.id != doc.uploaded_by  # 两人不同，断言才有区分力
    assert confirm_audit.actor_user_id == confirmer.id  # 是确认者本人，而非 uploaded_by


def test_near_dup_simhash_pends_for_confirmation(session: Session, tmp_path) -> None:
    """近似查重编排（F-1002）：同部门近拟内容（海明距离≤3）→新文档 is_active=False 待确认。"""
    storage = LocalFileStorage(tmp_path)
    dept = _dept_id(session)
    base = _docx_bytes([("第一章 总则", [_filler("总则", 18)])])
    first = _upload(
        session, storage, _meta(dept, "客服_制度甲_20260901"), base, "a.docx", DOCX_MIME
    )
    assert first.simhash is not None
    tweaked = _docx_bytes(
        [("第一章 总则", [_filler("总则", 18), "本条为微调补充说明，用于构造近似样本。"])]
    )
    second = _upload(
        session, storage, _meta(dept, "客服_制度乙_20260901"), tweaked, "b.docx", DOCX_MIME
    )

    assert second.status == "ready"
    assert second.is_active is False  # 近似命中→待确认
    assert _audits(session, second.id)[-1].detail["after"]["nearDup"] is True
    assert first.is_active is True  # 异标题文档不受影响


def test_expire_due_documents_offline(session: Session, tmp_path) -> None:
    """到期下线（F-1004）：expiry_date < 时钟当前日的文档置 is_active=False 并留审计；未到期不动。"""
    storage = LocalFileStorage(tmp_path)
    dept = _dept_id(session)
    expired = _upload(
        session,
        storage,
        _meta(dept, "客服_已过期_20260901", expiry=date(2026, 9, 15)),
        _docx_bytes([("第一章 总则", [_filler("总则", 18)])]),
        "expired.docx",
        DOCX_MIME,
    )
    future = _upload(
        session,
        storage,
        _meta(dept, "客服_未到期_20260901", expiry=date(2026, 10, 2)),
        _docx_bytes([("第一章 总则", [_filler("考勤", 18)])]),
        "future.docx",
        DOCX_MIME,
    )
    perpetual = _upload(
        session,
        storage,
        _meta(dept, "客服_无期限_20260901"),
        _docx_bytes([("第一章 总则", [_filler("附则", 18)])]),
        "perp.docx",
        DOCX_MIME,
    )

    count = expire_due_documents(session, FakeClock("2026-10-01T08:00:00+00:00"))

    assert count == 1
    assert expired.is_active is False
    assert future.is_active is True
    assert perpetual.is_active is True
    expire_audit = _audits(session, expired.id)[-1]
    assert expire_audit.action == "document.expire"
    assert expire_audit.detail["expiryDate"] == "2026-09-15"


def test_embed_failure_retry_idempotent(session: Session, tmp_path) -> None:
    """失败重试幂等：embed 注入失败→failed；预置残留块后重试→ready 且先删后插无重复。"""
    storage = LocalFileStorage(tmp_path)
    content = _docx_bytes(
        [("第一章 总则", [_filler("总则", 24)]), ("第二章 考勤", [_filler("考勤", 24)])]
    )
    doc = create_document(
        session,
        storage,
        session.scalars(select(User).where(User.username == "admin")).one(),
        _meta(_dept_id(session), "客服_重试幂等_20260901"),
        content,
        "retry.docx",
        DOCX_MIME,
    )
    process_document(session, _make_deps(storage, embedder=StubEmbedder(fail_times=1)), doc.id)
    assert doc.status == "failed"
    assert doc.parse_error == "向量化服务调用失败，请稍后重试"
    assert _chunks(session, doc.id) == []  # 失败路径不落半成品块

    session.add(
        Chunk(
            document_id=doc.id,
            dept_id=doc.dept_id,
            title_path="旧",
            content="旧残留块",
            content_chars=4,
            position=999,
            embedding=[0.0] * 1024,
        )
    )  # 预置上轮残留
    session.commit()
    process_document(session, _make_deps(storage), doc.id)  # 重试（干净 embedder）

    assert doc.status == "ready"
    expected = split(DocxParser().parse("retry.docx", content))
    rows = _chunks(session, doc.id)
    assert [row.position for row in rows] == [chunk.position for chunk in expected] == [0, 1]
    assert [row.content for row in rows] == [chunk.content for chunk in expected]  # 无重复无残留
    assert all(row.content != "旧残留块" for row in rows)
    assert [audit.action for audit in _audits(session, doc.id)] == [
        "document.upload",
        "document.failed",
        "document.ready",
    ]


def test_unexpected_error_maps_to_failed(session: Session, tmp_path, monkeypatch) -> None:
    """兜底失败：编排内未知异常→failed 中文通用原因（不泄漏内部细节），旧块保留（DOC-26 语义）。"""
    storage = LocalFileStorage(tmp_path)
    doc = _upload(
        session,
        storage,
        _meta(_dept_id(session), "客服_兜底失败_20260901"),
        # 正文 ≤400 字：notice 档 child_len=400 下仍单块（切分策略 B，勿再加大）
        _docx_bytes([("第一章 总则", [_filler("总则", 9)])]),
        "ok.docx",
        DOCX_MIME,
    )
    assert doc.status == "ready"
    before = _chunks(session, doc.id)

    def boom(*args, **kwargs):
        raise RuntimeError("internal detail")

    monkeypatch.setattr("app.services.ingest_pipeline.split_by_profile", boom)
    process_document(session, _make_deps(storage), doc.id)

    assert doc.status == "failed"
    assert doc.parse_error == "文档处理失败，请稍后重试"
    assert "internal detail" not in doc.parse_error
    assert _chunks(session, doc.id) == before  # 失败不动旧块集
    assert len(before) == 1


def test_process_missing_document_404(session: Session, tmp_path) -> None:
    """process 指向不存在的文档：抛 404 ApiError，不写任何状态。"""
    with pytest.raises(ApiError) as ei:
        process_document(session, _make_deps(LocalFileStorage(tmp_path)), 987654321)
    assert ei.value.http_status == 404


def test_concurrent_same_title_versions_serialized(session: Session, pg, tmp_path) -> None:
    """并发版本：两线程同标题提交（内存锁串行化提交段）→版本号 1、2，无唯一约束冲突。"""
    storage = LocalFileStorage(tmp_path)
    dept = _dept_id(session)
    admin_id = session.scalar(select(User.id).where(User.username == "admin"))
    title = "客服_并发版本_20260901"
    lock = threading.Lock()
    results: dict[int, tuple[int, int]] = {}
    errors: list[Exception] = []
    created: list[int] = []  # 建档即登记：无论后续哪一步失败，finally 都能清场

    def worker(idx: int, tag: str, lines: int) -> None:
        thread_session = Session(pg)
        try:
            content = _docx_bytes([("第一章 总则", [_filler(tag, lines)])])
            with lock:  # 内存锁串行化"创建+处理+提交"段（等价 SQLite 写串行语义）
                uploader = thread_session.get(User, admin_id)
                doc = create_document(
                    thread_session,
                    storage,
                    uploader,
                    _meta(dept, title),
                    content,
                    f"concurrent_{idx}.docx",
                    DOCX_MIME,
                )
                created.append(doc.id)
                process_document(thread_session, _make_deps(storage), doc.id)
                results[idx] = (doc.id, doc.version)
                thread_session.commit()
        except Exception as exc:  # noqa: BLE001 — 汇聚到断言，不静默
            errors.append(exc)
        finally:
            thread_session.close()

    threads = [
        threading.Thread(target=worker, args=(1, "甲线", 18)),
        threading.Thread(target=worker, args=(2, "乙线", 17)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert errors == []
        assert sorted(version for _, version in results.values()) == [1, 2]
        assert results[1][0] != results[2][0]
        rows = session.scalars(
            select(Document).where(Document.dept_id == dept, Document.title == title)
        ).all()
        assert sorted(row.version for row in rows) == [1, 2]
    finally:
        with pg.begin() as conn:  # 真实提交过的行须显式清理，防污染后续用例与下一次全量
            conn.execute(text("DELETE FROM chunks WHERE document_id = ANY(:ids)"), {"ids": created})
            conn.execute(
                text(
                    "DELETE FROM audit_logs WHERE object_type='document' AND object_id = ANY(:ids)"
                ),
                {"ids": created},
            )
            conn.execute(
                text("DELETE FROM documents WHERE id = ANY(:ids) OR title = :title"),
                {"ids": created, "title": title},
            )
