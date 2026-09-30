r"""管理端问答对管理 API 测试（F-402 列表/停用 + QA-06 编辑 + QA-07 软删）。

brief 断言 ①-⑥ 全覆盖：①列表分页/created_at 降序/DTO 键集/过滤（deptId/status/
keyword）与 keyword ILIKE 转义（`%`/`_` 按字面匹配不展开通配，§22）；②停用后检索
不命中（QA-05 走 API 层复验，恢复启用重新命中）；③删除后列表不见且检索不命中、
行仍在库（软删可追溯，QA-07）；④编辑后新答案生效且按新问题重新向量化（QA-06，
改问题后旧问题向量不再命中）；⑤toggle/update/delete 三操作审计读回（detail 逐键
断言，摘要截断 50 字）；⑥三操作成功响应后问答精确缓存整清（陈旧答案不靠 TTL）。
另固化：编辑校验同导入规则（问题 1-2000/答案 1-10000，中文 422）、未知/已删 id
404、member 403 门、列表状态枚举外 422。

qa_client 固定 test_mode 先于 lifespan 就绪：编辑重嵌取 app.state.chat_deps 的
StubEmbedder（与检索同指纹空间），match_qa 断言与导入同一向量口径。
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID
from app.core.llm import StubEmbedder
from app.db.models import AuditLog, Department, QaPair, User
from app.services.retrieval import match_qa
from tests.conftest import login_as

# ---------- 构造与登录辅助（口径同 tests/api/test_admin_qa_import.py） ----------


def make_user(session, username: str, password: str, role: str) -> User:
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


def admin_login(client, session) -> int:
    """创建并登录 admin（cookie 由 client 保持），返回其 user id 供审计断言。"""
    user = make_user(session, "qamadmin", "right-one", role="admin")
    assert login_as(client, "qamadmin", "right-one").status_code == 200
    return user.id


def _seed_qa(
    session,
    question: str,
    answer: str,
    embedder: StubEmbedder,
    *,
    status: str = "enabled",
    dept_code: str = "service",
    created_at: datetime | None = None,
) -> QaPair:
    """ORM 直插问答对（向量用 StubEmbedder 生成，与检索同指纹空间），flush 拿 id。"""
    dept = session.scalars(select(Department).where(Department.code == dept_code)).first()
    author = session.scalars(select(User).where(User.username == "admin")).first()
    qa = QaPair(
        dept_id=dept.id,
        question=question,
        answer=answer,
        embedding=embedder.embed([question])[0],
        status=status,
        created_by=author.id,
        created_at=created_at,
    )
    session.add(qa)
    session.flush()
    return qa


def _dept_id(session, code: str) -> int:
    return session.scalars(select(Department.id).where(Department.code == code)).one()


def _qa_view(session, qa: QaPair) -> list[int]:
    """F-502 检索视角：dept_id IN (所属部门, all)。"""
    return [qa.dept_id, _dept_id(session, "all")]


@pytest.fixture()
def qa_client(test_mode, client):
    """test_mode 先于 lifespan 就绪（fixture 参数序即装配序）：chat_deps 以 Stub 三件套
    装配挂 app.state，编辑端点拿到与检索同一 StubEmbedder，缓存断言走同一实例。"""
    return client


# ---------- ① 列表：分页 / created_at 降序 / DTO 键集 ----------


def test_列表分页与created_at降序及DTO键集(qa_client, session):
    """① 3 行数据 pageSize=2：page1 取最新 2 行、page2 取最旧 1 行、total=3；
    列表项键集恰为 {id,deptId,deptCode,question,answer,status,batchNo,createdAt,isActive}。"""
    embedder = StubEmbedder()
    admin_login(qa_client, session)
    for seq, text_ in enumerate(["问题甲", "问题乙", "问题丙"], start=1):
        _seed_qa(
            session,
            text_,
            f"答案{text_}",
            embedder,
            created_at=datetime(2026, 9, seq, tzinfo=UTC),
        )

    page1 = qa_client.get("/api/admin/qa-pairs", params={"page": 1, "pageSize": 2})
    assert page1.status_code == 200
    data = page1.json()["data"]
    assert set(data) == {"items", "total", "page", "pageSize"}
    assert (data["total"], data["page"], data["pageSize"]) == (3, 1, 2)
    assert [item["question"] for item in data["items"]] == ["问题丙", "问题乙"]  # 新→旧
    assert set(data["items"][0]) == {
        "id",
        "deptId",
        "deptCode",
        "question",
        "answer",
        "status",
        "batchNo",
        "createdAt",
        "isActive",
    }
    assert data["items"][0]["isActive"] is True and data["items"][0]["status"] == "enabled"

    page2 = qa_client.get("/api/admin/qa-pairs", params={"page": 2, "pageSize": 2})
    assert [item["question"] for item in page2.json()["data"]["items"]] == ["问题甲"]


# ---------- ① 列表：deptId/status/keyword 过滤与 ILIKE 转义 ----------


def test_列表过滤_部门状态关键字且百分号下划线按字面匹配(qa_client, session):
    """① keyword ILIKE 转义（§22）：`折扣率100%` 不命中「100X」行、`上限_万` 不命中
    「上限9万」行——通配符按字面匹配；deptId/status 过滤与非法状态 422 一并固化。"""
    embedder = StubEmbedder()
    admin_login(qa_client, session)
    a = _seed_qa(session, "客服折扣率100%规则问题", "答案A", embedder, dept_code="service")
    b = _seed_qa(
        session, "客服折扣率100X规则问题", "答案B", embedder, dept_code="service", status="disabled"
    )
    c = _seed_qa(session, "报销上限_万以内问题", "答案C", embedder, dept_code="hr")
    d = _seed_qa(session, "报销上限9万以内问题", "答案D", embedder, dept_code="hr")

    by_pct = qa_client.get("/api/admin/qa-pairs", params={"keyword": "折扣率100%"}).json()["data"]
    assert [item["id"] for item in by_pct["items"]] == [a.id]  # % 若展开为通配 B 也会命中
    by_underline = qa_client.get("/api/admin/qa-pairs", params={"keyword": "上限_万"}).json()[
        "data"
    ]
    assert [item["id"] for item in by_underline["items"]] == [c.id]  # _ 若展开为通配 D 也会命中
    by_answer = qa_client.get("/api/admin/qa-pairs", params={"keyword": "答案C"}).json()["data"]
    assert [item["id"] for item in by_answer["items"]] == [c.id]  # keyword 覆盖 answer 列

    by_dept = qa_client.get("/api/admin/qa-pairs", params={"deptId": a.dept_id}).json()["data"]
    assert {item["id"] for item in by_dept["items"]} == {a.id, b.id}
    by_status = qa_client.get("/api/admin/qa-pairs", params={"status": "disabled"}).json()["data"]
    assert [item["id"] for item in by_status["items"]] == [b.id]
    by_dept_status = qa_client.get(
        "/api/admin/qa-pairs", params={"deptId": c.dept_id, "status": "enabled"}
    ).json()["data"]
    assert {item["id"] for item in by_dept_status["items"]} == {c.id, d.id}

    bad = qa_client.get("/api/admin/qa-pairs", params={"status": "off"})
    assert bad.status_code == 422
    assert bad.json()["code"] == ERR_PARAM_INVALID


# ---------- ① 列表：DTO deptCode 值正确（join departments，非种子序推导） ----------


def test_列表DTO含deptCode且按部门行取值正确(qa_client, session):
    """① 列表项 deptCode 逐行取库内编码：三部门各一行，deptCode 与 deptId 同源一致
    （join departments 取 code，前端不再依赖种子序 id 推导部门名）。"""
    embedder = StubEmbedder()
    admin_login(qa_client, session)
    seeded = [
        (_seed_qa(session, "客服部门问题", "答案S", embedder, dept_code="service"), "service"),
        (_seed_qa(session, "人力部门问题", "答案H", embedder, dept_code="hr"), "hr"),
        (_seed_qa(session, "全员部门问题", "答案A", embedder, dept_code="all"), "all"),
    ]
    data = qa_client.get("/api/admin/qa-pairs", params={"page": 1, "pageSize": 20}).json()["data"]
    by_id = {item["id"]: item for item in data["items"]}
    assert len(by_id) == 3
    for qa, code in seeded:
        assert by_id[qa.id]["deptCode"] == code
        assert by_id[qa.id]["deptId"] == qa.dept_id  # 与 deptId 同行同源


# ---------- ② 停用后检索不命中（QA-05 API 层复验） ----------


def test_停用问答后检索不命中_恢复启用重新命中(qa_client, session):
    """② PATCH status=disabled → 库内 status 翻转、match_qa 不命中（QA-05）；恢复
    enabled 重新命中同一答案；非法状态 422；未知 id 404。"""
    embedder = StubEmbedder()
    admin_login(qa_client, session)
    qa = _seed_qa(session, "年假如何申请", "OA 提交年假申请单", embedder)
    view = _qa_view(session, qa)
    hit_before = match_qa(session, embedder.embed(["年假如何申请"])[0], view, 0.85)
    assert hit_before is not None and hit_before.qa_id == qa.id

    bad = qa_client.patch(f"/api/admin/qa-pairs/{qa.id}/status", json={"status": "off"})
    assert bad.status_code == 422
    assert bad.json()["code"] == ERR_PARAM_INVALID
    missing = qa_client.patch("/api/admin/qa-pairs/999999/status", json={"status": "disabled"})
    assert missing.status_code == 404

    resp = qa_client.patch(f"/api/admin/qa-pairs/{qa.id}/status", json={"status": "disabled"})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": qa.id, "status": "disabled"}
    assert session.scalar(select(QaPair.status).where(QaPair.id == qa.id)) == "disabled"
    assert match_qa(session, embedder.embed(["年假如何申请"])[0], view, 0.85) is None

    restore = qa_client.patch(f"/api/admin/qa-pairs/{qa.id}/status", json={"status": "enabled"})
    assert restore.status_code == 200
    hit = match_qa(session, embedder.embed(["年假如何申请"])[0], view, 0.85)
    assert hit is not None and (hit.qa_id, hit.answer) == (qa.id, "OA 提交年假申请单")


# ---------- ③ 删除后列表不见且检索不命中（QA-07 软删） ----------


def test_删除问答后_列表不见检索不命中且行仍在库(qa_client, session):
    """③ DELETE → 软删（is_active=false 行保留可追溯）：列表不含该行、match_qa 不命中、
    重复删除 404、未知 id 404；未删行不受影响。"""
    embedder = StubEmbedder()
    admin_login(qa_client, session)
    doomed = _seed_qa(session, "班车路线有哪些", "A 线环城，B 线直达园区", embedder)
    kept = _seed_qa(session, "工卡丢失怎么补办", "到行政前台补办", embedder)
    view = _qa_view(session, doomed)

    missing = qa_client.delete("/api/admin/qa-pairs/999999")
    assert missing.status_code == 404

    resp = qa_client.delete(f"/api/admin/qa-pairs/{doomed.id}")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": doomed.id, "isActive": False}

    row_active = session.execute(
        text("SELECT is_active FROM qa_pairs WHERE id = :i"), {"i": doomed.id}
    ).scalar_one()
    assert row_active is False  # 软删：行仍在库
    data = qa_client.get("/api/admin/qa-pairs").json()["data"]
    assert data["total"] == 1
    assert [item["id"] for item in data["items"]] == [kept.id]
    assert match_qa(session, embedder.embed(["班车路线有哪些"])[0], view, 0.85) is None
    assert qa_client.delete(f"/api/admin/qa-pairs/{doomed.id}").status_code == 404  # 已删不可再删


# ---------- ④ 编辑后新答案生效（QA-06） ----------


def test_编辑问答后新答案生效_且按新问题重新向量化(qa_client, session):
    """④ PUT 改问题与答案：提问命中新答案文本（QA-06）；旧问题向量不再命中——证明
    embedding 按新问题重新生成（StubEmbedder 确定性，旧向量对新问题相似度≈0）。"""
    embedder = StubEmbedder()
    admin_login(qa_client, session)
    qa = _seed_qa(session, "退货时限是多少", "旧答案：签收后七天内可退货", embedder)
    view = _qa_view(session, qa)

    resp = qa_client.put(
        f"/api/admin/qa-pairs/{qa.id}",
        json={"question": "退货时限是几天", "answer": "新答案：签收后十五天内可退货"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "id": qa.id,
        "question": "退货时限是几天",
        "answer": "新答案：签收后十五天内可退货",
    }

    stored = session.scalar(select(QaPair).where(QaPair.id == qa.id))
    assert (stored.question, stored.answer) == (
        "退货时限是几天",
        "新答案：签收后十五天内可退货",
    )
    hit = match_qa(session, embedder.embed(["退货时限是几天"])[0], view, 0.85)
    assert hit is not None and (hit.qa_id, hit.answer) == (
        qa.id,
        "新答案：签收后十五天内可退货",
    )
    assert match_qa(session, embedder.embed(["退货时限是多少"])[0], view, 0.85) is None


def test_编辑校验_同导入规则且未知id404(qa_client, session):
    """④ 编辑校验同导入规则：空问题/空答案/问题 2001/答案 10001 → 422 中文原因，
    库内内容不变；未知 id 与已软删 id 均 404。"""
    embedder = StubEmbedder()
    admin_login(qa_client, session)
    qa = _seed_qa(session, "原问题", "原答案", embedder)

    cases = [
        ({"question": "", "answer": "答案"}, "问题不能为空"),
        ({"question": "问题", "answer": ""}, "答案不能为空"),
        ({"question": "问" * 2001, "answer": "答案"}, "问题超过 2000 字上限"),
        ({"question": "问题", "answer": "答" * 10001}, "答案超过 10000 字上限"),
    ]
    for body, message in cases:
        resp = qa_client.put(f"/api/admin/qa-pairs/{qa.id}", json=body)
        assert resp.status_code == 422, (body, resp.status_code)
        assert resp.json()["code"] == ERR_PARAM_INVALID
        assert resp.json()["message"] == message
    stored = session.scalar(select(QaPair).where(QaPair.id == qa.id))
    assert (stored.question, stored.answer) == ("原问题", "原答案")  # 校验失败零改动

    assert (
        qa_client.put(
            "/api/admin/qa-pairs/999999", json={"question": "问", "answer": "答"}
        ).status_code
        == 404
    )
    doomed = _seed_qa(session, "待删问题", "待删答案", embedder)
    assert qa_client.delete(f"/api/admin/qa-pairs/{doomed.id}").status_code == 200
    assert (
        qa_client.put(
            f"/api/admin/qa-pairs/{doomed.id}", json={"question": "问", "answer": "答"}
        ).status_code
        == 404
    )  # 软删行不可再编辑


# ---------- ⑤ 三操作审计读回 ----------


def test_三操作审计读回_toggle更新删除_detail逐键断言(qa_client, session):
    """⑤ toggle/update/delete 各落一条审计：object_type/object_id/actor 正确；
    toggle detail={from,to}；update detail 带 before/after 摘要（超 50 字截断）；
    delete detail 带问题摘要。"""
    embedder = StubEmbedder()
    admin_id = admin_login(qa_client, session)
    t = _seed_qa(session, "停用对象问题", "停用对象答案", embedder)
    u = _seed_qa(session, "编辑对象问题", "长" * 60, embedder)
    d = _seed_qa(session, "删除对象问题", "删除对象答案", embedder)

    assert (
        qa_client.patch(
            f"/api/admin/qa-pairs/{t.id}/status", json={"status": "disabled"}
        ).status_code
        == 200
    )
    assert (
        qa_client.put(
            f"/api/admin/qa-pairs/{u.id}",
            json={"question": "编辑对象问题", "answer": "短" * 60},
        ).status_code
        == 200
    )
    assert qa_client.delete(f"/api/admin/qa-pairs/{d.id}").status_code == 200

    audits = dict(session.execute(select(AuditLog.action, AuditLog).order_by(AuditLog.id)).all())
    assert sorted(audits) == ["qa.delete", "qa.toggle", "qa.update"]
    for audit in audits.values():
        assert audit.object_type == "qa_pair"
        assert audit.actor_user_id == admin_id
    assert audits["qa.toggle"].object_id == t.id
    assert audits["qa.toggle"].detail == {"from": "enabled", "to": "disabled"}
    assert audits["qa.update"].object_id == u.id
    assert audits["qa.update"].detail == {
        "before": {"question": "编辑对象问题", "answer": "长" * 50},  # 旧值截断 50 字
        "after": {"question": "编辑对象问题", "answer": "短" * 50},  # 新值同口径截断
    }
    assert audits["qa.delete"].object_id == d.id
    assert audits["qa.delete"].detail == {"question": "删除对象问题"}


# ---------- ⑥ 三操作成功后问答精确缓存整清 ----------


def test_三操作成功后_问答精确缓存整清(qa_client, session):
    """⑥ 预热三条缓存：toggle 清 k1、update 清 k2、delete 清 k3——新答案立即生效，
    陈旧答案不靠 TTL 自然过期（同 documents retry/confirm 口径）。"""
    from app.main import app

    embedder = StubEmbedder()
    admin_login(qa_client, session)
    p1 = _seed_qa(session, "缓存问题一", "答案一", embedder)
    p2 = _seed_qa(session, "缓存问题二", "答案二", embedder)
    p3 = _seed_qa(session, "缓存问题三", "答案三", embedder)
    cache = app.state.chat_deps.cache
    cache.put("k1", {"answer": "陈旧一"})
    cache.put("k2", {"answer": "陈旧二"})
    cache.put("k3", {"answer": "陈旧三"})

    assert (
        qa_client.patch(
            f"/api/admin/qa-pairs/{p1.id}/status", json={"status": "disabled"}
        ).status_code
        == 200
    )
    assert cache.get("k1") is None  # 停用改变可检索事实：整清语义从宽（含 k2/k3）

    cache.put("k2", {"answer": "陈旧二"})  # 重新预热，单独验证编辑操作同样整清
    assert (
        qa_client.put(
            f"/api/admin/qa-pairs/{p2.id}", json={"question": "缓存问题二", "answer": "新答案二"}
        ).status_code
        == 200
    )
    assert cache.get("k2") is None  # 新答案立即生效

    cache.put("k3", {"answer": "陈旧三"})  # 重新预热，单独验证删除操作同样整清
    assert qa_client.delete(f"/api/admin/qa-pairs/{p3.id}").status_code == 200
    assert cache.get("k3") is None


# ---------- 权限门：member 全端点 403 ----------


def test_成员角色访问管理端点_403(qa_client, session):
    """member 角色：列表/停用/编辑/删除均 403/2004（admin-only）。"""
    make_user(session, "qammember", "right-one", role="member")
    assert login_as(qa_client, "qammember", "right-one").status_code == 200

    assert qa_client.get("/api/admin/qa-pairs").status_code == 403
    patch = qa_client.patch("/api/admin/qa-pairs/1/status", json={"status": "disabled"})
    assert patch.status_code == 403
    assert patch.json()["code"] == ERR_FORBIDDEN
    assert (
        qa_client.put("/api/admin/qa-pairs/1", json={"question": "问", "answer": "答"}).status_code
        == 403
    )
    assert qa_client.delete("/api/admin/qa-pairs/1").status_code == 403
