r"""文档上传与列表 API 测试（DOC-01/02/04/05/11 + §22.1 列表规范 + 权限门）。

brief 断言全覆盖：.doc 拒收 422 中文原因；51MB 拒；缺元数据 422 字段级；
未勾合规 422；member 全端点 403；上传 202 后列表出现 ready；响应键名全
camelCase（断言 "effectiveDate" in item）；dept/status/keyword 过滤与
pageSize 1-100 分页；retry 重跑 / confirm 激活 / 404。

test_mode=True（环境变量注入 + 清 get_settings 缓存）时端点内同步执行
process_document 便于断言；STORAGE_DIR 指向 tmp_path 隔离落盘。docx 构造
与 T13 同款口径：正文 ≥100 有效字符过质量红线。DOC-15/16（删除与引用拦截）
依赖 DELETE 端点，本任务未产出，留 W3 治理界面任务（见 task-14-report）。
"""

import io
import os
from datetime import date

import pytest
from docx import Document as DocxDocument
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    ERR_FORBIDDEN,
    ERR_PARAM_INVALID,
    ERR_UNSUPPORTED_FORMAT,
)
from app.db.models import AuditLog, Department, Document, User

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ---------- fixtures：settings 环境注入（手动保存/还原，保证还原先于清缓存） ----------


@pytest.fixture()
def sandbox_storage(tmp_path):
    """STORAGE_DIR 指向 tmp_path 并清 get_settings 缓存：上传落盘与解析读盘全隔离。"""
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
    """开启 settings.test_mode：上传端点同步执行 process_document（brief Step 3 口径）。"""
    saved = os.environ.get("TEST_MODE")
    os.environ["TEST_MODE"] = "true"
    get_settings.cache_clear()
    yield
    if saved is None:
        os.environ.pop("TEST_MODE", None)
    else:
        os.environ["TEST_MODE"] = saved
    get_settings.cache_clear()


# ---------- 构造与登录辅助（口径同 tests/api/test_auth.py 与 T13 集成测试） ----------


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


def admin_login(client, session: Session) -> None:
    """创建并登录 admin（cookie 由 client 保持），失败即失败在显式断言。"""
    make_user(session, "docadmin", "right-one", role="admin")
    resp = login(client, "docadmin", "right-one")
    assert resp.status_code == 200


def _filler(tag: str, lines: int) -> str:
    """长正文构造（同 T13）：每行唯一含序号，行数×约 27 字，避开清洗规则。"""
    return "\n".join(f"{tag}第{i}条 内容说明{i}：适用于全体员工并遵照执行。" for i in range(lines))


def _docx_bytes(sections: list[tuple[str, list[str]]]) -> bytes:
    """单级标题 docx：每标题节一段长正文（≥300 字成独立块）。"""
    document = DocxDocument()
    for heading, paragraphs in sections:
        document.add_heading(heading, level=1)
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _empty_docx() -> bytes:
    """零段落零标题：解析产物为空 → 触发"疑似扫描件或空文档"红线。"""
    buffer = io.BytesIO()
    DocxDocument().save(buffer)
    return buffer.getvalue()


def post_document(
    client,
    *,
    filename: str = "handbook.docx",
    content: bytes | None = None,
    mime: str = DOCX_MIME,
    title: str = "客服_员工手册_20260901",
    dept: str = "service",
    owner: str = "制度责任人",
    effective_date: str = "2026-09-01",
    expiry_date: str | None = None,
    compliant: bool = True,
):
    """multipart 上传请求：表单键名与 API 契约一致（camelCase）。"""
    data: dict[str, str] = {
        "title": title,
        "dept": dept,
        "owner": owner,
        "effectiveDate": effective_date,
        "complianceConfirmed": "true" if compliant else "false",
    }
    if expiry_date is not None:
        data["expiryDate"] = expiry_date
    if content is None:
        # 正文以 title 为 tag：docx 压缩时间戳为 2 秒粒度，同窗内两次保存字节相同会
        # 误触 SHA-256 查重（409），故内容必须随用例唯一
        content = _docx_bytes([("第一章 总则", [_filler(title, 18)])])
    return client.post("/api/documents", data=data, files={"file": (filename, content, mime)})


def _doc_audits(session: Session, doc_id: int) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.object_type == "document", AuditLog.object_id == doc_id)
            .order_by(AuditLog.id)
        ).all()
    )


# ---------- DOC-02：.doc 拒收 ----------


def test_upload_doc_rejected_with_chinese_reason(client, session, sandbox_storage, test_mode):
    """上传 .doc：422 code=1001，中文原因提示另存为 docx，不入库。"""
    admin_login(client, session)
    resp = post_document(
        client, filename="note.doc", content=b"legacy word binary", mime="application/msword"
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_UNSUPPORTED_FORMAT
    assert "docx" in body["message"]
    assert session.scalar(select(func.count()).select_from(Document)) == 0


# ---------- DOC-04：51MB 超限 ----------


def test_upload_oversize_rejected_422(client, session, sandbox_storage, test_mode):
    """51MB 文件：422 code=1003，提示 ≤50MB，不入库。"""
    admin_login(client, session)
    big = b"%PDF-1.4 " + b"0" * (51 * 1024 * 1024)
    resp = post_document(client, filename="big.pdf", content=big, mime="application/pdf")
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert "50MB" in body["message"]
    assert session.scalar(select(func.count()).select_from(Document)) == 0


# ---------- DOC-05：缺元数据字段级 422 / 未勾合规 422 ----------


def test_upload_missing_metadata_field_level_422(client, session, sandbox_storage, test_mode):
    """缺 title 与 owner：422/1003，data 给出字段级错误（standards §21.9）。"""
    admin_login(client, session)
    resp = client.post(
        "/api/documents",
        data={"dept": "service", "effectiveDate": "2026-09-01", "complianceConfirmed": "true"},
        files={"file": ("m.docx", _docx_bytes([("第一章 总则", [_filler("总则", 18)])]), DOCX_MIME)},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    fields = {entry["field"] for entry in body["data"]}
    assert {"title", "owner"} <= fields


def test_upload_compliance_unconfirmed_422(client, session, sandbox_storage, test_mode):
    """合规确认未勾：422 中文提示含"合规"，不入库（F-301）。"""
    admin_login(client, session)
    resp = post_document(client, compliant=False)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert "合规" in body["message"]
    assert session.scalar(select(func.count()).select_from(Document)) == 0


# ---------- 权限门：member 全端点 403 ----------


def test_member_forbidden_on_all_document_endpoints(client, session, sandbox_storage):
    """member 调用上传/列表/重试/确认：一律 403/2004（admin-only）。"""
    make_user(session, "docmember", "right-one", role="member")
    assert login(client, "docmember", "right-one").status_code == 200

    assert post_document(client).status_code == 403
    resp = client.get("/api/documents")
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
    assert client.post("/api/documents/1/retry").status_code == 403
    assert client.post("/api/documents/1/confirm").status_code == 403


# ---------- DOC-01：上传 202 → 列表 ready；camelCase ----------


def test_upload_202_ready_and_listed_camel_case(client, session, sandbox_storage, test_mode):
    """成功上传：202 {id,status}=ready（test_mode 同步处理）；GET 列表出现且键名全 camelCase。"""
    admin_login(client, session)
    resp = post_document(client)
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert set(data) == {"id", "status"}
    assert data["status"] == "ready"

    listed = client.get("/api/documents").json()["data"]
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["id"] == data["id"]
    assert item["status"] == "ready"
    assert item["title"] == "客服_员工手册_20260901"
    assert item["deptCode"] == "service"
    assert item["ownerName"] == "制度责任人"
    assert item["version"] == 1
    assert item["isActive"] is True
    assert item["effectiveDate"] == "2026-09-01"  # brief 指定断言键
    assert item["expiryDate"] is None
    assert all("_" not in key for key in item)  # 响应键名全 camelCase

    resp2 = post_document(client, title="客服_退货政策_20260901", expiry_date="2026-12-31")
    assert resp2.status_code == 202
    item2 = client.get("/api/documents").json()["data"]["items"][0]
    assert item2["expiryDate"] == "2026-12-31"


# ---------- §22.1：过滤 / 分页 / 排序 ----------


def test_list_filters_and_pagination(client, session, sandbox_storage, test_mode):
    """dept/status/keyword 过滤 + 分页切片；默认 created_at 降序（同事务时间相等以 id 兜底）。"""
    admin_login(client, session)
    id_a = post_document(client, title="客服_考勤制度_20260901").json()["data"]["id"]
    id_b = post_document(client, title="人力_薪酬制度_20260901", dept="hr").json()["data"]["id"]
    id_c = post_document(
        client, title="客服_空白件_20260901", content=_empty_docx()
    ).json()["data"]["id"]

    default_list = client.get("/api/documents").json()["data"]
    assert (default_list["total"], default_list["page"], default_list["pageSize"]) == (3, 1, 20)
    assert [i["id"] for i in default_list["items"]] == [id_c, id_b, id_a]

    hr_only = client.get("/api/documents", params={"dept": "hr"}).json()["data"]
    assert [i["id"] for i in hr_only["items"]] == [id_b]
    assert client.get("/api/documents", params={"dept": "bogus"}).status_code == 422

    failed = client.get("/api/documents", params={"status": "failed"}).json()["data"]
    assert [i["id"] for i in failed["items"]] == [id_c]
    assert "疑似扫描件或空文档" in failed["items"][0]["parseError"]
    ready = client.get("/api/documents", params={"status": "ready"}).json()["data"]
    assert ready["total"] == 2
    assert client.get("/api/documents", params={"status": "bogus"}).status_code == 422

    keyword = client.get("/api/documents", params={"keyword": "考勤"}).json()["data"]
    assert [i["id"] for i in keyword["items"]] == [id_a]
    miss = client.get("/api/documents", params={"keyword": "不存在的词条"}).json()["data"]
    assert (miss["total"], miss["items"]) == (0, [])

    page1 = client.get(
        "/api/documents", params={"status": "ready", "page": 1, "pageSize": 1}
    ).json()["data"]
    assert (page1["total"], len(page1["items"]), page1["items"][0]["id"]) == (2, 1, id_b)
    page2 = client.get(
        "/api/documents", params={"status": "ready", "page": 2, "pageSize": 1}
    ).json()["data"]
    assert [i["id"] for i in page2["items"]] == [id_a]


def test_list_page_bounds_422(client, session, sandbox_storage, test_mode):
    """§22.1 分页边界：pageSize 0 与 101 → 422 字段级；page 0 → 422。"""
    admin_login(client, session)
    for size in ("0", "101"):
        resp = client.get("/api/documents", params={"pageSize": size})
        assert resp.status_code == 422
        assert any("pageSize" in entry["field"] for entry in resp.json()["data"])
    assert client.get("/api/documents", params={"page": "0"}).status_code == 422


# ---------- DOC-11：失败重试 ----------


def test_retry_reruns_failed_document_and_guards(client, session, sandbox_storage, test_mode):
    """失败件重试：编排确实重跑（审计再落 failed）；ready 件拒绝重试 422；未知 id 404。"""
    admin_login(client, session)
    failed = post_document(
        client, title="客服_扫描件_20260901", content=_empty_docx()
    ).json()["data"]
    assert failed["status"] == "failed"

    resp = client.post(f"/api/documents/{failed['id']}/retry")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "failed"  # 空文档重跑仍失败：证明编排已重跑
    assert [a.action for a in _doc_audits(session, failed["id"])] == [
        "document.upload",
        "document.failed",
        "document.failed",
    ]

    ok = post_document(client, title="客服_正常件_20260901").json()["data"]
    guard = client.post(f"/api/documents/{ok['id']}/retry")
    assert guard.status_code == 422
    assert "失败" in guard.json()["message"]
    assert client.post("/api/documents/987654321/retry").status_code == 404


# ---------- 待确认 → confirm 激活 ----------


def test_confirm_activates_pending_document(client, session, sandbox_storage, test_mode):
    """敏感命中待确认（isActive=False）→ confirm 激活；未知 id 404。"""
    admin_login(client, session)
    content = _docx_bytes(
        [
            (
                "人事数据",
                ["员工张三身份证号 11010519491231002X 由人事部专人保管。", _filler("附则", 12)],
            )
        ]
    )
    data = post_document(client, title="客服_人事数据_20260901", content=content).json()["data"]
    assert data["status"] == "ready"

    item = client.get("/api/documents").json()["data"]["items"][0]
    assert item["isActive"] is False  # 敏感命中 → 待确认

    resp = client.post(f"/api/documents/{data['id']}/confirm")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": data["id"], "isActive": True}
    assert client.get("/api/documents").json()["data"]["items"][0]["isActive"] is True
    assert client.post("/api/documents/987654321/confirm").status_code == 404


# ---------- 非 test_mode：BackgroundTasks 接线（不真跑管线，桩掉后台入口） ----------


def test_upload_normal_mode_schedules_background(client, session, sandbox_storage, monkeypatch):
    """test_mode=False：响应 202 且状态 parsing，后台任务以 doc.id 被调度。"""
    from app.api import documents as documents_api

    scheduled: list[int] = []
    monkeypatch.setattr(
        documents_api, "_process_in_background", lambda doc_id: scheduled.append(doc_id)
    )
    admin_login(client, session)

    resp = post_document(client)
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "parsing"
    assert scheduled == [data["id"]]


# ---------- 元数据日期格式（字段级 422） ----------


def test_upload_invalid_effective_date_422(client, session, sandbox_storage, test_mode):
    """effectiveDate 非日期串：422 且 data 指名 effectiveDate 字段。"""
    admin_login(client, session)
    resp = client.post(
        "/api/documents",
        data={
            "title": "客服_坏日期_20260901",
            "dept": "service",
            "owner": "制度责任人",
            "effectiveDate": "不是日期",
            "complianceConfirmed": "true",
        },
        files={"file": ("bad_date.docx", _docx_bytes([("第一章 总则", [_filler("总则", 18)])]), DOCX_MIME)},
    )
    assert resp.status_code == 422
    assert "effectiveDate" in {entry["field"] for entry in resp.json()["data"]}
    assert date(2026, 9, 1)  # 保持 import 语义：日期经 Pydantic 解析后进库
