r"""zip 批量导入 API 测试（F-1007 + §6.3 解压安全上限）。

覆盖：合规包 2 docx 逐条 accepted 读回（文档行+审计 detail 读回）/重复字节条目
duplicate 指向已有文档/命名不符 skipped_metadata 不入库/嵌套 zip 整包 422/单条目
超 50MB 拒（低阈值注入，包继续）/条目数超限中止（低阈值注入）/zip 炸弹总量超限
中止且零残留（无文档、存储目录空）/损坏 zip 422/member 403。

安全上限经模块常量注入低阈值（同 test_documents 的 51MB 早拒口径——真实 500MB
会拖垮测试，边界值留给 unit 层）；test_mode=True 时条目解析同步执行，响应即可
断言终态；STORAGE_DIR 指向 tmp_path 隔离落盘。
"""

import io
import os
import zipfile
from datetime import date

import pytest
from docx import Document as DocxDocument
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID
from app.db.models import AuditLog, Department, Document, User
from app.services import zip_import

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ---------- fixtures：settings 环境注入（口径同 test_documents.py） ----------


@pytest.fixture()
def sandbox_storage(tmp_path):
    """STORAGE_DIR 指向 tmp_path 并清 get_settings 缓存：落盘与解析读盘全隔离。"""
    saved = os.environ.get("STORAGE_DIR")
    os.environ["STORAGE_DIR"] = str(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    if saved is None:
        os.environ.pop("STORAGE_DIR", None)
    else:
        os.environ["STORAGE_DIR"] = saved
    get_settings.cache_clear()


@pytest.fixture()
def test_mode():
    """开启 settings.test_mode：条目解析在请求内同步执行（响应即可断言终态）。"""
    saved = os.environ.get("TEST_MODE")
    os.environ["TEST_MODE"] = "true"
    get_settings.cache_clear()
    yield
    if saved is None:
        os.environ.pop("TEST_MODE", None)
    else:
        os.environ["TEST_MODE"] = saved
    get_settings.cache_clear()


# ---------- 构造与登录辅助 ----------


def make_user(session: Session, username: str, password: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def admin_login(client, session: Session) -> User:
    """创建并登录 admin（cookie 由 client 保持），返回用户便于断言 owner/审计 actor。"""
    user = make_user(session, "zipadmin", "right-one", role="admin")
    assert login(client, "zipadmin", "right-one").status_code == 200
    return user


def _filler(tag: str, lines: int) -> str:
    """长正文构造（同 test_documents）：每行唯一含序号，避开清洗规则。"""
    return "\n".join(f"{tag}第{i}条 内容说明{i}：适用于全体员工并遵照执行。" for i in range(lines))


def _docx_bytes(tag: str) -> bytes:
    """单标题 docx：正文 ≥300 字成块，tag 保证不同条目字节不同（避开误触查重）。"""
    document = DocxDocument()
    document.add_heading("第一章 总则", level=1)
    document.add_paragraph(_filler(tag, 18))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_zip(entries: dict[str, bytes]) -> bytes:
    """内存构造 zip（writestr 时间戳固定 1980，内容相同则字节确定）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def post_zip(client, content: bytes):
    return client.post(
        "/api/documents/import-zip", files={"file": ("batch.zip", content, "application/zip")}
    )


def _dept_id(session: Session, code: str) -> int:
    return session.scalars(select(Department.id).where(Department.code == code)).one()


def _result_by_name(data: dict, filename: str) -> dict:
    matches = [r for r in data["results"] if r["filename"] == filename]
    assert len(matches) == 1
    return matches[0]


# ---------- 合规包：逐条 accepted + 文档行/审计读回 ----------


def test_zip_two_docx_accepted_ingested_and_audited(client, session, sandbox_storage, test_mode):
    """合规包 2 docx：202 逐条 accepted+documentId；文档行按文件名元数据落库、解析
    至 ready；责任人=上传管理员；落盘 UUID；审计 document.import_zip 计数读回。"""
    admin = admin_login(client, session)
    zip_bytes = make_zip(
        {
            "service_员工手册_20260901.docx": _docx_bytes("手册"),
            "hr_考勤制度_2026-09-15.docx": _docx_bytes("考勤"),
        }
    )
    resp = post_zip(client, zip_bytes)
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert set(data) == {"results", "accepted", "rejected", "skipped"}
    assert (data["accepted"], data["rejected"], data["skipped"]) == (2, 0, 0)

    first = _result_by_name(data, "service_员工手册_20260901.docx")
    second = _result_by_name(data, "hr_考勤制度_2026-09-15.docx")
    assert set(first) == {"filename", "status", "documentId"}
    assert first["status"] == "accepted"
    assert set(second) == {"filename", "status", "documentId"}
    assert second["status"] == "accepted"

    doc1 = session.get(Document, first["documentId"])
    doc2 = session.get(Document, second["documentId"])
    assert doc1.title == "员工手册"  # 主题不含部门/日期
    assert doc1.dept_id == _dept_id(session, "service")
    assert doc1.effective_date == date(2026, 9, 1)  # YYYYMMDD 白名单解析
    assert doc1.owner_name == "zipadmin"  # 责任人=上传管理员兜底
    assert doc1.original_filename == "service_员工手册_20260901.docx"
    assert doc1.status == "ready"  # test_mode 同步解析至终态
    assert doc1.is_active is True
    assert doc2.title == "考勤制度"
    assert doc2.dept_id == _dept_id(session, "hr")
    assert doc2.effective_date == date(2026, 9, 15)  # YYYY-MM-DD 白名单解析
    assert doc2.status == "ready"

    assert "员工手册" not in doc1.file_path  # 落盘 UUID，不落原始文件名
    assert (sandbox_storage / doc1.file_path).is_file()  # 文件真实落盘

    audits = session.scalars(select(AuditLog).where(AuditLog.action == "document.import_zip")).all()
    assert len(audits) == 1
    assert audits[0].actor_user_id == admin.id
    assert audits[0].detail == {"total": 2, "accepted": 2, "rejected": 0, "skipped": 0}


# ---------- 重复字节条目 → duplicate 指向已有文档 ----------


def test_zip_duplicate_entry_maps_to_existing_document(client, session, sandbox_storage, test_mode):
    """包内两条字节相同的条目：先者 accepted，后者 duplicate 且 documentId 指向先者，
    计入 skipped，库里只落一行（F-1001 精确查重的批量语义）。"""
    admin_login(client, session)
    same = _docx_bytes("同件")
    resp = post_zip(
        client,
        make_zip({"service_政策A_20260901.docx": same, "service_政策B_20260901.docx": same}),
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert (data["accepted"], data["skipped"]) == (1, 1)

    first = _result_by_name(data, "service_政策A_20260901.docx")
    second = _result_by_name(data, "service_政策B_20260901.docx")
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert second["documentId"] == first["documentId"]
    assert "重复" in second["reason"]
    assert session.scalar(select(func.count()).select_from(Document)) == 1


# ---------- 命名不符 → skipped_metadata 不入库 ----------


def test_zip_bad_naming_skipped_metadata_not_ingested(client, session, sandbox_storage, test_mode):
    """命名不符（无分隔/日期非法/部门枚举外）：逐条 skipped_metadata+中文原因，
    一律不入库；合规条目照常 accepted。"""
    admin_login(client, session)
    resp = post_zip(
        client,
        make_zip(
            {
                "service_员工手册_20260901.docx": _docx_bytes("手册"),
                "通知.docx": _docx_bytes("通知"),
                "hr_薪酬制度_2026年9月.docx": _docx_bytes("薪酬"),
                "legal_制度_20260901.docx": _docx_bytes("法务"),
            }
        ),
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert (data["accepted"], data["rejected"], data["skipped"]) == (1, 0, 3)

    for name in ("通知.docx", "hr_薪酬制度_2026年9月.docx", "legal_制度_20260901.docx"):
        item = _result_by_name(data, name)
        assert item["status"] == "skipped_metadata"
        assert "元数据待补" in item["reason"]
        assert "documentId" not in item  # 不入库：无文档可指向
    assert session.scalar(select(func.count()).select_from(Document)) == 1


# ---------- §6.3：嵌套 zip 整包拒收 ----------


def test_zip_nested_zip_rejected_whole_package(client, session, sandbox_storage, test_mode):
    """嵌套 zip：整包 422/1003 中文原因，合规条目也不导入，零残留。"""
    admin_login(client, session)
    inner = make_zip({"inner.txt": b"nested payload"})
    resp = post_zip(
        client,
        make_zip(
            {
                "service_员工手册_20260901.docx": _docx_bytes("手册"),
                "service_内嵌_20260901.zip": inner,
            }
        ),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert "嵌套" in body["message"]
    assert "zip" in body["message"]
    assert session.scalar(select(func.count()).select_from(Document)) == 0
    assert list(sandbox_storage.rglob("*")) == []  # 零落盘


# ---------- §6.3：单条目超上限 → 该条 rejected，包继续 ----------


def test_zip_entry_oversize_rejected_but_package_continues(
    client, session, sandbox_storage, test_mode, monkeypatch
):
    """单条目超 50MB 上限（低阈值注入）：该条 rejected+原因，合规条目照常 accepted。"""
    good = _docx_bytes("手册")
    monkeypatch.setattr(zip_import, "MAX_ENTRY_BYTES", len(good) + 512)
    big = b"PK\x03\x04" + b"0" * (len(good) + 1024)
    admin_login(client, session)
    resp = post_zip(
        client,
        make_zip(
            {
                "service_大文件_20260901.docx": big,
                "service_员工手册_20260901.docx": good,
            }
        ),
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert (data["accepted"], data["rejected"]) == (1, 1)
    rejected = _result_by_name(data, "service_大文件_20260901.docx")
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "文件大小超过 50MB 上限"
    assert "documentId" not in rejected
    accepted = _result_by_name(data, "service_员工手册_20260901.docx")
    assert accepted["status"] == "accepted"
    assert session.scalar(select(func.count()).select_from(Document)) == 1


# ---------- §6.3：条目数超限 → 中止 ----------


def test_zip_entry_count_over_limit_aborts(
    client, session, sandbox_storage, test_mode, monkeypatch
):
    """条目数超上限（低阈值注入 2）：整包 422，一个都不导入。"""
    monkeypatch.setattr(zip_import, "MAX_ENTRIES", 2)
    admin_login(client, session)
    resp = post_zip(
        client,
        make_zip(
            {
                "service_甲制度_20260901.docx": _docx_bytes("甲"),
                "service_乙制度_20260901.docx": _docx_bytes("乙"),
                "service_丙制度_20260901.docx": _docx_bytes("丙"),
            }
        ),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert "条目" in body["message"]
    assert session.scalar(select(func.count()).select_from(Document)) == 0


# ---------- §6.3：zip 炸弹总量超限 → 边解压边计数中止，零残留 ----------


def test_zip_total_limit_abort_leaves_nothing_behind(
    client, session, sandbox_storage, test_mode, monkeypatch
):
    """总量超上限（低阈值注入）：读到即中止 422，未入库任何文档、存储目录零文件
    （预扫描不落盘的设计使"中止并清理"无残留可清）。"""
    monkeypatch.setattr(zip_import, "MAX_TOTAL_BYTES", 1024)
    admin_login(client, session)
    resp = post_zip(
        client,
        make_zip(
            {
                "service_甲制度_20260901.docx": _docx_bytes("甲"),
                "service_乙制度_20260901.docx": _docx_bytes("乙"),
            }
        ),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert "总量" in body["message"]
    assert session.scalar(select(func.count()).select_from(Document)) == 0
    assert list(sandbox_storage.rglob("*")) == []  # 中止后零残留


# ---------- 损坏包 422 / 权限门 ----------


def test_zip_corrupt_archive_422(client, session, sandbox_storage, test_mode):
    """非 zip 字节：422/1003 中文提示，不入库。"""
    admin_login(client, session)
    resp = post_zip(client, b"this is definitely not a zip archive")
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert "zip" in body["message"]
    assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_zip_member_forbidden(client, session, sandbox_storage):
    """member 调用批量导入：403/2004（admin-only，同单文件上传口径）。"""
    make_user(session, "zipmember", "right-one", role="member")
    assert login(client, "zipmember", "right-one").status_code == 200
    resp = post_zip(client, make_zip({"service_手册_20260901.docx": _docx_bytes("手册")}))
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
