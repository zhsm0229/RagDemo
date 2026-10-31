r"""评测自动配置端点 API 测试（05 票）：GET 读生效值（缺省回落 Settings）/ PUT 覆写
+ 审计 eval.auto_config；非法数值 422/1003；member 403。持久化在 app_settings
（PUT 后 GET 读回，重启不丢由表承载）。"""

from sqlalchemy import select

from app.core.errors import ERR_FORBIDDEN
from app.db.models import AuditLog, Department
from tests.conftest import login_as


def make_user(session, username: str, password: str, role: str):
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    from app.db.models import User as UserModel

    user = UserModel(
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
    make_user(session, "auto_admin", "right-one", role="admin")
    assert login_as(client, "auto_admin", "right-one").status_code == 200


def test_GET返回缺省生效值(client, session) -> None:
    admin_login(client, session)
    resp = client.get("/api/admin/eval-auto/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "enabled": False,
        "synthPerSeed": 2,
        "docSamplePerDept": 20,
        "weeklyMaxQuestions": 100,
        "regressionPp": 5.0,
    }


def test_PUT覆写读回并留审计(client, session) -> None:
    admin_login(client, session)
    payload = {
        "enabled": True,
        "synthPerSeed": 3,
        "docSamplePerDept": 10,
        "weeklyMaxQuestions": 80,
        "regressionPp": 3.5,
    }
    resp = client.put("/api/admin/eval-auto/config", json=payload)
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is True

    data = client.get("/api/admin/eval-auto/config").json()["data"]
    assert data["synthPerSeed"] == 3
    assert data["weeklyMaxQuestions"] == 80

    audit = session.scalars(select(AuditLog).where(AuditLog.action == "eval.auto_config")).one()
    assert audit.detail["before"]["enabled"] is False
    assert audit.detail["after"]["synthPerSeed"] == 3
    assert audit.object_type == "app_setting"


def test_PUT非法数值422(client, session) -> None:
    admin_login(client, session)
    payload = {
        "enabled": True,
        "synthPerSeed": 0,  # 越界（ge=1）
        "docSamplePerDept": 10,
        "weeklyMaxQuestions": 80,
        "regressionPp": 3.5,
    }
    resp = client.put("/api/admin/eval-auto/config", json=payload)
    assert resp.status_code == 422


def test_成员无权读写配置(client, session) -> None:
    make_user(session, "auto_member", "right-one", role="member")
    assert login_as(client, "auto_member", "right-one").status_code == 200
    assert client.get("/api/admin/eval-auto/config").status_code == 403
    resp = client.put(
        "/api/admin/eval-auto/config",
        json={
            "enabled": True,
            "synthPerSeed": 2,
            "docSamplePerDept": 20,
            "weeklyMaxQuestions": 100,
            "regressionPp": 5.0,
        },
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
