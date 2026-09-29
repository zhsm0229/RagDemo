r"""管理端提问记录 API 测试（Task 8，F-701，brief ①-⑤ 逐条落地）。

断言口径：以 user 消息行为单位且恒滤 is_eval（W3 全局约束：评测数据不污染运营
查询——列表不可见、详情 404）；列表项 answerType/hasCitations/feedback 取自配对
回答（user 行自身恒空，P7 列表"是否有引用/反馈"语义），中断问题配对落空 → 三者
记 None/false；questionPreview 恰截 50 字；DTO 键集合全等 camelCase、列表
{items,total,page,pageSize}（§3.5）；详情 citations JSONB 与落库全等、tokens 逐
字段；member 403/2004、未登录 401/2003（§5.5 依赖链）；非法分页/日期 422/1003。
造数：真 ORM 直插（created_at 显式给值——分区键入主键可直插，默认分区接收），
插入按时间正序 → id 升序与时间序一致，DESC 断言同时钉住 created_at 与 id 兜底。
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_FORBIDDEN, ERR_NOT_FOUND, ERR_PARAM_INVALID, ERR_UNAUTHENTICATED
from app.db.models import Conversation, Department, Message, User

_MSG_NOT_FOUND = "提问记录不存在"
_ITEM_KEYS = {
    "id",
    "conversationId",
    "createdAt",
    "username",
    "deptCode",
    "questionPreview",
    "answerType",
    "hasCitations",
    "feedback",
}
_USER_KEYS = {"id", "conversationId", "createdAt", "username", "deptCode", "content"}
_ANSWER_KEYS = {
    "id",
    "content",
    "citations",
    "answerType",
    "model",
    "promptTokens",
    "completionTokens",
    "latencyMs",
    "cacheHit",
    "feedback",
    "feedbackCategory",
    "feedbackComment",
}

# 长问题：>50 字，验证 questionPreview 恰截前 50 字
_Q1 = "请问客服部门的工作时间是什么？请告诉我具体的上班时间、午休安排，以及法定节假日的调休和加班审批流程是怎么安排的，谢谢。"
_A1 = "答：客服工作时间为 9:00-18:00，午休 12:00-13:00。"
_CITATION_1 = {
    "n": 1,
    "refType": "doc",
    "refId": 42,
    "title": "客服_排班制度",
    "page": 3,
    "titlePath": "客服_排班制度/第一章",
    "score": 0.8765,
}
_CITATION_5 = {
    "n": 1,
    "refType": "doc",
    "refId": 7,
    "title": "全员_报销制度",
    "page": 12,
    "titlePath": "全员_报销制度/第七章",
    "score": 0.9102,
}


# ---------- 构造与登录辅助（口径同 tests/api/test_conversations.py） ----------


def make_user(session: Session, username: str, password: str, dept_code: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），指定部门编码与角色。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == dept_code)).one()
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


def _ask(
    session: Session, conv: Conversation, user: User, question: str, asked_at: datetime, **answer_kw
) -> tuple[Message, Message]:
    """插一组（user 问 + assistant 占位答）：与 api/chat._persist_placeholder 同形态
    （同事务先后发号）；answer_kw 透传回答列（is_active=False 模拟中断、feedback 等
    模拟反馈）。is_eval 例外：双行同置（评测引擎 Task 6 口径——user+assistant 消息
    均标 is_eval=true）；is_active 例外仅作用回答行（中断只软删 assistant 占位行）。"""
    is_eval = answer_kw.pop("is_eval", False)
    q = Message(
        conversation_id=conv.id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="user",
        content=question,
        created_at=asked_at,
        is_eval=is_eval,
    )
    a = Message(
        conversation_id=conv.id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="assistant",
        content=answer_kw.pop("answer", ""),
        created_at=asked_at,
        is_eval=is_eval,
        **answer_kw,
    )
    session.add_all([q, a])
    session.flush()
    return q, a


def _seed_dataset(session: Session) -> SimpleNamespace:
    """① 混合数据集：3 会话跨 service/hr 两部门、5 组真实问答 + 1 组 is_eval 评测。

    覆盖：点踩（a1 down）、点赞（a3 up）、无反馈（a2/a5）、中断无回答（a4 占位行
    is_active=False）、评测消息（q_eval/a_eval，必须被过滤）。时间正序插入：
    q5(09-01) → q1(09-10 08:00) → q2(09-10 08:05) → q_eval(09-10 09:00) →
    q3(09-15) → q4(09-20)。
    """
    admin = make_user(session, "admin_w3", "right-one", "all", role="admin")
    alice = make_user(session, "alice_svc", "right-one", "service", role="member")
    bob = make_user(session, "bob_hr", "right-one", "hr", role="member")
    conv_svc = Conversation(user_id=alice.id, dept_id=alice.dept_id)
    conv_hr = Conversation(user_id=bob.id, dept_id=bob.dept_id)
    conv_hr2 = Conversation(user_id=bob.id, dept_id=bob.dept_id)
    session.add_all([conv_svc, conv_hr, conv_hr2])
    session.flush()

    def at(day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 9, day, hour, minute, tzinfo=UTC)

    q5, a5 = _ask(
        session,
        conv_hr2,
        bob,
        "报销标准是多少？",
        at(1, 12),
        answer="答：按《报销制度》第七条执行。",
        citations=[_CITATION_5],
        answer_type="rag",
    )
    q1, a1 = _ask(
        session,
        conv_svc,
        alice,
        _Q1,
        at(10, 8),
        answer=_A1,
        citations=[_CITATION_1],
        answer_type="rag",
        model="stub",
        prompt_tokens=120,
        completion_tokens=80,
        latency_ms=740,
        cache_hit=False,
        feedback="down",
        feedback_category="答案错误",
        feedback_comment="时间过时了",
    )
    q2, a2 = _ask(
        session,
        conv_svc,
        alice,
        "周末也上班吗？",
        at(10, 8, 5),
        answer="答：周末轮休。",
        answer_type="qa",
        model="stub",
        prompt_tokens=30,
        completion_tokens=12,
        latency_ms=95,
        cache_hit=True,
    )
    q_eval, a_eval = _ask(
        session,
        conv_svc,
        alice,
        "【评测】客服工作时间是多少？",
        at(10, 9),
        answer="【评测】stub 答案",
        is_eval=True,
    )
    q3, a3 = _ask(
        session,
        conv_hr,
        bob,
        "年假有几天？",
        at(15, 9),
        answer="答：入职满一年五天。",
        answer_type="qa",
        feedback="up",
    )
    q4, a4 = _ask(session, conv_hr, bob, "产假政策是什么？", at(20, 10), is_active=False)
    return SimpleNamespace(
        admin=admin,
        alice=alice,
        bob=bob,
        conv_svc=conv_svc,
        conv_hr=conv_hr,
        q1=q1,
        a1=a1,
        q2=q2,
        a2=a2,
        q3=q3,
        a3=a3,
        q4=q4,
        a4=a4,
        q5=q5,
        a5=a5,
        q_eval=q_eval,
        a_eval=a_eval,
    )


def _get_data(client, params: dict | None = None) -> dict:
    resp = client.get("/api/admin/messages", params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["code"], body["message"]) == (0, "ok")
    return body["data"]


def _ids(client, params: dict | None = None) -> list[int]:
    return [item["id"] for item in _get_data(client, params)["items"]]


# ---------- ① 混合数据集逐筛选：is_eval 过滤、部门/日期/点踩、列表项字段全等 ----------


def test_list_filters_over_mixed_dataset(client, session) -> None:
    """① 逐筛选断言：无条件全量 DESC（is_eval 不可见）；部门/日期范围/是否点踩及
    组合各命中精确 id 集；列表项键集合全等、q1 项逐字段全等（questionPreview 恰 50 字）。"""
    d = _seed_dataset(session)
    assert login(client, "admin_w3", "right-one").status_code == 200
    full_order = [d.q4.id, d.q3.id, d.q2.id, d.q1.id, d.q5.id]  # created_at DESC

    data = _get_data(client)
    assert set(data) == {"items", "total", "page", "pageSize"}
    assert (data["total"], data["page"], data["pageSize"]) == (5, 1, 20)
    assert [item["id"] for item in data["items"]] == full_order
    assert all(set(item) == _ITEM_KEYS for item in data["items"])
    assert d.q_eval.id not in {item["id"] for item in data["items"]}  # is_eval 恒被过滤
    assert (
        data["items"][3]
        == {  # 逐字段全等（含截 50 字 preview 与配对回答三字段）
            "id": d.q1.id,
            "conversationId": d.conv_svc.id,
            "createdAt": d.q1.created_at.isoformat(),
            "username": "alice_svc",
            "deptCode": "service",
            "questionPreview": _Q1[:50],
            "answerType": "rag",
            "hasCitations": True,
            "feedback": "down",
        }
    )
    assert len(_Q1) > 50 and len(data["items"][3]["questionPreview"]) == 50

    assert _ids(client, {"deptId": d.alice.dept_id}) == [d.q2.id, d.q1.id]
    assert _ids(client, {"deptId": d.bob.dept_id}) == [d.q4.id, d.q3.id, d.q5.id]
    assert _ids(client, {"deptId": 987654321}) == []  # 不存在的部门：空集而非报错
    assert _ids(client, {"dateFrom": "2026-09-05", "dateTo": "2026-09-15"}) == [
        d.q3.id,
        d.q2.id,
        d.q1.id,
    ]
    assert _ids(client, {"dateFrom": "2026-09-12"}) == [d.q4.id, d.q3.id]
    assert _ids(client, {"dateTo": "2026-09-05"}) == [d.q5.id]
    assert _ids(client, {"hasFeedback": "true"}) == [d.q1.id]  # 仅点踩
    assert _ids(client, {"hasFeedback": "false"}) == [d.q4.id, d.q3.id, d.q2.id, d.q5.id]
    # 组合 AND 有区分度：hr 部门 09-12 之后的两问（service 两问虽也在库但部门不符）
    assert _ids(client, {"deptId": d.bob.dept_id, "dateFrom": "2026-09-12"}) == [d.q4.id, d.q3.id]
    assert _ids(client, {"deptId": d.bob.dept_id, "hasFeedback": "true"}) == []
    detail_eval = client.get(f"/api/admin/messages/{d.q_eval.id}")
    assert (detail_eval.status_code, detail_eval.json()["code"]) == (404, ERR_NOT_FOUND)
    assert detail_eval.json()["message"] == _MSG_NOT_FOUND  # 评测消息详情同被拒（运营查询不触达）


# ---------- ② 分页：5 条 + pageSize=2 → 三页切片；分页/日期边界 422 ----------


def test_list_pagination_and_param_bounds(client, session) -> None:
    """② 5 条 + pageSize=2：total=5，第 1/2 页各 2 条、第 3 页恰 1 条（DESC 切片）；
    page 0 / pageSize 0/101 / 非法日期 → 422/1003 字段级指名。"""
    d = _seed_dataset(session)
    assert login(client, "admin_w3", "right-one").status_code == 200
    expected = [d.q4.id, d.q3.id, d.q2.id, d.q1.id, d.q5.id]

    page1 = _get_data(client, {"page": 1, "pageSize": 2})
    page2 = _get_data(client, {"page": 2, "pageSize": 2})
    page3 = _get_data(client, {"page": 3, "pageSize": 2})
    assert (page1["total"], page1["page"], page1["pageSize"]) == (5, 1, 2)
    assert [item["id"] for item in page1["items"]] == expected[:2]
    assert [item["id"] for item in page2["items"]] == expected[2:4]
    assert (page3["total"], len(page3["items"])) == (5, 1)
    assert page3["items"][0]["id"] == expected[4]

    for params in (
        {"page": "0"},
        {"pageSize": "0"},
        {"pageSize": "101"},
        {"dateFrom": " nonsense"},
    ):
        resp = client.get("/api/admin/messages", params=params)
        assert resp.status_code == 422, params
        body = resp.json()
        assert body["code"] == ERR_PARAM_INVALID
        assert {entry["field"].split(".")[-1] for entry in body["data"]} == {next(iter(params))}


# ---------- ③ 详情：citations JSONB 与落库全等、tokens 逐字段；404 形态 ----------


def test_detail_reads_back_citations_tokens_and_404_shapes(client, session) -> None:
    """③ 详情 user/assistant 两段全等：citations JSONB 逐字段 round-trip、model/
    tokens/latency/cacheHit/反馈三列逐字段；assistant 消息 id / 评测 user 消息 /
    不存在 id → 404/3003 同文案。"""
    d = _seed_dataset(session)
    assert login(client, "admin_w3", "right-one").status_code == 200

    resp = client.get(f"/api/admin/messages/{d.q1.id}")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert set(body) == {"user", "assistant"}
    assert body["user"] == {
        "id": d.q1.id,
        "conversationId": d.conv_svc.id,
        "createdAt": d.q1.created_at.isoformat(),
        "username": "alice_svc",
        "deptCode": "service",
        "content": _Q1,
    }
    assert set(body["user"]) == _USER_KEYS
    assert body["assistant"] == {
        "id": d.a1.id,
        "content": _A1,
        "citations": [_CITATION_1],  # JSONB 与落库全等
        "answerType": "rag",
        "model": "stub",
        "promptTokens": 120,
        "completionTokens": 80,
        "latencyMs": 740,
        "cacheHit": False,
        "feedback": "down",
        "feedbackCategory": "答案错误",
        "feedbackComment": "时间过时了",
    }
    assert set(body["assistant"]) == _ANSWER_KEYS

    for target in (d.a1.id, d.q_eval.id, 987654321):  # assistant 行 / 评测消息 / 不存在
        miss = client.get(f"/api/admin/messages/{target}")
        assert miss.status_code == 404, target
        assert (miss.json()["code"], miss.json()["message"]) == (ERR_NOT_FOUND, _MSG_NOT_FOUND)


# ---------- ④ 中断消息详情：assistant=null ----------


def test_interrupted_question_detail_has_null_assistant(client, session) -> None:
    """④ 中断问题（占位行 is_active=false 被软删）详情：user 段照常、assistant 为
    null；列表项该问 answerType/hasCitations/feedback 记 None/false（配对落空）。"""
    d = _seed_dataset(session)
    assert login(client, "admin_w3", "right-one").status_code == 200

    resp = client.get(f"/api/admin/messages/{d.q4.id}")

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["user"] == {
        "id": d.q4.id,
        "conversationId": d.conv_hr.id,
        "createdAt": d.q4.created_at.isoformat(),
        "username": "bob_hr",
        "deptCode": "hr",
        "content": "产假政策是什么？",
    }
    assert body["assistant"] is None
    listed = next(item for item in _get_data(client)["items"] if item["id"] == d.q4.id)
    assert (listed["answerType"], listed["hasCitations"], listed["feedback"]) == (None, False, None)


# ---------- ⑤ 权限门（§5.5）：未登录 401、member 403 ----------


def test_member_and_anonymous_are_rejected(client, session) -> None:
    """⑤ 未登录 → 401/2003；member 已登录 → 403/2004（列表与详情一致，§5.5 依赖链）。"""
    d = _seed_dataset(session)

    anonymous_list = client.get("/api/admin/messages")
    anonymous_detail = client.get(f"/api/admin/messages/{d.q1.id}")
    for resp in (anonymous_list, anonymous_detail):
        assert (resp.status_code, resp.json()["code"]) == (401, ERR_UNAUTHENTICATED)

    make_user(session, "plain_member", "right-one", "service", role="member")
    assert login(client, "plain_member", "right-one").status_code == 200
    member_list = client.get("/api/admin/messages")
    member_detail = client.get(f"/api/admin/messages/{d.q1.id}")
    for resp in (member_list, member_detail):
        assert (resp.status_code, resp.json()["code"]) == (403, ERR_FORBIDDEN)
