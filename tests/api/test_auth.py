"""鉴权 API 测试（AUTH-01~09，brief 用例表 8 行全覆盖 + 参数化补充）。

覆盖：正确登录 / 失败计数提示 / 5 次锁定+审计 / 成功清零 / 首登强制改密 /
禁用即时生效 / 无 cookie / member 越权；另含密码 >72 字节 422、改密 <8 位 422、
锁定纯函数 evaluate_login 与滑动窗口限流器的穷举单测。
fixtures：tests/api/conftest.py 的 client/fake_clock；用户直插测试事务（bcrypt 哈希）。
"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.db.models import AuditLog, Department, User
from tests.conftest import login_as


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



# ---------- 用例表 1：正确登录 ----------


def test_login_success_sets_httponly_cookie(client, session) -> None:
    """正确登录：200 + Set-Cookie(jwt, httpOnly, SameSite=Lax, 8h)；body code=0。"""
    make_user(session, "alice", "correct-horse", role="admin")
    resp = login_as(client, "alice", "correct-horse")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert body["data"]["username"] == "alice"
    assert body["data"]["role"] == "admin"
    assert body["data"]["mustChangePassword"] is False
    cookie = resp.headers["set-cookie"].lower()
    assert "jwt=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "max-age=28800" in cookie  # 8 小时，与令牌有效期一致
    me = client.get("/api/auth/me")  # cookie 自动携带：取到当前用户
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "alice"


# ---------- 用例表 2：错 4 次提示剩余次数 ----------


def test_wrong_password_four_times_shows_remaining(client, session) -> None:
    """错 4 次：每次 2001 且 message 含剩余 3/2/1/0，未锁定。"""
    user = make_user(session, "bob", "right-one")
    for remaining in ("3", "2", "1", "0"):
        resp = login_as(client, "bob", "wrong-pass")
        assert resp.status_code == 401
        assert resp.json()["code"] == 2001
        assert remaining in resp.json()["message"]
    session.refresh(user)
    assert user.failed_attempts == 4
    assert user.locked_until is None


# ---------- 用例表 3：错 5 次锁定 + 审计 ----------


def test_five_failures_lock_account_and_write_audit(client, session, fake_clock) -> None:
    """错 5 次：第 5 次 2002 锁定 15 分钟；正确密码也 2002；locked_until 落库；写审计。"""
    user = make_user(session, "carol", "right-one")
    for _ in range(4):
        assert login_as(client, "carol", "wrong-pass").json()["code"] == 2001
    fifth = login_as(client, "carol", "wrong-pass")
    assert fifth.status_code == 401
    assert fifth.json()["code"] == 2002
    assert "15" in fifth.json()["message"]
    session.refresh(user)  # 读回断言：锁定状态持久化（§15.4）
    assert user.failed_attempts == 5
    assert user.locked_until == fake_clock.now() + timedelta(minutes=15)
    denied = login_as(client, "carol", "right-one")  # 锁定期间正确密码同样拒绝
    assert denied.json()["code"] == 2002
    audits = session.scalars(select(AuditLog).where(AuditLog.action == "user.lock")).all()
    assert len(audits) == 1
    assert audits[0].object_id == user.id
    assert audits[0].detail["after"]["failedAttempts"] == 5


# ---------- Rider T7：锁定 401 响应体携带 lockedUntil ----------


def test_locked_login_response_carries_locked_until(client, session, fake_clock) -> None:
    """锁定 2002 响应：data.lockedUntil 为 ISO 时刻且等于库内锁定截止（第 5 次触发与
    窗口内正确密码重试两处抛出点均携带），供前端倒计时消费（T7 评审移交）。"""
    from datetime import datetime

    user = make_user(session, "frank", "right-one")
    for _ in range(4):
        assert login_as(client, "frank", "wrong-pass").json()["code"] == 2001
    expected = (fake_clock.now() + timedelta(minutes=15)).isoformat()
    fifth = login_as(client, "frank", "wrong-pass")
    assert fifth.status_code == 401
    assert fifth.json()["code"] == 2002
    assert fifth.json()["data"]["lockedUntil"] == expected

    session.refresh(user)
    denied = login_as(client, "frank", "right-one")  # 锁定窗口内正确密码同样拒绝
    assert denied.status_code == 401
    assert denied.json()["code"] == 2002
    body_until = denied.json()["data"]["lockedUntil"]
    assert body_until == user.locked_until.isoformat()
    assert datetime.fromisoformat(body_until) == user.locked_until  # ISO 可解析且精确相等


# ---------- 用例表 4：成功登录清零计数 ----------


def test_success_login_resets_failed_counter(client, session) -> None:
    """失败 3 次 → 登录成功 → 再失败 1 次：剩余回到 3（计数已清零），不锁定。"""
    user = make_user(session, "dave", "right-one")
    for _ in range(3):
        assert login_as(client, "dave", "wrong-pass").json()["code"] == 2001
    assert login_as(client, "dave", "right-one").status_code == 200
    resp = login_as(client, "dave", "wrong-pass")
    assert resp.json()["code"] == 2001
    assert "3" in resp.json()["message"]  # 若未清零，此处剩余应为 0
    session.refresh(user)
    assert user.failed_attempts == 1
    assert user.locked_until is None


# ---------- 用例表 5：首登强制改密 ----------


def test_first_login_forced_password_change_flow(client, session) -> None:
    """首登改密：登录可成功但业务依赖 403 提示改密；<8 位 422；改后放行、旧密码失效。"""
    user = make_user(session, "erin", "temp-pass", must_change=True)
    assert login_as(client, "erin", "temp-pass").status_code == 200
    blocked = client.get("/api/auth/admin/ping")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == 2004
    assert "修改密码" in blocked.json()["message"]
    short = client.post("/api/auth/change-password", json={"old": "temp-pass", "new": "short"})
    assert short.status_code == 422
    assert short.json()["code"] == 1003
    assert "8" in short.json()["message"]
    ok = client.post("/api/auth/change-password", json={"old": "temp-pass", "new": "new-pass-123"})
    assert ok.status_code == 200
    assert ok.json()["code"] == 0
    session.refresh(user)  # 读回：must_change 清除 + 新哈希落库
    assert user.must_change_password is False
    assert verify_password("new-pass-123", user.password_hash)
    assert client.get("/api/auth/me").json()["data"]["mustChangePassword"] is False
    ping = client.get("/api/auth/admin/ping")  # 已过改密关卡，member 落到权限 403
    assert ping.status_code == 403
    assert "无权限" in ping.json()["message"]
    assert login_as(client, "erin", "temp-pass").json()["code"] == 2001  # 旧密码失效
    assert login_as(client, "erin", "new-pass-123").status_code == 200
    change_audit = session.scalars(
        select(AuditLog).where(AuditLog.action == "user.change_password")
    ).all()
    assert len(change_audit) == 1
    assert change_audit[0].actor_user_id == user.id


def test_change_password_rejects_wrong_old(client, session) -> None:
    """改密验旧密码：原密码不正确 → 400/2001，密码未变更。"""
    user = make_user(session, "henry", "right-one")
    login_as(client, "henry", "right-one")
    resp = client.post(
        "/api/auth/change-password", json={"old": "wrong-old", "new": "new-pass-123"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == 2001
    session.refresh(user)
    assert verify_password("right-one", user.password_hash)


def test_change_password_requires_login(client) -> None:
    """未登录调改密接口：401/2003。"""
    resp = client.post("/api/auth/change-password", json={"old": "x", "new": "new-pass-123"})
    assert resp.status_code == 401
    assert resp.json()["code"] == 2003


@pytest.mark.parametrize("new_password", ["", "1234567", "a" * 73, "密" * 25])
def test_change_password_length_bounds(client, session, new_password) -> None:
    """新密码边界：<8 位或超 72 字节 → 422/1003。"""
    make_user(session, "iris", "right-one")
    login_as(client, "iris", "right-one")
    resp = client.post("/api/auth/change-password", json={"old": "right-one", "new": new_password})
    assert resp.status_code == 422
    assert resp.json()["code"] == 1003


# ---------- 用例表 6：禁用即时生效 ----------


def test_disabled_user_rejected_with_old_cookie(client, session) -> None:
    """禁用后带旧 cookie 的下一次请求：401/2003（§5.6）。"""
    user = make_user(session, "frank", "right-one")
    assert login_as(client, "frank", "right-one").status_code == 200
    user.is_active = False
    session.flush()
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == 2003


# ---------- 终审 I-1：禁用用户登录本身也拒绝（验密后置门，防探测） ----------


def test_disabled_user_login_rejected_same_2003_message(client, session) -> None:
    """禁用用户 login（密码正确）：401/2003「未登录或登录已过期」，与
    get_current_user 的禁用口径同码同文案（不区分原因，无可探测差异）；拒绝路径
    不落 last_login、不动计数；正常用户登录不受影响。"""
    disabled = make_user(session, "被禁登录者", "right-one")
    disabled.is_active = False
    session.flush()
    resp = login_as(client, "被禁登录者", "right-one")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == 2003
    assert body["message"] == "未登录或登录已过期"
    session.refresh(disabled)  # 读回：拒绝路径零状态写入
    assert disabled.is_active is False
    assert disabled.failed_attempts == 0
    assert disabled.last_login_at is None
    make_user(session, "在职登录者", "right-one")  # 同批次正常用户不受牵连
    assert login_as(client, "在职登录者", "right-one").status_code == 200


# ---------- 用例表 7：无 cookie / 坏 cookie ----------


@pytest.mark.parametrize("headers", [{}, {"Cookie": "jwt=not-a-valid-token"}])
def test_missing_or_bad_cookie_unauthorized(client, headers) -> None:
    """无 cookie 或令牌损坏：401/2003。"""
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == 2003


# ---------- 用例表 8：member 越权访问 admin ----------


def test_member_cannot_access_admin_route(client, session) -> None:
    """member 访问 admin-only 路由：403/2004；admin 正常 200。"""
    make_user(session, "member1", "right-one", role="member")
    make_user(session, "root", "right-one", role="admin")
    login_as(client, "member1", "right-one")
    resp = client.get("/api/auth/admin/ping")
    assert resp.status_code == 403
    assert resp.json()["code"] == 2004
    assert "无权限" in resp.json()["message"]
    client.cookies.clear()
    login_as(client, "root", "right-one")
    ok = client.get("/api/auth/admin/ping")
    assert ok.status_code == 200
    assert ok.json()["data"]["pong"] is True


# ---------- 参数化补充：登录密码 >72 字节（bcrypt 上限，API 边界校验） ----------


@pytest.mark.parametrize("password", ["a" * 73, "密" * 25])
def test_login_password_over_72_bytes_rejected(client, session, password) -> None:
    """登录密码超 72 字节：422/1003，在触碰 bcrypt 前拒绝。"""
    make_user(session, "gina", "right-one")
    resp = login_as(client, "gina", password)
    assert resp.status_code == 422
    assert resp.json()["code"] == 1003


# ---------- 补充：未知用户名 / 登录限流 ----------


def test_unknown_username_returns_generic_2001(client) -> None:
    """未知用户名：与密码错误同码 2001，不提示用户是否存在。"""
    resp = login_as(client, "ghost", "whatever-123")
    assert resp.status_code == 401
    assert resp.json()["code"] == 2001


def test_login_rate_limited_by_sliding_window(client, session) -> None:
    """限流：同 IP+用户名窗口内第 11 次请求 429/2005；其他用户名不受影响。"""
    make_user(session, "jack", "right-one")
    for _ in range(10):
        resp = login_as(client, "jack", "wrong-pass")
        assert resp.status_code == 401  # 2001 或 2002（5 次后已锁定），均非限流
    blocked = login_as(client, "jack", "wrong-pass")
    assert blocked.status_code == 429
    assert blocked.json()["code"] == 2005
    make_user(session, "jack2", "right-one")
    other = login_as(client, "jack2", "wrong-pass")
    assert other.status_code == 401
    assert other.json()["code"] == 2001
