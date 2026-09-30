r"""零召回块清单 API 测试（W5 Task 4，F-1011）：GET /api/admin/zero-recall-chunks。

口径（与 F-1009 零召回文档榜同一口径的**块级一面**，见 services.overview_views
互指 docstring）：chunks.is_active 且（last_hit_at IS NULL 或 last_hit_at < now-days）
且所属文档 is_active+ready——文档级是"任一块近期命中即整篇不算"的反连接，块级是
逐块判定；文档闸（is_active+ready，不看 deleted_at）与文档级逐字一致。时间基准
取注入时钟（FakeClock 恰 30 天双例），窗口起点 Python 侧折算后参数绑定；排序
created_at 降序（同刻 id 降序兜底）；响应 {items,total,page,pageSize}（§3.5）。
断言整包精确对比；种子一律 ORM 直插（聚合不涉向量运算，零向量直插满足 NOT NULL，
W3 既有手法）。
"""

import datetime as dt
import hashlib

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID, ERR_UNAUTHENTICATED
from app.db.models import Chunk, Department, Document, User
from tests.conftest import login_as

_CHUNK_VEC = [0.0] * 1024  # 清单判定不涉向量运算：零向量直插满足 NOT NULL（W3 手法）
_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_LIST_URL = "/api/admin/zero-recall-chunks"


def make_user(session: Session, username: str, dept_code: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），指定部门编码与角色。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == dept_code)).one()
    user = User(
        username=username,
        password_hash=hash_password("right-one"),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _as_admin(client: TestClient, session: Session) -> None:
    """建 admin（免首登改密）并登录；每用例独立建号避免用户名冲突。"""
    make_user(session, "zrc_admin", "all", role="admin")
    assert login_as(client, "zrc_admin", "right-one").status_code == 200


def _make_doc(
    session: Session,
    title: str,
    dept_code: str,
    *,
    status: str = "ready",
    is_active: bool = True,
) -> Document:
    """直插文档（content_hash 以标题派生保唯一——软删不占 hash 的部分唯一索引）。"""
    admin_id = session.scalar(select(User.id).where(User.username == "zrc_admin"))
    dept_id = session.scalar(select(Department.id).where(Department.code == dept_code))
    doc = Document(
        title=title,
        dept_id=dept_id,
        original_filename=f"{title}.docx",
        file_path=f"t4/{hashlib.sha256(title.encode()).hexdigest()}.docx",
        mime=_DOC_MIME,
        size_bytes=16,
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
        effective_date=dt.date(2026, 1, 1),
        owner_name="T4责任人",
        status=status,
        is_active=is_active,
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    return doc


def _add_chunk(
    session: Session,
    doc: Document,
    *,
    last_hit_at: dt.datetime | None = None,
    hit_count: int = 0,
    is_active: bool = True,
    created_at: dt.datetime | None = None,
    content: str = "零召回块正文",
    title_path: str | None = None,
) -> Chunk:
    """直插块：last_hit_at 为 None 即从未被召回（零召回块级口径的入列样本）。"""
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=title_path if title_path is not None else doc.title,
        content=content,
        content_chars=len(content),
        position=0,
        embedding=_CHUNK_VEC,
        hit_count=hit_count,
        last_hit_at=last_hit_at,
        is_active=is_active,
    )
    if created_at is not None:
        chunk.created_at = created_at
    session.add(chunk)
    session.flush()
    return chunk


def _get(client: TestClient, params: dict | None = None):
    resp = client.get(_LIST_URL, params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["code"], body["message"]) == (0, "ok")
    return body["data"]


def _item(chunk: Chunk, doc: Document, dept_code: str) -> dict:
    """期望 items 项（整包精确对比基准）：contentPreview=content（短文本不截断）。"""
    return {
        "chunkId": chunk.id,
        "documentTitle": doc.title,
        "deptCode": dept_code,
        "titlePath": chunk.title_path,
        "contentPreview": chunk.content,
        "createdAt": chunk.created_at.isoformat(),
    }


# ---------- ① 口径整包读回：块级判定 + 文档闸 + 排序 ----------


def test_零召回块清单_口径整包读回与排序(client, session) -> None:
    """① 混合数据集（时钟 2026-10-01T08:00Z，days=30 起点 09-01T08:00）：从未召回、
    陈旧召回（08-01 < 起点）入列；近期召回（09-28）、停用块、停用文档块、未就绪
    文档块不入列；items 按 created_at 降序，外壳 {items,total,page,pageSize} 回显。"""
    _as_admin(client, session)
    doc_svc = _make_doc(session, "T4Z-客服手册", "service")
    doc_hr = _make_doc(session, "T4Z-考勤制度", "hr")
    doc_inactive = _make_doc(session, "T4Z-待确认", "service", is_active=False)
    doc_pending = _make_doc(session, "T4Z-解析中", "all", status="pending")
    never = _add_chunk(
        session,
        doc_svc,
        created_at=dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC),
        title_path="T4Z-客服手册>第一章",
    )  # 从未召回：入列
    _add_chunk(
        session,
        doc_svc,
        last_hit_at=dt.datetime(2026, 9, 28, tzinfo=dt.UTC),
        hit_count=3,
        created_at=dt.datetime(2026, 9, 19, tzinfo=dt.UTC),
    )  # 近 30 天命中过：不入列
    stale = _add_chunk(
        session,
        doc_svc,
        last_hit_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        hit_count=9,
        created_at=dt.datetime(2026, 8, 15, tzinfo=dt.UTC),
    )  # 仅剩窗口前的陈旧召回：入列
    _add_chunk(
        session, doc_hr, is_active=False, created_at=dt.datetime(2026, 8, 10, tzinfo=dt.UTC)
    )  # 停用块：不入列
    _add_chunk(
        session, doc_inactive, created_at=dt.datetime(2026, 8, 9, tzinfo=dt.UTC)
    )  # 停用文档（待确认）的块：不入列（文档闸与 F-1009 一致）
    _add_chunk(
        session, doc_pending, created_at=dt.datetime(2026, 8, 8, tzinfo=dt.UTC)
    )  # 未就绪文档的块：不入列

    data = _get(client)
    assert data["total"] == 2
    assert data["page"] == 1 and data["pageSize"] == 20
    assert data["items"] == [
        _item(never, doc_svc, "service"),
        _item(stale, doc_svc, "service"),
    ]


# ---------- ② contentPreview 截 50 字 ----------


def test_内容预览截断50字(client, session) -> None:
    """② 超 50 字内容只出前 50 字（前端预览列不整段拖表）；短文本原样不补位。"""
    _as_admin(client, session)
    doc = _make_doc(session, "T4Z-预览样本", "hr")
    long_content = "长" * 80
    long_chunk = _add_chunk(session, doc, content=long_content)
    short_chunk = _add_chunk(session, doc, content="短文本")

    items = {item["chunkId"]: item for item in _get(client)["items"]}
    assert items[long_chunk.id]["contentPreview"] == "长" * 50
    assert len(items[long_chunk.id]["contentPreview"]) == 50
    assert items[short_chunk.id]["contentPreview"] == "短文本"


# ---------- ③ 恰 30 天边界：FakeClock 前拨构造清单两侧 ----------


def test_恰好30天边界_fakeclock前拨进清单(client, session, fake_clock) -> None:
    """③ 恰在 30 天线的 last_hit_at 即算近期召回（不入列）；时钟前拨 1 秒后该块
    落入"最近一次召回早于窗口起点"一侧 → 入列（与 F-1009 文档级边界判定同款）。"""
    fake_clock.set("2026-10-01T00:00:00+00:00")
    _as_admin(client, session)
    doc = _make_doc(session, "T4Z-边界样本", "hr")
    edge = _add_chunk(session, doc, last_hit_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))

    assert _get(client, {"days": 30})["items"] == []  # 恰压 30 天线：算近期，不入列

    fake_clock.advance(seconds=1)  # 前拨 1 秒：last_hit_at 严格早于 now-30d
    listed = _get(client, {"days": 30})["items"]
    assert [item["chunkId"] for item in listed] == [edge.id]


def test_从未召回块恒在列且days窗口收放生效(client, session, fake_clock) -> None:
    """④ last_hit_at IS NULL 不受窗口影响：days=1/365 恒入列；days 同时真实收放
    窗口——3 天前召回过的块 days=1 入列（召回早于窗口起点）、days=365 不入列。"""
    fake_clock.set("2026-10-01T08:00:00+00:00")
    _as_admin(client, session)
    doc = _make_doc(session, "T4Z-窗口样本", "service")
    never = _add_chunk(session, doc, created_at=dt.datetime(2026, 9, 19, tzinfo=dt.UTC))
    recent = _add_chunk(
        session,
        doc,
        last_hit_at=dt.datetime(2026, 9, 28, tzinfo=dt.UTC),
        hit_count=1,
        created_at=dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
    )  # 3 天前召回

    narrow = _get(client, {"days": 1})
    assert [item["chunkId"] for item in narrow["items"]] == [recent.id, never.id]  # created 降序
    wide = _get(client, {"days": 365})
    assert [item["chunkId"] for item in wide["items"]] == [never.id]  # 近期召回被 365 天窗口覆盖


# ---------- ⑤ days/page/pageSize 参数校验 ----------


def test_参数校验_days越界与非数字_422(client, session) -> None:
    """⑤ days=0/366/x → 422/1003 字段级（Query ge/le 边界）；page=0、pageSize=101
    同样 422/1003（§3.5 列表分页口径）。"""
    _as_admin(client, session)
    for params in (
        {"days": "0"},
        {"days": "366"},
        {"days": "x"},
        {"page": "0"},
        {"pageSize": "101"},
    ):
        resp = client.get(_LIST_URL, params=params)
        assert resp.status_code == 422, params
        assert resp.json()["code"] == ERR_PARAM_INVALID, params


def test_分页与同刻created_at按id降序兜底(client, session) -> None:
    """⑥ 分页 {items,total,page,pageSize} 回显：pageSize=2 翻两页取尽 3 块、越界页
    空 items 且 total 不变；同刻 created_at 按 id 降序兜底（后插的在前，行次序确定）。"""
    _as_admin(client, session)
    doc = _make_doc(session, "T4Z-分页样本", "hr")
    same_at = dt.datetime(2026, 8, 8, 8, 8, tzinfo=dt.UTC)
    first = _add_chunk(session, doc, created_at=same_at - dt.timedelta(days=1))
    second = _add_chunk(session, doc, created_at=same_at)
    third = _add_chunk(session, doc, created_at=same_at)  # 与 second 同刻：id 降序 third 在前

    page1 = _get(client, {"page": 1, "pageSize": 2})
    assert (page1["total"], page1["page"], page1["pageSize"]) == (3, 1, 2)
    assert [item["chunkId"] for item in page1["items"]] == [third.id, second.id]  # 同刻 id 降序
    page2 = _get(client, {"page": 2, "pageSize": 2})
    assert (page2["page"], page2["pageSize"]) == (2, 2)
    assert [item["chunkId"] for item in page2["items"]] == [first.id]
    empty = _get(client, {"page": 3, "pageSize": 2})
    assert empty["items"] == [] and empty["total"] == 3  # 越界空页，total 仍为全量


# ---------- ⑦ 权限门（§5.5） ----------


def test_匿名与成员角色访问清单被拒(client, session) -> None:
    """⑦ 未登录 401/2003；member 已登录 403/2004（admin-only 运营下钻工具）。"""
    resp = client.get(_LIST_URL)
    assert (resp.status_code, resp.json()["code"]) == (401, ERR_UNAUTHENTICATED)

    make_user(session, "zrc_member", "service", role="member")
    assert login_as(client, "zrc_member", "right-one").status_code == 200
    resp = client.get(_LIST_URL)
    assert (resp.status_code, resp.json()["code"]) == (403, ERR_FORBIDDEN)
