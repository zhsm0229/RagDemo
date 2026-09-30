r"""用户管理 API 测试（F-102，前端 P6 用户管理页后端）。

覆盖：发放（随机初始密码 16 位无歧义字符集、仅响应体展示一次、user.create
审计读回、重复用户名 409/3005、用户名 2-32 位中文/字母/数字 §21.5、deptCode
与 role 枚举）；列表（降序分页、状态字段、keyword ILIKE 转义对照）；禁用
（即时失效 401、禁用自己 422 防自锁、user.toggle 审计 {from,to}）；重置
（旧密码失效 + must_change=true + 新密码可登录、user.reset_password 审计）；
member 全端点 403 与未登录 401。
fixtures：tests/api/conftest.py 的 client/fake_clock；用户直插测试事务。
"""

import json
import string
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import get_clock
from app.core.errors import (
    ERR_DUPLICATE_USER,
    ERR_FORBIDDEN,
    ERR_PARAM_INVALID,
    ERR_UNAUTHENTICATED,
)
from app.core.security import hash_password, verify_password
from app.db.models import AuditLog, Department, User
from app.main import app
from tests.conftest import login_as

# 与实现同源的无歧义字符集断言口径（字母数字去 0O1lI）：发放密码要人工抄写/口传
_UNAMBIGUOUS = frozenset(string.ascii_letters + string.digits) - frozenset("0O1lI")


# ---------- 构造与登录辅助（口径同 tests/api/test_auth.py） ----------


def make_user(
    session: Session,
    username: str,
    password: str = "right-one",
    role: str = "member",
    must_change: bool = False,
) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内，service 的 commit 后仍可见。"""
    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=must_change,
    )
    session.add(user)
    session.flush()
    return user


def admin_login(client, session: Session) -> User:
    """创建并登录 admin（cookie 由 client 保持），返回该用户供 actor 断言。"""
    admin = make_user(session, "useradmin", "right-one", role="admin")
    assert login_as(client, "useradmin", "right-one").status_code == 200
    return admin


def create_user(
    client, username: str = "新员工甲", dept_code: str = "service", role: str = "member"
):
    return client.post(
        "/api/admin/users", json={"username": username, "deptCode": dept_code, "role": role}
    )


def toggle(client, user_id: int, is_active: bool):
    return client.patch(f"/api/admin/users/{user_id}/status", json={"isActive": is_active})


# ---------- 发放：一次性随机初始密码（F-102 验收） ----------


def test_create_issues_one_time_password_and_audits(client, session) -> None:
    """发放：201 {id, initialPassword}；密码 16 位无歧义字符集；用该密码登录成功
    且首登强制改密；列表与审计均无明文二次泄露；user.create 审计读回。"""
    admin = admin_login(client, session)
    resp = create_user(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    data = body["data"]
    assert set(data) == {"id", "initialPassword"}
    initial = data["initialPassword"]
    assert len(initial) == 16
    assert set(initial) <= _UNAMBIGUOUS  # 去 0O1lI：任一字符肉眼可辨

    created = session.scalars(select(User).where(User.username == "新员工甲")).one()
    assert created.id == data["id"]  # 读回：id 与落库行一致
    assert created.must_change_password is True  # 发放的号首登强制改密（§21.5）
    assert verify_password(initial, created.password_hash)  # bcrypt 落库可验
    assert login_as(client, "新员工甲", initial).status_code == 200  # 初始密码可登录
    me = client.get("/api/auth/me").json()["data"]
    assert me["mustChangePassword"] is True

    login_as(client, "useradmin", "right-one")  # 切回管理员会话再查管理列表
    listed = client.get("/api/admin/users", params={"keyword": "新员工甲"}).json()["data"]
    assert listed["total"] == 1
    assert "initialPassword" not in listed["items"][0]
    assert initial not in json.dumps(listed)  # 明文不在列表任何字段二次出现

    audits = session.scalars(select(AuditLog).where(AuditLog.action == "user.create")).all()
    assert len(audits) == 1
    assert audits[0].object_type == "user"
    assert audits[0].object_id == created.id
    assert audits[0].actor_user_id == admin.id
    assert initial not in json.dumps(audits[0].detail)  # 明文不进审计


def test_create_duplicate_username_conflict_409(client, session) -> None:
    """重复用户名：409/3005 中文提示；库内仍只有一个同名账号。"""
    admin_login(client, session)
    assert create_user(client).status_code == 201
    dup = create_user(client)
    assert dup.status_code == 409
    body = dup.json()
    assert body["code"] == ERR_DUPLICATE_USER
    assert "已存在" in body["message"]
    count = session.scalar(
        select(func.count()).select_from(User).where(User.username == "新员工甲")
    )
    assert count == 1


# 服务内 rollback 会拆掉会话与夹具外部事务的关联（ingest.py:278 记录的 join-external
# 已知交互），用例卸载时 conftest 的 transaction.rollback() 发 SAWarning——按用例收窄静默
@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_create_duplicate_race_at_commit_maps_409(client, session, monkeypatch) -> None:
    """并发抢注兜底（DOC-06 同款，终审 M-2）：预查重通过后、提交前对手抢注同名
    ——commit 撞 users_username_key 的 IntegrityError 映射 409/3005 而非 500，
    回滚零痕迹（新号行与审计一并丢弃）。用审计写入点作钩子在预查后插同名行。"""
    from app.services import users_admin

    admin_login(client, session)
    real_record_audit = users_admin.record_audit

    def race_insert(db, *args, **kwargs):
        outcome = real_record_audit(db, *args, **kwargs)  # 先走原审计（内含 flush）
        dept = db.scalars(select(Department).where(Department.code == "service")).one()
        db.add(
            User(
                username="竞态撞名者",
                password_hash=hash_password("rival-password-1"),
                dept_id=dept.id,
                role="member",
            )
        )  # 审计 flush 之后才插抢注行：其 INSERT 推迟到 commit 的 flush 执行，
        return outcome  # 冲突恰好落在被 try/except 包住的 commit 语句（复现并发半边）

    monkeypatch.setattr(users_admin, "record_audit", race_insert)
    resp = create_user(client, username="竞态撞名者")
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == ERR_DUPLICATE_USER
    assert "已存在" in body["message"]
    count = session.scalar(  # 读回：rollback 后两行同名（新号+抢注者）均不落库
        select(func.count()).select_from(User).where(User.username == "竞态撞名者")
    )
    assert count == 0


@pytest.mark.parametrize(
    "bad_username", ["张", "a" * 33, "张三@mail", "客服 张三", "客服_张三", "ab-cd", ""]
)
def test_create_rejects_invalid_username(client, session, bad_username) -> None:
    """用户名口径（standards §21.5）：2-32 位中文/字母/数字——过短/超长/含符号、
    空格、下划线一律 422/1003 中文提示，不入库。"""
    admin_login(client, session)
    resp = create_user(client, username=bad_username)
    assert resp.status_code == 422
    assert resp.json()["code"] == ERR_PARAM_INVALID
    assert "用户名" in resp.json()["message"]
    count = session.scalar(
        select(func.count()).select_from(User).where(User.username == bad_username)
    )
    assert count == 0  # 坏用户名不入库（共享测试库可能含历史行，按用户名精确圈定）


@pytest.mark.parametrize("username", ["张三", "a" * 32, "员工Zhang3"])
def test_create_accepts_username_boundaries(client, session, username) -> None:
    """边界合法：2 位、32 位、中英数字混合均 201 且可登录字段落库。"""
    admin_login(client, session)
    resp = create_user(client, username=username)
    assert resp.status_code == 201
    created = session.scalars(select(User).where(User.username == username)).one()
    assert created.role == "member"


def test_create_rejects_unknown_dept_and_role(client, session) -> None:
    """deptCode 枚举外与 role 枚举外：422/1003，两个坏号均不入库。"""
    admin_login(client, session)
    bad_dept = create_user(client, username="员工一", dept_code="finance")
    assert bad_dept.status_code == 422
    assert bad_dept.json()["code"] == ERR_PARAM_INVALID
    assert "部门" in bad_dept.json()["message"]
    bad_role = create_user(client, username="员工二", role="root")
    assert bad_role.status_code == 422
    assert bad_role.json()["code"] == ERR_PARAM_INVALID
    assert "角色" in bad_role.json()["message"]
    count = session.scalar(select(func.count()).select_from(User).where(User.role == "root"))
    assert count == 0


# ---------- 列表：降序分页 / 状态字段 / keyword 转义 ----------


def test_list_users_desc_pagination_and_fields(client, session) -> None:
    """列表：created_at 降序（同刻 id 兜底）、分页；items 键名全 camelCase 且状态
    字段齐全；新建号 mustChangePassword=true、locked=false、lastLoginAt=null。
    共享测试库可能含历史用户行，断言一律以 keyword 圈定本次发放的账号。"""
    admin = admin_login(client, session)
    id_a = create_user(client, username="员工甲").json()["data"]["id"]
    id_b = create_user(client, username="员工乙").json()["data"]["id"]

    listed = client.get("/api/admin/users", params={"keyword": "员工"}).json()["data"]
    assert (listed["total"], listed["page"], listed["pageSize"]) == (2, 1, 20)
    assert [i["id"] for i in listed["items"]] == [id_b, id_a]  # 降序：后发放的在前
    item = listed["items"][0]
    assert set(item) == {
        "id",
        "username",
        "name",
        "deptCode",
        "role",
        "status",
        "locked",
        "mustChangePassword",
        "lastLoginAt",
        "createdAt",
    }
    assert item["username"] == "员工乙"
    assert item["name"] == "员工乙"  # 无 name 列则 name 即 username
    assert item["deptCode"] == "service"
    assert item["role"] == "member"
    assert item["status"] == "正常"
    assert item["locked"] is False
    assert item["mustChangePassword"] is True
    assert item["lastLoginAt"] is None

    page2 = client.get(
        "/api/admin/users", params={"keyword": "员工", "page": 2, "pageSize": 1}
    ).json()["data"]
    assert (page2["total"], len(page2["items"])) == (2, 1)
    assert page2["items"][0]["id"] == id_a
    all_first = client.get("/api/admin/users").json()["data"]["items"][:3]  # 全量降序最新在前
    assert [i["id"] for i in all_first] == [id_b, id_a, admin.id]


def test_list_shows_locked_flag_by_current_time(client, session) -> None:
    """locked 按当下时刻判定：locked_until 在未来 → true，且 status 不受锁定影响
    （锁定不改 is_active，仍为「正常」）。"""
    admin_login(client, session)
    create_user(client, username="被锁员工")
    user = session.scalars(select(User).where(User.username == "被锁员工")).one()
    user.locked_until = get_clock().now() + timedelta(minutes=15)
    session.flush()
    item = client.get("/api/admin/users", params={"keyword": "被锁员工"}).json()["data"]["items"][0]
    assert item["locked"] is True
    assert item["status"] == "正常"


def test_list_keyword_wildcards_matched_literally(client, session) -> None:
    """keyword ILIKE 转义（§22，同 qa_pairs 口径）：`折扣率100%`/`折扣率100_` 按
    字面匹配——若展开为通配，两个近似用户名都会被命中；字面 keyword 才精确命中。"""
    admin_login(client, session)
    id_x = create_user(client, username="折扣率100X号").json()["data"]["id"]
    id_9 = create_user(client, username="折扣率1009号").json()["data"]["id"]

    by_pct = client.get("/api/admin/users", params={"keyword": "折扣率100%"}).json()["data"]
    assert (by_pct["total"], by_pct["items"]) == (0, [])  # % 展开则两行皆命中
    by_underline = client.get("/api/admin/users", params={"keyword": "折扣率100_"}).json()["data"]
    assert (by_underline["total"], by_underline["items"]) == (0, [])  # _ 同理
    by_literal = client.get("/api/admin/users", params={"keyword": "折扣率100X"}).json()["data"]
    assert [i["id"] for i in by_literal["items"]] == [id_x]
    by_digit = client.get("/api/admin/users", params={"keyword": "折扣率1009"}).json()["data"]
    assert [i["id"] for i in by_digit["items"]] == [id_9]


# ---------- 启停：禁用即时失效 + 防自锁 + 审计 ----------


def test_disable_immediately_revokes_and_audits(client, session) -> None:
    """禁用：PATCH isActive=false 后该用户旧 cookie 下次请求 401/2003（§5.6）、
    重新登录也拒绝；user.toggle {from,to} 审计读回；启用后 is_active 恢复。"""
    admin = admin_login(client, session)
    member = make_user(session, "被禁员工", "right-one", role="member")

    member_client = TestClient(app)  # 独立 cookie jar：保住被禁用户的旧会话
    assert login_as(member_client, "被禁员工", "right-one").status_code == 200
    assert member_client.get("/api/auth/me").status_code == 200

    resp = toggle(client, member.id, False)
    assert resp.status_code == 200
    assert resp.json()["data"] == {"id": member.id, "isActive": False}

    denied = member_client.get("/api/auth/me")  # 旧 cookie 即时失效
    assert denied.status_code == 401
    assert denied.json()["code"] == ERR_UNAUTHENTICATED

    relogin = login_as(member_client, "被禁员工", "right-one")  # 重新登录同样拒绝（终审 I-1）
    assert relogin.status_code == 401
    assert relogin.json()["code"] == ERR_UNAUTHENTICATED
    assert relogin.json()["message"] == "未登录或登录已过期"

    session.refresh(member)
    assert member.is_active is False  # 读回：落库
    audits = session.scalars(select(AuditLog).where(AuditLog.action == "user.toggle")).all()
    assert len(audits) == 1
    assert audits[0].object_id == member.id
    assert audits[0].actor_user_id == admin.id
    assert audits[0].detail == {"from": True, "to": False}

    reenable = toggle(client, member.id, True)
    assert reenable.status_code == 200
    assert reenable.json()["data"] == {"id": member.id, "isActive": True}
    session.refresh(member)
    assert member.is_active is True


def test_admin_cannot_disable_self(client, session) -> None:
    """禁用自己：422「不能禁用当前登录账号」防自锁；状态不变、不写审计。"""
    admin = admin_login(client, session)
    resp = toggle(client, admin.id, False)
    assert resp.status_code == 422
    assert resp.json()["code"] == ERR_PARAM_INVALID
    assert "不能禁用当前登录账号" in resp.json()["message"]
    session.refresh(admin)
    assert admin.is_active is True
    assert session.scalars(select(AuditLog).where(AuditLog.action == "user.toggle")).all() == []


def test_toggle_unknown_user_404(client, session) -> None:
    """启停未知用户 id：404/1003 中文提示。"""
    admin_login(client, session)
    resp = toggle(client, 987654321, False)
    assert resp.status_code == 404
    assert resp.json()["code"] == ERR_PARAM_INVALID


# ---------- 重置密码：旧密失效 / must_change / 新密可登录 / 审计 ----------


def test_reset_password_invalidates_old_and_audits(client, session) -> None:
    """重置：新初始密码一次展示；旧密码失效 + must_change=true + 新密码可登录；
    user.reset_password 审计读回且不含明文。"""
    admin = admin_login(client, session)
    member = make_user(session, "重置对象", "old-pass-123", role="member")

    resp = client.post(f"/api/admin/users/{member.id}/reset-password")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data) == {"id", "initialPassword"}
    new_pwd = data["initialPassword"]
    assert len(new_pwd) == 16
    assert set(new_pwd) <= _UNAMBIGUOUS

    session.refresh(member)  # 读回：新哈希落库 + 首登强制改密
    assert member.must_change_password is True
    assert verify_password(new_pwd, member.password_hash)
    assert not verify_password("old-pass-123", member.password_hash)

    assert login_as(client, "重置对象", "old-pass-123").json()["code"] == 2001  # 旧密码失效
    assert login_as(client, "重置对象", new_pwd).status_code == 200  # 新密码可登录

    audits = session.scalars(select(AuditLog).where(AuditLog.action == "user.reset_password")).all()
    assert len(audits) == 1
    assert audits[0].object_id == member.id
    assert audits[0].actor_user_id == admin.id
    assert new_pwd not in json.dumps(audits[0].detail)  # 明文不进审计


def test_reset_unknown_user_404(client, session) -> None:
    """重置未知用户 id：404/1003 中文提示。"""
    admin_login(client, session)
    resp = client.post("/api/admin/users/987654321/reset-password")
    assert resp.status_code == 404
    assert resp.json()["code"] == ERR_PARAM_INVALID


# ---------- 权限门：member 全端点 403 / 未登录 401 ----------


def test_member_forbidden_on_all_user_endpoints(client, session) -> None:
    """member 调列表/发放/启停/重置：一律 403/2004（admin-only）。"""
    make_user(session, "普通成员", "right-one", role="member")
    assert login_as(client, "普通成员", "right-one").status_code == 200
    requests = (
        ("get", "/api/admin/users", {}),
        (
            "post",
            "/api/admin/users",
            {"json": {"username": "张三", "deptCode": "service", "role": "member"}},
        ),
        ("patch", "/api/admin/users/1/status", {"json": {"isActive": False}}),
        ("post", "/api/admin/users/1/reset-password", {}),
    )
    for method, path, kwargs in requests:
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 403
        assert resp.json()["code"] == ERR_FORBIDDEN


def test_user_endpoints_require_login(client) -> None:
    """未登录调列表：401/2003（管理接口不开匿名口）。"""
    resp = client.get("/api/admin/users")
    assert resp.status_code == 401
    assert resp.json()["code"] == ERR_UNAUTHENTICATED
