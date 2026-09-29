r"""消息反馈 API 测试（Task 11，brief ①-⑤ + 改评/幂等语义）：点赞点踩与 badcase 入口。

断言口径：down 落 feedback/feedback_comment/feedback_category 三列且
badcase_status='待处理'（F-702 治理队列入口），审计 action='message.feedback'/
object_type='message'/object_id 逐字段读回且 detail 不含 comment 原文；跨字段
422/1003 字段级指名 comment/category/feedback（model_validator 经 422 处理器的
ctx.error.field 带出字段名）；up 忽略随附备注/类型置 NULL 且 badcase_status 不动；
归属校验（IDOR §21.8）：他人消息 / user role 消息 / 中断占位行 / 不存在 id 一律
404/3003 同文案且零写入；改评语义：down→up 清空备注与类型、badcase_status 保持
'待处理'不回退（已进治理队列的记录不因改评消失）；重复 down 幂等更新为最新值；
反馈结果经 Task 10 历史接口 feedbackCategory 读回（F-208 状态可见联动）。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ERR_NOT_FOUND, ERR_PARAM_INVALID
from app.db.models import AuditLog, Department, Message, User

_MSG_NOT_FOUND = "消息不存在或无权访问"


# ---------- 构造与登录辅助（口径同 tests/api/test_conversations.py） ----------


def make_user(session: Session, username: str, password: str) -> User:
    """直插测试用户（bcrypt 哈希），挂在测试事务内。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == "all")).first()
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


def login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def new_conversation(client) -> int:
    """新建空会话并返回 id（POST /api/conversations → data.id）。"""
    resp = client.post("/api/conversations")
    assert resp.status_code == 200
    return resp.json()["data"]["id"]


def add_message(
    session: Session, user: User, conv_id: int, role: str, content: str, **kw
) -> Message:
    """直插消息行（Task 10 ④ 先例）：占位行等形态经 kw 传 is_active 等列。"""
    message = Message(
        conversation_id=conv_id,
        user_id=user.id,
        dept_id=user.dept_id,
        role=role,
        content=content,
        **kw,
    )
    session.add(message)
    session.flush()
    return message


def setup_answer(
    session: Session, client, username: str, password: str
) -> tuple[User, int, Message]:
    """登录 + 新建会话 + 直插一条本人 assistant 消息，返回 (user, conv_id, 消息)。"""
    user = make_user(session, username, password)
    assert login(client, username, password).status_code == 200
    conv_id = new_conversation(client)
    return user, conv_id, add_message(session, user, conv_id, "assistant", "答：年假五到十天")


def post_feedback(client, message_id: int, **body):
    return client.post(f"/api/messages/{message_id}/feedback", json=body)


def feedback_audits(session: Session) -> list[AuditLog]:
    """本动作的审计行（按 id ASC）：断言前 expire_all 由用例自行控制。"""
    return session.scalars(
        select(AuditLog).where(AuditLog.action == "message.feedback").order_by(AuditLog.id)
    ).all()


# ---------- ① down 全字段：三列 + badcase_status='待处理' + 审计逐字段读回 ----------


def test_down_persists_columns_badcase_status_and_audit(client, session) -> None:
    """① down 全字段：三列读回（comment strip 落库）+ badcase_status='待处理'
    （F-702 入口）；审计 action/object 逐字段读回，detail 含 feedback/category、
    不含 comment 原文（用户文本已在 messages 列，审计不重复存储）。"""
    user, _, answer = setup_answer(session, client, "fbowner", "right-one")

    resp = post_feedback(
        client, answer.id, feedback="down", category="答案错误", comment="  日期算错了  "
    )

    assert resp.status_code == 200
    body = resp.json()
    assert (body["code"], body["message"]) == (0, "ok")
    assert body["data"] == {
        "id": answer.id,
        "feedback": "down",
        "feedbackCategory": "答案错误",
        "feedbackComment": "日期算错了",  # strip 后落库
        "badcaseStatus": "待处理",
    }
    session.expire_all()  # app 侧 UPDATE 经同一 session，强制重载验证落库形态
    row = session.scalars(select(Message).where(Message.id == answer.id)).one()
    assert (row.feedback, row.feedback_category, row.feedback_comment) == (
        "down",
        "答案错误",
        "日期算错了",
    )
    assert row.badcase_status == "待处理"
    audits = feedback_audits(session)
    assert len(audits) == 1
    audit = audits[0]
    assert (audit.actor_user_id, audit.object_type, audit.object_id) == (
        user.id,
        "message",
        answer.id,
    )
    assert audit.detail == {"feedback": "down", "category": "答案错误"}  # 无 comment 原文


# ---------- ② 跨字段校验：down 缺/空/超长 comment、category 缺失/非法 → 422 字段级 ----------


def test_down_comment_and_category_field_level_422(client, session) -> None:
    """② down 跨字段校验：缺备注/全空白/超 500 字 → 422/1003 指名 comment；
    category 缺失或非法值 → 422 指名 category；feedback 非法字面量/缺失 → 指名
    feedback。校验失败零写入（反馈三列不动、不留审计）。"""
    _, _, answer = setup_answer(session, client, "fbvalid", "right-one")

    bad_comment = [
        {"feedback": "down", "category": "其他"},  # 缺 comment
        {"feedback": "down", "category": "其他", "comment": "  \n\t "},  # strip 后空
        {"feedback": "down", "category": "其他", "comment": "字" * 501},  # 超长
    ]
    for payload in bad_comment:
        resp = post_feedback(client, answer.id, **payload)
        assert resp.status_code == 422, payload
        body = resp.json()
        assert body["code"] == ERR_PARAM_INVALID
        assert {entry["field"] for entry in body["data"]} == {"comment"}

    bad_category = [
        {"feedback": "down", "comment": "没选类型"},  # 缺 category
        {"feedback": "down", "comment": "类型非法", "category": "界面好看"},  # 枚举外
    ]
    for payload in bad_category:
        resp = post_feedback(client, answer.id, **payload)
        assert resp.status_code == 422, payload
        body = resp.json()
        assert body["code"] == ERR_PARAM_INVALID
        assert {entry["field"] for entry in body["data"]} == {"category"}

    for payload in ({"feedback": "star"}, {"comment": "缺 feedback 字段"}):
        resp = post_feedback(client, answer.id, **payload)
        assert resp.status_code == 422, payload
        assert {entry["field"] for entry in resp.json()["data"]} == {"feedback"}

    session.expire_all()
    row = session.scalars(select(Message).where(Message.id == answer.id)).one()
    assert (row.feedback, row.feedback_category, row.feedback_comment, row.badcase_status) == (
        None,
        None,
        None,
        None,
    )
    assert feedback_audits(session) == []  # 校验失败零写入


def test_down_comment_500_chars_accepted(client, session) -> None:
    """§21.5 边界：strip 后恰 500 字备注可落库（501 字已在 ② 拒绝）。"""
    _, _, answer = setup_answer(session, client, "fb500", "right-one")

    resp = post_feedback(
        client, answer.id, feedback="down", category="引用无关", comment="边" * 500
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["feedbackComment"] == "边" * 500


# ---------- ③ up：忽略随附 comment/category 置 NULL，badcase_status 不动 ----------


def test_up_ignores_comment_and_category(client, session) -> None:
    """③ up：feedback='up'，随附的 comment/category 忽略置 NULL；badcase_status
    不动（未进过治理队列保持 None）。"""
    _, _, answer = setup_answer(session, client, "fbup", "right-one")

    resp = post_feedback(
        client, answer.id, feedback="up", category="答案错误", comment="不该带的备注"
    )

    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "id": answer.id,
        "feedback": "up",
        "feedbackCategory": None,
        "feedbackComment": None,
        "badcaseStatus": None,
    }
    session.expire_all()
    row = session.scalars(select(Message).where(Message.id == answer.id)).one()
    assert (row.feedback, row.feedback_category, row.feedback_comment, row.badcase_status) == (
        "up",
        None,
        None,
        None,
    )


# ---------- ④ 归属校验（IDOR §21.8）：他人 / user role / 占位行 / 不存在 → 404/3003 ----------


def test_feedback_forbidden_targets_404_with_zero_writes(client, session) -> None:
    """④ 他人消息、user role 消息、中断占位行（is_active=false）、不存在 id：
    一律 404/3003 同文案"消息不存在或无权访问"，且零写入（反馈列与审计均不动）。"""
    owner = make_user(session, "fbowner2", "right-one")
    make_user(session, "fbother2", "right-two")
    assert login(client, "fbowner2", "right-one").status_code == 200
    conv_id = new_conversation(client)
    answer = add_message(session, owner, conv_id, "assistant", "答：内部口径")
    user_msg = add_message(session, owner, conv_id, "user", "问：我的问题")
    placeholder = add_message(session, owner, conv_id, "assistant", "", is_active=False)

    assert login(client, "fbother2", "right-two").status_code == 200  # 切换到用户 B
    hijack = post_feedback(client, answer.id, feedback="down", category="其他", comment="越权尝试")
    assert (hijack.status_code, hijack.json()["code"]) == (404, ERR_NOT_FOUND)
    assert hijack.json()["message"] == _MSG_NOT_FOUND

    assert login(client, "fbowner2", "right-one").status_code == 200  # 切回本人
    for target in (user_msg.id, placeholder.id, 987654321):  # user 消息/占位行/不存在
        resp = post_feedback(client, target, feedback="down", category="其他", comment="不该成功")
        assert resp.status_code == 404, target
        assert (resp.json()["code"], resp.json()["message"]) == (ERR_NOT_FOUND, _MSG_NOT_FOUND)

    session.expire_all()
    row = session.scalars(select(Message).where(Message.id == answer.id)).one()
    assert (row.feedback, row.feedback_category, row.feedback_comment, row.badcase_status) == (
        None,
        None,
        None,
        None,
    )
    assert feedback_audits(session) == []  # 越权请求零写入


# ---------- ⑤ Task 10 历史接口读回 feedbackCategory（F-208 状态可见联动） ----------


def test_history_endpoint_reflects_feedback(client, session) -> None:
    """⑤ 反馈后经 GET /api/conversations/{id}/messages 读回 feedback/
    feedbackCategory/feedbackComment（F-208 与会话历史联动可见）。"""
    _, conv_id, answer = setup_answer(session, client, "fbhist", "right-one")

    downed = post_feedback(
        client, answer.id, feedback="down", category="拒答不当", comment="该答的没答"
    )
    assert downed.status_code == 200

    items = client.get(f"/api/conversations/{conv_id}/messages").json()["data"]["items"]
    target = next(item for item in items if item["id"] == answer.id)
    assert (target["feedback"], target["feedbackCategory"], target["feedbackComment"]) == (
        "down",
        "拒答不当",
        "该答的没答",
    )


# ---------- ⑥ 改评：down→up 清空备注/类型，badcase_status 保持'待处理'不回退 ----------


def test_change_mind_down_then_up_keeps_badcase_pending(client, session) -> None:
    """⑥ 改评（down→up）：清空 comment/category、feedback='up'，badcase_status
    保持'待处理'不回退——已进治理队列的记录不因改评消失（端点 docstring 契约）。"""
    _, _, answer = setup_answer(session, client, "fbchange", "right-one")
    assert (
        post_feedback(
            client, answer.id, feedback="down", category="其他", comment="先点个踩"
        ).status_code
        == 200
    )

    resp = post_feedback(client, answer.id, feedback="up", comment="改主意了")

    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "id": answer.id,
        "feedback": "up",
        "feedbackCategory": None,
        "feedbackComment": None,
        "badcaseStatus": "待处理",
    }
    session.expire_all()
    row = session.scalars(select(Message).where(Message.id == answer.id)).one()
    assert (row.feedback, row.feedback_comment, row.feedback_category) == ("up", None, None)
    assert row.badcase_status == "待处理"  # 治理队列记录不因改评消失


# ---------- ⑦ 幂等：重复 down 更新为最新值（允许改备注/类型） ----------


def test_repeat_down_updates_to_latest_values(client, session) -> None:
    """⑦ 重复 down 幂等更新为最新备注/类型，badcase_status 仍'待处理'；每次反馈
    各留一条审计（detail.category 反映历次选择）。"""
    _, _, answer = setup_answer(session, client, "fbrepeat", "right-one")
    first = post_feedback(
        client, answer.id, feedback="down", category="答案错误", comment="第一版备注"
    )
    assert first.status_code == 200

    second = post_feedback(
        client, answer.id, feedback="down", category="引用无关", comment="改成引用无关"
    )

    assert second.status_code == 200
    assert second.json()["data"] == {
        "id": answer.id,
        "feedback": "down",
        "feedbackCategory": "引用无关",
        "feedbackComment": "改成引用无关",
        "badcaseStatus": "待处理",
    }
    session.expire_all()
    row = session.scalars(select(Message).where(Message.id == answer.id)).one()
    assert (row.feedback, row.feedback_category, row.feedback_comment) == (
        "down",
        "引用无关",
        "改成引用无关",
    )
    assert row.badcase_status == "待处理"
    assert [a.detail["category"] for a in feedback_audits(session)] == ["答案错误", "引用无关"]


# ---------- ⑧ 未反馈消息的计数兜底：会话级审计零误伤（不写他动作审计） ----------


def test_feedback_audit_count_bound_to_action(client, session) -> None:
    """⑧ up 反馈也留审计且 detail 恒不含 category 键之外的用户文本：up 的 detail
    恰为 {"feedback":"up"}（category=None 不入审计）。"""
    _, _, answer = setup_answer(session, client, "fbupaudit", "right-one")

    resp = post_feedback(client, answer.id, feedback="up")

    assert resp.status_code == 200
    audits = feedback_audits(session)
    assert len(audits) == 1
    assert audits[0].detail == {"feedback": "up"}
    assert (
        session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.object_type == "message")
        )
        == 1
    )


# ---------- ⑨ F-702 W3 裁决：admin 处理终态不被用户改评重开 ----------


def test_repeat_down_preserves_admin_final_badcase_status(client, session) -> None:
    """admin 已置处理终态（Task 9 管理端点前直改 messages 行模拟）后用户再次 down：
    badcase_status 保持终态不重开（'已忽略'/'已修语料'），备注/类型照常更新为新值、
    审计照常落——信息不丢（W2 账本移交③控制器裁决，Task 9 管理界面消费同一语义）。"""
    _, _, ignored = setup_answer(session, client, "fbfinal1", "right-one")
    _, _, fixed = setup_answer(session, client, "fbfinal2", "right-two")
    ignored.badcase_status = "已忽略"  # 模拟 admin 处理终态（Task 9 才出管理端点）
    fixed.badcase_status = "已修语料"
    session.flush()

    assert login(client, "fbfinal1", "right-one").status_code == 200  # 切回本人再反馈
    first = post_feedback(
        client, ignored.id, feedback="down", category="答案错误", comment="换了个说法"
    )
    assert login(client, "fbfinal2", "right-two").status_code == 200
    second = post_feedback(client, fixed.id, feedback="down", category="其他", comment="补充了细节")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"]["badcaseStatus"] == "已忽略"  # 终态不被重开
    assert first.json()["data"]["feedbackComment"] == "换了个说法"  # 备注照常更新
    assert second.json()["data"]["badcaseStatus"] == "已修语料"
    session.expire_all()
    rows = session.scalars(select(Message).where(Message.id.in_([ignored.id, fixed.id]))).all()
    by_id = {row.id: row for row in rows}
    assert (by_id[ignored.id].badcase_status, by_id[ignored.id].feedback_category) == (
        "已忽略",
        "答案错误",
    )
    assert (by_id[fixed.id].badcase_status, by_id[fixed.id].feedback_comment) == (
        "已修语料",
        "补充了细节",
    )
    assert [a.detail["category"] for a in feedback_audits(session)] == ["答案错误", "其他"]
