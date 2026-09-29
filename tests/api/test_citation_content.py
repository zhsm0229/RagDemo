r"""引用正文端点测试（T8 评审裁决 Rider：前端"查看原文"数据契约）。

覆盖：doc 分支六键全 {refType, refId, content, title, page, titlePath}（title 取
文档规范标题）；qa 分支 answer/question 映射 content/title、page/titlePath 恒空；
部门隔离 0 容忍（红线①）——跨部门（hr 查 service 块、service 查 hr 问答）与
不存在 id 一律 404/3003 统一文案"引用不存在或无权访问"（不区分不存在与无权，
不给探测空间）；全员部门内容对任意部门用户可见（service 用户读 all 块、hr 用户
读 all 问答，双向）；检索面外的内容不得经引用读出（软删块 / 停用问答 / 归档
文档 → 404，与检索 F-502 的存活闸同口径）；refType 枚举外 422；未登录 401。
种子一律 ORM 直插（同 test_admin_retrieval_test 口径）。
"""

import hashlib
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_NOT_FOUND, ERR_PARAM_INVALID, ERR_UNAUTHENTICATED
from app.core.llm import StubEmbedder
from app.db.models import Chunk, Department, Document, QaPair, User

_MSG_NOT_FOUND = "引用不存在或无权访问"


# ---------- 构造与登录辅助 ----------


def make_user(session: Session, username: str, password: str, dept_code: str) -> User:
    """直插指定部门的 member 测试用户（业务端点登录即可，非 admin）。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == dept_code)).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role="member",
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _dept_ids(session: Session) -> dict[str, int]:
    """种子三部门 code → id（seed.py：service/hr/all）。"""
    return dict(session.execute(select(Department.code, Department.id)).all())


def _admin_id(session: Session) -> int:
    return session.execute(select(User.id).where(User.username == "admin")).scalar_one()


def _add_document(session: Session, dept_id: int, title: str, **extra) -> Document:
    """就绪且活跃的种子文档（检索面内的可引用样本）。"""
    doc = Document(
        title=title,
        dept_id=dept_id,
        original_filename=f"{title}.docx",
        file_path=f"seed/{title}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=hashlib.sha256(title.encode("utf-8")).hexdigest(),
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        uploaded_by=_admin_id(session),
        **extra,
    )
    session.add(doc)
    session.flush()
    return doc


def _add_chunk(
    session: Session,
    doc: Document,
    content: str,
    *,
    title_path: str,
    page_no: int | None = None,
    **extra,
) -> Chunk:
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=title_path,
        content=content,
        content_chars=len(content),
        position=0,
        page_no=page_no,
        embedding=StubEmbedder().embed([content])[0],
        **extra,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _add_qa(session: Session, dept_id: int, question: str, answer: str, **extra) -> QaPair:
    fields = {
        "dept_id": dept_id,
        "question": question,
        "answer": answer,
        "embedding": StubEmbedder().embed([question])[0],
        "status": "enabled",
        "created_by": _admin_id(session),
    }
    qa = QaPair(**{**fields, **extra})  # extra 覆盖默认值（如 status="disabled"）
    session.add(qa)
    session.flush()
    return qa


def _seed(session: Session) -> dict:
    """三部门内容种子：service/all 各 doc+chunk，service/all/hr 各一问答对。"""
    ids = _dept_ids(session)
    service_doc = _add_document(session, ids["service"], "客服_差旅制度")
    all_doc = _add_document(session, ids["all"], "全员_安全须知")
    return {
        "service_chunk": _add_chunk(
            session,
            service_doc,
            "差旅报销正文原文",
            title_path="第一章 总则",
            page_no=3,
        ),
        "all_chunk": _add_chunk(
            session,
            all_doc,
            "安全须知正文原文",
            title_path="第二章 保密",
            page_no=None,
        ),
        "service_qa": _add_qa(
            session, ids["service"], "差旅报销标准是什么", "差旅报销标准答案原文"
        ),
        "all_qa": _add_qa(session, ids["all"], "安全须知是什么", "安全须知答案原文"),
        "hr_qa": _add_qa(session, ids["hr"], "hr 内部问答", "hr 内部答案原文"),
    }


def _get(client: TestClient, ref_type: str, ref_id: int):
    return client.get("/api/citations/content", params={"refType": ref_type, "refId": ref_id})


# ---------- 命中：两分支六键全 ----------


def test_doc_citation_returns_full_payload(client, session) -> None:
    """doc 引用：六键全，title 取文档规范标题，page/titlePath 取块定位信息。"""
    chunk = _seed(session)["service_chunk"]
    make_user(session, "serviceuser", "right-one", "service")
    assert login(client, "serviceuser", "right-one").status_code == 200
    resp = _get(client, "doc", chunk.id)
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "refType": "doc",
        "refId": chunk.id,
        "content": "差旅报销正文原文",
        "title": "客服_差旅制度",
        "page": 3,
        "titlePath": "第一章 总则",
    }


def test_qa_citation_maps_answer_and_question(client, session) -> None:
    """qa 引用：answer 映射 content、question 映射 title，page/titlePath 恒空。"""
    qa = _seed(session)["service_qa"]
    make_user(session, "serviceuser", "right-one", "service")
    assert login(client, "serviceuser", "right-one").status_code == 200
    resp = _get(client, "qa", qa.id)
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "refType": "qa",
        "refId": qa.id,
        "content": "差旅报销标准答案原文",
        "title": "差旅报销标准是什么",
        "page": None,
        "titlePath": None,
    }


# ---------- 全员部门双向可见 ----------


def test_all_dept_content_visible_to_any_dept_user(client, session) -> None:
    """全员部门内容双向可见：service 用户读 all 块、hr 用户读 all 问答均 200。"""
    seeds = _seed(session)
    make_user(session, "serviceuser", "right-one", "service")
    make_user(session, "hruser", "right-one", "hr")
    assert login(client, "serviceuser", "right-one").status_code == 200
    resp = _get(client, "doc", seeds["all_chunk"].id)
    assert resp.status_code == 200
    assert resp.json()["data"]["content"] == "安全须知正文原文"
    assert login(client, "hruser", "right-one").status_code == 200
    resp = _get(client, "qa", seeds["all_qa"].id)
    assert resp.status_code == 200
    assert resp.json()["data"]["content"] == "安全须知答案原文"


# ---------- 部门隔离 0 容忍：越权与不存在同文案 404/3003 ----------


def test_cross_dept_citation_rejected_with_unified_message(client, session) -> None:
    """串库 0 容忍：hr 查 service 块、service 查 hr 问答 → 404/3003 统一文案。"""
    seeds = _seed(session)
    make_user(session, "serviceuser", "right-one", "service")
    make_user(session, "hruser", "right-one", "hr")
    assert login(client, "hruser", "right-one").status_code == 200
    resp = _get(client, "doc", seeds["service_chunk"].id)
    assert resp.status_code == 404
    assert resp.json() == {"code": ERR_NOT_FOUND, "message": _MSG_NOT_FOUND, "data": None}
    assert login(client, "serviceuser", "right-one").status_code == 200
    resp = _get(client, "qa", seeds["hr_qa"].id)
    assert resp.status_code == 404
    assert resp.json() == {"code": ERR_NOT_FOUND, "message": _MSG_NOT_FOUND, "data": None}


def test_missing_citation_404_same_message(client, session) -> None:
    """不存在的 refId：404/3003 同文案（与无权访问不可区分，不给探测空间）。"""
    _seed(session)
    make_user(session, "serviceuser", "right-one", "service")
    assert login(client, "serviceuser", "right-one").status_code == 200
    resp = _get(client, "doc", 999999)
    assert resp.status_code == 404
    assert resp.json() == {"code": ERR_NOT_FOUND, "message": _MSG_NOT_FOUND, "data": None}


# ---------- 检索面外内容不可经引用读出 ----------


def test_content_outside_retrieval_surface_not_readable(client, session) -> None:
    """软删块 / 停用问答 / 归档文档：检索已不可见的内容引用读出 → 404。"""
    ids = _dept_ids(session)
    seeds = _seed(session)
    dead_chunk = _add_chunk(
        session,
        _add_document(session, ids["service"], "客服_已撤回制度"),
        "已撤回正文",
        title_path="第一章 撤回",
        is_active=False,
    )
    disabled_qa = _add_qa(
        session, ids["service"], "停用问答是什么", "停用问答答案", status="disabled"
    )
    archived_chunk = _add_chunk(
        session,
        _add_document(session, ids["service"], "客服_归档制度", is_active=False),
        "归档正文",
        title_path="第一章 归档",
    )
    make_user(session, "serviceuser", "right-one", "service")
    assert login(client, "serviceuser", "right-one").status_code == 200
    for ref_type, ref_id in (
        ("doc", dead_chunk.id),
        ("qa", disabled_qa.id),
        ("doc", archived_chunk.id),
        ("qa", seeds["service_qa"].id),  # 基线对照：存活样本仍可读
    ):
        resp = _get(client, ref_type, ref_id)
        expected_status = 200 if ref_id == seeds["service_qa"].id else 404
        assert resp.status_code == expected_status, (ref_type, ref_id)


# ---------- 参数门与登录门 ----------


def test_ref_type_outside_enum_422(client, session) -> None:
    """refType 枚举外：422/1003（FastAPI Literal 校验，非业务 404）。"""
    make_user(session, "serviceuser", "right-one", "service")
    assert login(client, "serviceuser", "right-one").status_code == 200
    resp = _get(client, "video", 1)
    assert resp.status_code == 422
    assert resp.json()["code"] == ERR_PARAM_INVALID


def test_unauthenticated_401(client, session) -> None:
    """未登录：401/2003（业务端点登录门，无部门过滤先行放行之理）。"""
    _seed(session)
    resp = _get(client, "doc", 1)
    assert resp.status_code == 401
    assert resp.json()["code"] == ERR_UNAUTHENTICATED
