r"""评测题库列表与层级切换 API 测试（02 票，评测集自动更新）。

断言：列表分页与五维筛选（origin/tier/dept/qtype/keyword，camelCase 响应）；tier
切换 official↔extended 200 读回 + 审计 eval.tier（detail from/to）；未知题 404/3003、
非法层级 422/1003、member 403/2004。存量人工题默认 origin=manual_import/tier=official
（0014 迁移缺省，直插行验证缺省生效）。

认证辅助复用同目录 test_admin_eval_import（make_user/admin_login 先例）。
"""

import pytest
from sqlalchemy import select

from app.core.errors import ERR_FORBIDDEN, ERR_NOT_FOUND
from app.db.models import AuditLog, Department, EvalQuestion
from tests.conftest import login_as

pytestmark = pytest.mark.usefixtures("fake_clock")


def make_user(session, username: str, password: str, role: str):
    """直插测试用户（口径同 test_admin_eval_import.make_user）。"""
    from app.core.security import hash_password
    from app.db.models import User

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


def admin_login(client, session) -> None:
    make_user(session, "evaladmin2", "right-one", role="admin")
    assert login_as(client, "evaladmin2", "right-one").status_code == 200


def _dept_id(session, code: str) -> int:
    return session.scalars(select(Department.id).where(Department.code == code)).one()


def _add_question(
    session,
    dept_code: str,
    question: str,
    *,
    expected: str = "标准答案",
    qtype: str = "factual",
) -> EvalQuestion:
    """直插评测题（不填 origin/tier → 验证 0014 server_default 缺省生效）。"""
    item = EvalQuestion(
        dept_id=_dept_id(session, dept_code),
        question=question,
        expected_answer=expected,
        qtype=qtype,
    )
    session.add(item)
    session.flush()
    return item


@pytest.fixture()
def _seeded(session):
    """两部门各一题：service 一道事实题、hr 一道拒答题，筛选断言的固定背景。"""
    svc = _add_question(session, "service", "蓝鲸值班的夜班补贴是多少？")
    hr = _add_question(session, "hr", "[拒答] 外部门礼节性问候", qtype="cross_reject")
    return {"svc": svc, "hr": hr}


# ---------- 列表：分页 + 筛选 ----------


def test_列表返回缺省层级与来源(client, session, _seeded) -> None:
    """直插行未显式给 origin/tier：响应字段应为迁移缺省 manual_import/official。"""
    admin_login(client, session)
    resp = client.get("/api/admin/eval-questions?page=1&pageSize=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["pageSize"] == 10
    first = data["items"][0]
    assert first["origin"] == "manual_import"
    assert first["tier"] == "official"
    assert first["isActive"] is True
    assert {"id", "deptCode", "question", "expectedAnswer", "qtype", "createdAt"} <= set(first)


def test_列表按层级与部门筛选(client, session, _seeded) -> None:
    admin_login(client, session)
    _seeded["svc"].tier = "extended"
    session.flush()

    resp = client.get("/api/admin/eval-questions?tier=extended")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert [item["id"] for item in items] == [_seeded["svc"].id]
    assert items[0]["deptCode"] == "service"

    resp = client.get("/api/admin/eval-questions?dept=hr&qtype=cross_reject")
    items = resp.json()["data"]["items"]
    assert [item["id"] for item in items] == [_seeded["hr"].id]


def test_列表按关键词筛选(client, session, _seeded) -> None:
    admin_login(client, session)
    resp = client.get("/api/admin/eval-questions?keyword=蓝鲸值班")
    items = resp.json()["data"]["items"]
    assert [item["id"] for item in items] == [_seeded["svc"].id]


# ---------- 层级切换：升格/降格 + 审计 ----------


def test_升格extended到official并留审计(client, session, _seeded) -> None:
    admin_login(client, session)
    target = _seeded["svc"]
    target.tier = "extended"
    session.flush()

    resp = client.patch(f"/api/admin/eval-questions/{target.id}/tier", json={"tier": "official"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": target.id, "tier": "official"}
    session.expire_all()
    session.refresh(target)
    assert target.tier == "official"
    audit = session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "eval.tier", AuditLog.object_id == target.id)
        .order_by(AuditLog.id.desc())
    ).first()
    assert audit is not None
    assert audit.object_type == "eval"
    assert audit.detail == {"from": "extended", "to": "official"}


def test_降格official到extended(client, session, _seeded) -> None:
    admin_login(client, session)
    target = _seeded["hr"]
    resp = client.patch(f"/api/admin/eval-questions/{target.id}/tier", json={"tier": "extended"})
    assert resp.status_code == 200
    session.expire_all()
    session.refresh(target)
    assert target.tier == "extended"


def test_层级切换错误分支(client, session, _seeded) -> None:
    admin_login(client, session)
    target = _seeded["svc"]
    assert (
        client.patch(
            f"/api/admin/eval-questions/{target.id}/tier", json={"tier": "gold"}
        ).status_code
        == 422
    )
    resp = client.patch("/api/admin/eval-questions/999999/tier", json={"tier": "official"})
    assert resp.status_code == 404
    assert resp.json()["code"] == ERR_NOT_FOUND


def test_成员无权访问列表与切换(client, session, _seeded) -> None:
    make_user(session, "evalmember2", "right-one", role="member")
    assert login_as(client, "evalmember2", "right-one").status_code == 200
    assert client.get("/api/admin/eval-questions").status_code == 403
    resp = client.patch(
        f"/api/admin/eval-questions/{_seeded['svc'].id}/tier", json={"tier": "extended"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
