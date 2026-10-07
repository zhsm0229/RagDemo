"""我的记忆 API 测试（F-210 管理面，agent-evolution §三.5）。

覆盖：手动添加（1-200 字校验 + strip 口径 + 读回断言）、列表分页 updated_at 降序
与用户隔离（另一用户的记忆不可见）、关键字过滤（%/_ 通配符按字面匹配 §22）、
软删（归属不符与不存在一律 404 同话术，防 IDOR 探测）、用户级开关（/auth/me
memoryEnabled 读回 + 审计留痕）、容量上限淘汰最旧、ReadyUser 门与未登录 401。
fixtures：tests/api/conftest.py 的 client/fake_clock；用户与记忆直插测试事务
（service 的 commit 只释放 savepoint，对本 session 可见，测试结束整体回滚）。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import AuditLog, Department, User, UserMemory
from tests.conftest import login_as

_T0 = datetime(2026, 10, 6, 8, 0, 0, tzinfo=UTC)
_MSG_NOT_FOUND = "记忆不存在或无权访问"


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


def make_memory(
    session: Session,
    user_id: int,
    content: str,
    *,
    source_message_id: int | None = None,
    updated_at: datetime | None = None,
) -> UserMemory:
    """直插记忆行：updated_at 可控（列表降序与容量淘汰的最旧判定依赖它）。"""
    memory = UserMemory(user_id=user_id, content=content, source_message_id=source_message_id)
    if updated_at is not None:
        memory.created_at = updated_at
        memory.updated_at = updated_at
    session.add(memory)
    session.flush()
    return memory


def login(client: TestClient, username: str, password: str = "right-one") -> None:
    """登录并断言成功（cookie 由 client 的 cookie jar 保持）。"""
    resp = login_as(client, username, password)
    assert resp.status_code == 200, resp.json()


def active_memories(session: Session) -> list[UserMemory]:
    """读回当前 is_active 记忆（先 expire_all 强制重查，防身份映射旧值）。"""
    session.expire_all()
    return list(session.scalars(select(UserMemory).where(UserMemory.is_active.is_(True))).all())


# ---------- 手动添加 ----------


def test_add_memory_creates_row_and_audits(client, session) -> None:
    """POST 合法内容：201 落库（strip 后内容、归属本人、软删位 true、来源手动）+ 审计留痕。"""
    user = make_user(session, "alice")
    login(client, "alice")
    resp = client.post("/api/memories", json={"content": "  我负责华东区客服排班  "})
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    item = body["data"]
    assert item["content"] == "我负责华东区客服排班"  # strip 口径
    assert item["source"] == "manual"
    assert item["sourceMessageId"] is None
    assert item["createdAt"]
    # 读回断言（§15.4）：落库内容与归属逐字段核对
    row = session.scalars(select(UserMemory).where(UserMemory.id == item["id"])).one()
    assert row.user_id == user.id
    assert row.content == "我负责华东区客服排班"
    assert row.is_active is True
    assert row.source_message_id is None
    audit = session.scalars(
        select(AuditLog).where(AuditLog.action == "memory.add", AuditLog.object_id == row.id)
    ).one()
    assert audit.actor_user_id == user.id
    assert audit.object_type == "user_memory"
    assert audit.detail["source"] == "manual"


@pytest.mark.parametrize(
    ("content", "reason"),
    [("", "空串"), ("   ", "全空白"), ("a" * 201, "超 200 字上限")],
)
def test_add_memory_rejects_invalid_length(client, session, content: str, reason: str) -> None:
    """非法长度一律 422/1003 零入库（空串/全空白/超 200 字上限）。"""
    make_user(session, "alice")
    login(client, "alice")
    resp = client.post("/api/memories", json={"content": content})
    assert resp.status_code == 422, reason
    body = resp.json()
    assert body["code"] == 1003
    assert body["data"] is None
    assert session.scalars(select(UserMemory)).first() is None


def test_add_memory_accepts_200_chars(client, session) -> None:
    """恰好 200 字（strip 后）为合法上界：201 入库。"""
    make_user(session, "alice")
    login(client, "alice")
    resp = client.post("/api/memories", json={"content": f"  {'字' * 200}  "})
    assert resp.status_code == 201
    row = active_memories(session)[0]
    assert len(row.content) == 200


def test_add_memory_rejects_duplicate_content(client, session) -> None:
    """A6 手动添加查重：strip 后与本人活跃记忆精确重复 → 409/3001 零入库零审计；
    同内容归属他人不冲突（按用户隔离查重），软删行不参与查重（删后可重加）。"""
    alice = make_user(session, "alice")
    make_user(session, "bob")
    login(client, "alice")
    first = client.post("/api/memories", json={"content": "  我负责华东区客服排班  "})
    assert first.status_code == 201

    dup = client.post("/api/memories", json={"content": "我负责华东区客服排班"})
    assert dup.status_code == 409
    assert dup.json() == {"code": 3001, "message": "已存在相同内容的记忆", "data": None}

    login(client, "bob")
    foreign = client.post("/api/memories", json={"content": "我负责华东区客服排班"})
    assert foreign.status_code == 201  # 用户隔离：他人已有同内容不影响本人添加
    login(client, "alice")
    session.expire_all()
    session.query(UserMemory).filter(UserMemory.user_id == alice.id).update({"is_active": False})
    session.flush()
    readd = client.post("/api/memories", json={"content": "我负责华东区客服排班"})
    assert readd.status_code == 201  # 软删行不参与查重：删后可重新添加

    assert len(active_memories(session)) == 2  # alice 1 条（重加）+ bob 1 条
    add_audits = session.scalars(select(AuditLog).where(AuditLog.action == "memory.add")).all()
    assert len(add_audits) == 3  # alice 首加 + bob 添加 + alice 重加；409 不留痕


def test_add_memory_capacity_evicts_oldest(client, session, monkeypatch) -> None:
    """容量上限（settings.memory_max_per_user）：写满后再加，最旧（updated_at 早）软删淘汰。"""
    alice = make_user(session, "alice")
    # 上限收敛为 2 便于断言：monkeypatch 本模块命名空间内的 get_settings（缺省 30 不必插 30 行）
    monkeypatch.setattr(
        "app.services.memory.get_settings",
        lambda: SimpleNamespace(memory_max_per_user=2, memory_inject_limit=10),
    )
    login(client, "alice")
    make_memory(session, alice.id, "旧画像", updated_at=_T0)
    make_memory(session, alice.id, "较新画像", updated_at=_T0 + timedelta(minutes=1))
    resp = client.post("/api/memories", json={"content": "最新画像"})
    assert resp.status_code == 201
    rows = {
        m.content: m
        for m in session.scalars(select(UserMemory).where(UserMemory.user_id == alice.id))
    }
    assert rows["旧画像"].is_active is False  # 淘汰最旧：软删不物理删
    assert rows["较新画像"].is_active is True
    assert rows["最新画像"].is_active is True
    assert len(active_memories(session)) == 2
    evict_audits = session.scalars(select(AuditLog).where(AuditLog.action == "memory.evict")).all()
    assert len(evict_audits) == 1  # B6：淘汰留痕（容量淘汰不是静默丢失）
    assert evict_audits[0].object_id == rows["旧画像"].id
    assert evict_audits[0].detail["id"] == rows["旧画像"].id
    assert evict_audits[0].detail["content"] == "旧画像"
    assert evict_audits[0].actor_user_id == alice.id


# ---------- 列表 ----------


def test_list_memories_paginates_orders_and_isolates(client, session) -> None:
    """GET 列表：updated_at 降序分页；只含本人记忆（用户隔离），他人行不计入 total。"""
    alice = make_user(session, "alice")
    bob = make_user(session, "bob")
    make_memory(session, alice.id, "旧画像", updated_at=_T0)
    make_memory(
        session, alice.id, "自动画像", source_message_id=99, updated_at=_T0 + timedelta(minutes=5)
    )
    make_memory(session, alice.id, "最新画像", updated_at=_T0 + timedelta(minutes=9))
    make_memory(session, bob.id, "别人的画像", updated_at=_T0 + timedelta(minutes=1))
    login(client, "alice")
    first = client.get("/api/memories", params={"page": 1, "pageSize": 2})
    assert first.status_code == 200
    data = first.json()["data"]
    assert (data["total"], data["page"], data["pageSize"]) == (3, 1, 2)
    assert [i["content"] for i in data["items"]] == ["最新画像", "自动画像"]
    assert data["items"][1]["source"] == "auto"  # sourceMessageId 非空 → 自动提炼
    assert data["items"][1]["sourceMessageId"] == 99
    second = client.get("/api/memories", params={"page": 2, "pageSize": 2})
    assert [i["content"] for i in second.json()["data"]["items"]] == ["旧画像"]


def test_list_memories_keyword_filter_and_like_escape(client, session) -> None:
    """keyword 对内容过滤（命中 1 条）；通配符按字面匹配不展开（§22）：`100_的`
    作 ILIKE 通配会命中 `100%的`（_ 匹配任一字符），按字面则零命中。"""
    alice = make_user(session, "alice")
    make_memory(session, alice.id, "负责华东区客服排班", updated_at=_T0)
    make_memory(session, alice.id, "通过率100%的口径", updated_at=_T0 + timedelta(minutes=1))
    make_memory(session, alice.id, "倾向先给结论再给依据", updated_at=_T0 + timedelta(minutes=2))
    login(client, "alice")
    hit = client.get("/api/memories", params={"keyword": "华东区"})
    assert [i["content"] for i in hit.json()["data"]["items"]] == ["负责华东区客服排班"]
    literal = client.get("/api/memories", params={"keyword": "100%"})
    assert [i["content"] for i in literal.json()["data"]["items"]] == ["通过率100%的口径"]
    none = client.get("/api/memories", params={"keyword": "100_的"})
    assert none.json()["data"]["total"] == 0


# ---------- 软删与归属 ----------


def test_soft_delete_own_memory(client, session) -> None:
    """DELETE 本人记忆：软删（is_active=false 读回）+ 退出列表 + 审计 memory.delete。"""
    alice = make_user(session, "alice")
    login(client, "alice")
    added = client.post("/api/memories", json={"content": "要删的画像"}).json()["data"]
    resp = client.delete(f"/api/memories/{added['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": added["id"], "isActive": False}
    session.expire_all()
    row = session.scalars(select(UserMemory).where(UserMemory.id == added["id"])).one()
    assert row.is_active is False  # 读回断言：软删落库，行保留可追溯
    assert active_memories(session) == []
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "memory.delete")).one()
    assert audit.object_id == added["id"]
    assert audit.actor_user_id == alice.id


def test_delete_other_users_memory_404(client, session) -> None:
    """删除他人记忆：404 与"不存在"同话术（防 IDOR 探测），他人行不被改动、无审计。"""
    alice = make_user(session, "alice")
    bob = make_user(session, "bob")
    foreign = make_memory(session, bob.id, "别人的画像", updated_at=_T0)
    login(client, "alice")
    resp = client.delete(f"/api/memories/{foreign.id}")
    assert resp.status_code == 404
    assert resp.json() == {"code": 3003, "message": _MSG_NOT_FOUND, "data": None}
    session.expire_all()
    session.refresh(foreign)
    assert foreign.is_active is True
    assert alice.id != bob.id
    assert (
        session.scalars(select(AuditLog).where(AuditLog.action == "memory.delete")).first() is None
    )


def test_delete_nonexistent_and_already_deleted_404(client, session) -> None:
    """不存在与已软删的记忆：均 404 同话术（已删行不可再删，不给探测空间）。"""
    make_user(session, "alice")
    login(client, "alice")
    missing = client.delete("/api/memories/99999")
    assert missing.status_code == 404
    assert missing.json() == {"code": 3003, "message": _MSG_NOT_FOUND, "data": None}
    own = client.post("/api/memories", json={"content": "只删一次"}).json()["data"]
    assert client.delete(f"/api/memories/{own['id']}").status_code == 200
    again = client.delete(f"/api/memories/{own['id']}")
    assert again.status_code == 404
    assert again.json()["message"] == _MSG_NOT_FOUND


# ---------- 开关与 /auth/me ----------


def test_me_carries_memory_enabled_default_true(client, session) -> None:
    """/auth/me 携带 memoryEnabled：未显式设置时取迁移缺省 true（server_default）。"""
    make_user(session, "alice")
    login(client, "alice")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["data"]["memoryEnabled"] is True


def test_set_enabled_roundtrip_updates_row_and_audits(client, session) -> None:
    """PUT settings：false→true 往返；users 行读回一致；/auth/me 同步；每次变更留痕。"""
    alice = make_user(session, "alice")
    login(client, "alice")
    off = client.put("/api/memories/settings", json={"enabled": False})
    assert off.status_code == 200
    assert off.json()["data"] == {"memoryEnabled": False}
    session.expire_all()
    session.refresh(alice)
    assert alice.memory_enabled is False  # 读回断言：开关落库
    assert client.get("/api/auth/me").json()["data"]["memoryEnabled"] is False
    on = client.put("/api/memories/settings", json={"enabled": True})
    assert on.json()["data"] == {"memoryEnabled": True}
    session.expire_all()
    session.refresh(alice)
    assert alice.memory_enabled is True
    audits = list(session.scalars(select(AuditLog).where(AuditLog.action == "memory.settings")))
    assert [(a.actor_user_id, a.object_type, a.detail) for a in audits] == [
        (alice.id, "user", {"enabled": False}),
        (alice.id, "user", {"enabled": True}),
    ]


# ---------- 鉴权门 ----------


def test_memory_endpoints_require_ready_user(client, session) -> None:
    """首登未改密（must_change_password）：记忆端点在 ReadyUser 门后 403/2004。"""
    make_user(session, "alice", must_change=True)
    login(client, "alice")
    assert client.get("/api/memories").json()["code"] == 2004
    assert client.post("/api/memories", json={"content": "x"}).json()["code"] == 2004
    assert client.delete("/api/memories/1").json()["code"] == 2004
    assert client.put("/api/memories/settings", json={"enabled": False}).json()["code"] == 2004


def test_memory_endpoints_require_login(client) -> None:
    """未登录：401/2003（全局未登录文案，与既有口径一致）。"""
    resp = client.get("/api/memories")
    assert resp.status_code == 401
    assert resp.json()["code"] == 2003
