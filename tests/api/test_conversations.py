r"""会话管理 API 测试（Task 10，brief ①-⑥ 逐条落地）：新建/列表/历史消息/软删。

断言口径：响应体 {code, message, data}、列表 {items,total,page,pageSize}（§3.5）、
DTO 键全 camelCase 且集合全等；列表 updated_at DESC（同事务 now() 同刻以 id 降序
兜底，documents 先例）；历史消息 ASC、citations JSONB 逐字段读回、中断占位行
（assistant is_active=false，Task 9 CHAT-12 遗留形态）被过滤而 user 消息全返回；
软删后列表不见、messages 404、重复删除幂等 200 且行保留（不物理删）；越权
（§21.8 IDOR）一律 404/3003，不区分"不存在"与"无权"。
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ERR_NOT_FOUND, ERR_PARAM_INVALID
from app.db.models import Conversation, Department, Message, User

_MSG_NOT_FOUND = "会话不存在或无权访问"
_MESSAGE_KEYS = {
    "id",
    "role",
    "content",
    "citations",
    "feedback",
    "feedbackCategory",
    "feedbackComment",
    "answerType",
    "cacheHit",
    "createdAt",
}


# ---------- 构造与登录辅助（口径同 tests/api/test_documents.py） ----------


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


def create_conversation(client) -> int:
    """新建空会话并返回 id（POST /api/conversations → data.id）。"""
    resp = client.post("/api/conversations")
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


def list_ids(client, **params) -> list[int]:
    """GET 列表的 id 序（params 透传 page/pageSize）。"""
    return [
        item["id"]
        for item in client.get("/api/conversations", params=params).json()["data"]["items"]
    ]


# ---------- ① 新建 → 列表第 1 位、字段读回 ----------


def test_create_conversation_listed_first_with_fields(client, session) -> None:
    """① 新建空会话：data 恰 {id} 一键；列表第 1 位且 title 读回 DB 默认"新会话"。"""
    user = make_user(session, "convuser", "right-one", role="member")
    assert login(client, "convuser", "right-one").status_code == 200

    created = client.post("/api/conversations")

    assert created.status_code == 200
    body = created.json()
    assert body["code"] == 0 and body["message"] == "ok"
    assert set(body["data"]) == {"id"}

    listed = client.get("/api/conversations").json()["data"]
    assert (listed["total"], listed["page"], listed["pageSize"]) == (1, 1, 20)
    assert [item["id"] for item in listed["items"]] == [body["data"]["id"]]  # 第 1 位（唯一）
    item = listed["items"][0]
    assert set(item) == {"id", "title", "updatedAt"}  # 键集合全等，全 camelCase
    assert item["title"] == "新会话"  # DB server_default，代码不内嵌副本
    datetime.fromisoformat(item["updatedAt"])  # ISO 8601 可解析
    row = session.scalars(select(Conversation)).one()
    assert (row.id, row.user_id, row.dept_id, row.is_active) == (
        body["data"]["id"],
        user.id,
        user.dept_id,
        True,
    )


# ---------- ② 两条会话按 updated_at DESC ----------


def test_two_conversations_ordered_updated_at_desc(client, session) -> None:
    """② 后建的会话排前：updated_at DESC（同事务 now() 同刻以 id 降序兜底）。"""
    make_user(session, "orderuser", "right-one", role="member")
    assert login(client, "orderuser", "right-one").status_code == 200

    id_first = create_conversation(client)
    id_second = create_conversation(client)

    assert list_ids(client) == [id_second, id_first]


# ---------- ③ 分页：7 条 + pageSize=5 → total=7、第 2 页 2 条；边界 422 ----------


def test_conversation_list_pagination(client, session) -> None:
    """③ 7 条 + pageSize=5：total=7，第 1 页 5 条、第 2 页恰 2 条（id 降序切片）。"""
    make_user(session, "pageuser", "right-one", role="member")
    assert login(client, "pageuser", "right-one").status_code == 200
    created = [create_conversation(client) for _ in range(7)]

    page1 = client.get("/api/conversations", params={"page": 1, "pageSize": 5}).json()["data"]
    page2 = client.get("/api/conversations", params={"page": 2, "pageSize": 5}).json()["data"]

    assert (page1["total"], page1["page"], page1["pageSize"]) == (7, 1, 5)
    assert [item["id"] for item in page1["items"]] == list(reversed(created))[:5]
    assert (page2["total"], page2["page"], len(page2["items"])) == (7, 2, 2)
    assert [item["id"] for item in page2["items"]] == list(reversed(created))[5:]


def test_conversation_list_page_bounds_422(client, session) -> None:
    """§3.5 分页边界：page 0 与 pageSize 0/101 → 422/1003 字段级指名 page/pageSize。"""
    make_user(session, "bounduser", "right-one", role="member")
    assert login(client, "bounduser", "right-one").status_code == 200

    for params in ({"page": "0"}, {"pageSize": "0"}, {"pageSize": "101"}):
        resp = client.get("/api/conversations", params=params)
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == ERR_PARAM_INVALID
        # query 参数 loc 带 "query." 前缀（validation_error_handler 以 "." 连接）
        assert {entry["field"].split(".")[-1] for entry in body["data"]} == set(params)


# ---------- ④ 历史消息 ASC、citations 逐字段、中断占位行被过滤 ----------


def test_messages_history_asc_filters_interrupted_placeholder(client, session) -> None:
    """④ 历史消息 ASC 全字段读回：citations JSONB 逐字段相等；is_active=false 的
    assistant 中断占位行（Task 9 CHAT-12 遗留形态）不出现，user 消息全部返回。"""
    user = make_user(session, "historyuser", "right-one", role="member")
    assert login(client, "historyuser", "right-one").status_code == 200
    conv_id = create_conversation(client)
    citation = {
        "n": 1,
        "refType": "doc",
        "refId": 42,
        "title": "全员_考勤制度",
        "page": 3,
        "titlePath": "全员_考勤制度/第一章",
        "score": 0.8765,
    }
    m1 = Message(
        conversation_id=conv_id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="user",
        content="第一问：年假几天",
    )
    m2 = Message(
        conversation_id=conv_id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="assistant",
        content="第一答：五到十天",
        citations=[citation],
        answer_type="rag",
        cache_hit=False,
        feedback="down",
        feedback_category="答案错误",
        feedback_comment="日期算错了",
    )
    m3 = Message(  # 中断占位行：content 留空 + is_active=false（chat_stream 断流收尾形态）
        conversation_id=conv_id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="assistant",
        content="",
        is_active=False,
    )
    m4 = Message(
        conversation_id=conv_id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="user",
        content="第二问：病假呢",
    )
    m5 = Message(
        conversation_id=conv_id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="assistant",
        content="第二答：三到十天",
        answer_type="qa",
        cache_hit=True,
    )
    session.add_all([m1, m2, m3, m4, m5])
    session.flush()

    resp = client.get(f"/api/conversations/{conv_id}/messages")

    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert [item["id"] for item in items] == [m1.id, m2.id, m4.id, m5.id]  # ASC，m3 被过滤
    assert all(set(item) == _MESSAGE_KEYS for item in items)  # 10 键集合全等 camelCase
    first_answer = items[1]
    assert first_answer["citations"] == [citation]  # JSONB 逐字段 round-trip
    assert (
        first_answer["feedback"],
        first_answer["feedbackCategory"],
        first_answer["feedbackComment"],
    ) == (
        "down",
        "答案错误",
        "日期算错了",
    )
    assert (first_answer["answerType"], first_answer["cacheHit"]) == ("rag", False)
    second_answer = items[3]
    assert (second_answer["answerType"], second_answer["cacheHit"], second_answer["citations"]) == (
        "qa",
        True,
        None,
    )
    assert items[0]["role"] == "user" and items[0]["citations"] is None  # user 消息全返回
    datetime.fromisoformat(items[0]["createdAt"])


# ---------- ⑤ 越权（§21.8 IDOR）：用户 B 访问/删除用户 A 会话 404/3003 ----------


def test_other_user_conversation_idor_404(client, session) -> None:
    """⑤ 用户 B 读/删用户 A 的会话：一律 404/3003 同文案（不区分不存在与无权）；
    B 的列表不见 A 的会话；不存在 id 同文案 404。"""
    make_user(session, "convowner", "right-one", role="member")
    make_user(session, "convother", "right-two", role="member")
    assert login(client, "convowner", "right-one").status_code == 200
    conv_id = create_conversation(client)
    assert login(client, "convother", "right-two").status_code == 200  # 切换登录态

    messages = client.get(f"/api/conversations/{conv_id}/messages")
    deletion = client.delete(f"/api/conversations/{conv_id}")

    assert messages.status_code == 404
    assert (messages.json()["code"], messages.json()["message"]) == (ERR_NOT_FOUND, _MSG_NOT_FOUND)
    assert deletion.status_code == 404
    assert (deletion.json()["code"], deletion.json()["message"]) == (ERR_NOT_FOUND, _MSG_NOT_FOUND)
    listed = client.get("/api/conversations").json()["data"]
    assert (listed["total"], listed["items"]) == (0, [])
    missing = client.get("/api/conversations/987654321/messages")
    assert (missing.status_code, missing.json()["code"]) == (404, ERR_NOT_FOUND)
    assert session.scalar(select(func.count()).select_from(Message)) == 0  # 越权请求零写入


# ---------- ⑥ 软删：列表不见、messages 404、重复删除幂等 200 ----------


def test_soft_delete_hides_and_is_idempotent(client, session) -> None:
    """⑥ 删除后列表不见、GET messages 404/3003；重复删除幂等 200；行保留
    is_active=false（不物理删）。"""
    user = make_user(session, "deluser", "right-one", role="member")
    assert login(client, "deluser", "right-one").status_code == 200
    conv_id = create_conversation(client)
    session.add(
        Message(
            conversation_id=conv_id,
            user_id=user.id,
            dept_id=user.dept_id,
            role="user",
            content="删除前的问题",
        )
    )
    session.flush()

    first = client.delete(f"/api/conversations/{conv_id}")
    repeat = client.delete(f"/api/conversations/{conv_id}")  # 幂等：已软删再删仍 200

    assert first.status_code == 200
    assert first.json()["data"] == {"id": conv_id, "isActive": False}
    assert repeat.status_code == 200
    assert client.get("/api/conversations").json()["data"]["total"] == 0  # 列表不见
    messages = client.get(f"/api/conversations/{conv_id}/messages")
    assert (messages.status_code, messages.json()["code"]) == (404, ERR_NOT_FOUND)  # 历史不可达
    session.expire_all()  # app 侧 UPDATE 经同一 session，强制重载验证落库形态
    assert session.scalar(select(func.count()).select_from(Conversation)) == 1  # 不物理删
    assert session.scalars(select(Conversation.is_active)).one() is False
