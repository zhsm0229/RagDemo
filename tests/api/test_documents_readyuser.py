r"""documents 三端点首登改密门回归（findings I12）：/file、/preview、/images/{n}
依赖 CurrentUser → ReadyUser，must_change_password=true 用户访问一律 403/2004，
与 citations/conversations/chat 的 AUTH-04 口径一致。

文档直插（status=ready + is_active + 全员部门 + 盘上文件齐全）：正控先证普通
用户三端点 200（文档确实可看），再证改密门用户 403——403 只能来自改密门而非
存活/隔离闸；改密落库后同请求转 200，读回闭环。
"""

import hashlib
import os
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import ERR_FORBIDDEN
from app.db.models import Department, Document, DocumentImage, User
from tests.conftest import login_as

_PDF_BYTES = b"%PDF-1.4\n%readyuser-gate-passthrough\n"
_PNG_BYTES = b"\x89PNG\r\n\x1a\nreadyuser-gate-image-bytes"
_DOC_TITLE = "口径闸-ReadyUser回归"


@pytest.fixture()
def sandbox_storage(tmp_path, monkeypatch):
    """STORAGE_DIR 指向 tmp_path（口径同 test_document_images 的本地夹具）。"""
    saved = os.environ.get("STORAGE_DIR")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    if saved is None:
        monkeypatch.delenv("STORAGE_DIR", raising=False)
    else:
        monkeypatch.setenv("STORAGE_DIR", saved)
    get_settings.cache_clear()


def _make_user(session, username: str, *, role: str = "member", must_change: bool = False) -> User:
    """直插测试用户（test_auth.make_user 同口径）：must_change 显式给值防漏。"""
    dept = session.scalars(select(Department).where(Department.code == "all")).one()
    from app.core.security import hash_password

    user = User(
        username=username,
        password_hash=hash_password("ReadyUser#2026"),
        dept_id=dept.id,
        role=role,
        must_change_password=must_change,
    )
    session.add(user)
    session.flush()
    return user


def _make_ready_doc(session, uploader_id: int) -> Document:
    """直插 ready 文档 + 内嵌图片登记，盘上落齐原文件与图片（三端点均可达 200）。"""
    content_hash = hashlib.sha256(_DOC_TITLE.encode()).hexdigest()
    doc = Document(
        title=_DOC_TITLE,
        dept_id=session.scalar(select(Department.id).where(Department.code == "all")),
        original_filename=f"{_DOC_TITLE}.pdf",
        file_path=f"readyuser/{content_hash}.pdf",
        mime="application/pdf",
        size_bytes=len(_PDF_BYTES),
        content_hash=content_hash,
        effective_date=date(2026, 1, 1),
        owner_name="口径责任人",
        status="ready",
        uploaded_by=uploader_id,
    )
    session.add(doc)
    session.flush()  # 先取 id 再登记图片与落盘
    image = DocumentImage(
        document_id=doc.id,
        image_index=1,
        file_path=f"images/{doc.id}/img_1.png",
        mime="image/png",
    )
    session.add(image)
    storage = Path(get_settings().storage_dir)
    (storage / doc.file_path).parent.mkdir(parents=True, exist_ok=True)
    (storage / doc.file_path).write_bytes(_PDF_BYTES)
    (storage / image.file_path).parent.mkdir(parents=True, exist_ok=True)
    (storage / image.file_path).write_bytes(_PNG_BYTES)
    session.flush()
    return doc


def _assert_three_endpoints(client, doc_id: int, expected_status: int) -> None:
    """三端点统一断言：/preview、/file、/images/1 同一鉴权口径。"""
    for path in (
        f"/api/documents/{doc_id}/preview",
        f"/api/documents/{doc_id}/file",
        f"/api/documents/{doc_id}/images/1",
    ):
        resp = client.get(path)
        assert resp.status_code == expected_status, (path, resp.status_code, resp.text)


def test_documents_endpoints_block_first_login_user(client, session, sandbox_storage) -> None:
    """首登未改密用户访问 /file、/preview、/images 一律 403/2004；改密后放行。"""
    admin = _make_user(session, "gate_admin", role="admin")
    member = _make_user(session, "gate_member", must_change=True)  # 首登未改密
    doc = _make_ready_doc(session, uploader_id=admin.id)

    # 正控：已过改密门的用户三端点 200——文档本身可看，排他证 403 来自改密门
    assert login_as(client, "gate_admin", "ReadyUser#2026").status_code == 200
    _assert_three_endpoints(client, doc.id, expected_status=200)

    # 改密门用户：登录可成功，但三端点 403/2004 提示先改密
    assert login_as(client, "gate_member", "ReadyUser#2026").status_code == 200
    for path in (
        f"/api/documents/{doc.id}/preview",
        f"/api/documents/{doc.id}/file",
        f"/api/documents/{doc.id}/images/1",
    ):
        resp = client.get(path)
        assert (resp.status_code, resp.json()["code"]) == (403, ERR_FORBIDDEN), path
        assert "修改密码" in resp.json()["message"], path

    # 读回闭环：改密落库后同请求转 200（403 确系 must_change_password 门所拦）
    member.must_change_password = False
    session.commit()
    session.refresh(member)
    assert member.must_change_password is False
    _assert_three_endpoints(client, doc.id, expected_status=200)
