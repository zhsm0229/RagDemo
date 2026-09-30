r"""管理端 badcase 治理 API 测试（Task 9，F-702，brief ①-④ + W3 联动裁决 + 权限门）。

断言口径：队列以 messages 行 feedback='down' AND is_eval=false 为一条点踩记录
（一条回答一条记录，天然不重复）；is_eval 的点踩不进队列（W3 约束：评测数据不
污染运营查询）；badcaseStatus 缺省 = 全部状态（brief 钉死：'待处理' 由前端传，
API 缺省不过滤）；questionPreview 恰截 50 字；DTO 键集合全等、列表
{items,total,page,pageSize}（§3.5）；PATCH 仅 down 且非评测消息可寻址（不存在/
点赞/评测点踩 → 404/3003 同文案），处置枚举外 → 422/1003 字段级指名
badcaseStatus；note 只入审计不入 messages，规则随冻结基线 ADM-03（规格优先于
计划）：'已忽略' 必填理由、'已修语料' 可选，给了则 strip 后 1-500；审计
action='badcase.update'、detail={messageId,from,to,note} 逐字段读回；与 Task 1
裁决联动（API 级端到端）：admin 置'已忽略'后用户再 down → 状态保持'已忽略'但
category/comment 更新并在队列可见。member 403/2004、未登录 401/2003（§5.5）。
造数：真 ORM 直插（created_at 显式给值——分区键入主键可直插，时间正序插入 →
id 升序与时间序一致，DESC 断言同时钉住 created_at 与 id 兜底）；④ 联动走真实
反馈端点（W2 POST /api/messages/{id}/feedback）端到端验证。
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_FORBIDDEN, ERR_NOT_FOUND, ERR_PARAM_INVALID, ERR_UNAUTHENTICATED
from app.db.models import AuditLog, Conversation, Department, Message, User
from tests.conftest import login_as

_MSG_NOT_FOUND = "点踩记录不存在"
_ITEM_KEYS = {
    "messageId",
    "createdAt",
    "deptCode",
    "questionPreview",
    "category",
    "comment",
    "badcaseStatus",
}

# 长问题：>50 字，验证 questionPreview 恰截前 50 字
_LONG_Q = (
    "请问客服部门法定节假日的排班是怎么安排的？加班审批流程和调休规则具体是怎样的，"
    "跨部门借调期间工时怎么计算，麻烦详细说明一下，谢谢。"
)


# ---------- 构造与登录辅助（口径同 tests/api/test_admin_messages.py） ----------


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


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


def _ask(
    session: Session, conv: Conversation, user: User, question: str, asked_at: datetime, **answer_kw
) -> Message:
    """插一组（user 问 + assistant 答），返回 assistant 行（badcase 记录主体）；
    形态与 api/chat 落库一致（同事务先后发号，user id < assistant id，配对锚定
    "id 小于回答行的最新 user 行"）。is_eval 双行同置（评测引擎 Task 6 口径）。"""
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
    return a


def _downed(answer: Message, category: str, comment: str, status: str = "待处理") -> Message:
    """把回答行标成已点踩（feedback 三列 + 队列状态，与 W2 反馈端点落库形态一致）。"""
    answer.feedback = "down"
    answer.feedback_category = category
    answer.feedback_comment = comment
    answer.badcase_status = status
    return answer


def _seed_dataset(session: Session) -> SimpleNamespace:
    """① 混合数据集：2 会话跨 service/hr 两部门、3 条点踩（2 待处理 + 1 已处理）+
    评测点踩（必须被过滤）+ 点赞 + 无反馈。时间正序插入：b1(09-03) → b2(09-05) →
    b3(09-07) → b_eval(09-08) → up(09-09) → plain(09-10)，队列 DESC 期望
    [b3, b2, b1]。"""
    admin = make_user(session, "admin_b9", "right-one", "all", role="admin")
    alice = make_user(session, "alice_svc", "right-one", "service", role="member")
    bob = make_user(session, "bob_hr", "right-one", "hr", role="member")
    conv_svc = Conversation(user_id=alice.id, dept_id=alice.dept_id)
    conv_hr = Conversation(user_id=bob.id, dept_id=bob.dept_id)
    session.add_all([conv_svc, conv_hr])
    session.flush()

    b1 = _downed(
        _ask(
            session,
            conv_svc,
            alice,
            _LONG_Q,
            _at(3, 9),
            answer="答：客服工作时间为 9:00-18:00。",
            answer_type="rag",
        ),
        "答案错误",
        "工时时间过时了",
    )
    b2 = _downed(
        _ask(
            session,
            conv_hr,
            bob,
            "报销标准是多少？",
            _at(5, 10),
            answer="答：按《报销制度》第七条执行。",
            answer_type="qa",
        ),
        "引用无关",
        "引用和问题对不上",
    )
    b3 = _downed(
        _ask(
            session,
            conv_hr,
            bob,
            "年假有几天？",
            _at(7, 11),
            answer="答：入职满一年五天。",
            answer_type="qa",
        ),
        "其他",
        "回答格式混乱",
        status="已修语料",
    )
    b_eval = _downed(
        _ask(
            session,
            conv_svc,
            alice,
            "【评测】客服工作时间是多少？",
            _at(8, 12),
            answer="【评测】stub 答案",
            is_eval=True,
        ),
        "答案错误",
        "评测数据也点踩",
    )
    up_msg = _ask(
        session,
        conv_svc,
        alice,
        "周末也上班吗？",
        _at(9, 13),
        answer="答：周末轮休。",
        feedback="up",
    )
    plain = _ask(session, conv_hr, bob, "产假政策是什么？", _at(10, 14), answer="答：98 天。")
    return SimpleNamespace(
        admin=admin,
        alice=alice,
        bob=bob,
        conv_svc=conv_svc,
        conv_hr=conv_hr,
        b1=b1,
        b2=b2,
        b3=b3,
        b_eval=b_eval,
        up_msg=up_msg,
        plain=plain,
    )


def _get_data(client, params: dict | None = None) -> dict:
    resp = client.get("/api/admin/badcases", params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["code"], body["message"]) == (0, "ok")
    return body["data"]


def _ids(client, params: dict | None = None) -> list[int]:
    return [item["messageId"] for item in _get_data(client, params)["items"]]


def badcase_audits(session: Session) -> list[AuditLog]:
    """本动作的审计行（按 id ASC）；断言前 expire_all 由用例自行控制。"""
    return session.scalars(
        select(AuditLog).where(AuditLog.action == "badcase.update").order_by(AuditLog.id)
    ).all()


# ---------- ① 点踩队列：缺省全部 / 状态筛 / 部门筛 / is_eval 过滤 / 不重复 ----------


def test_list_down_queue_filters_uniqueness_and_fields(client, session) -> None:
    """① 双部门点踩队列：缺省=全部状态 DESC（含已处理）；badcaseStatus='待处理'
    筛、'已修语料' 筛、deptId 筛及组合各命中精确 messageId 集；is_eval 点踩不可见；
    列表项键集合全等、b1 项逐字段全等（questionPreview 恰 50 字）；一条回答一条
    记录不重复。"""
    d = _seed_dataset(session)
    assert login_as(client, "admin_b9", "right-one").status_code == 200
    desc_all = [d.b3.id, d.b2.id, d.b1.id]  # created_at DESC

    data = _get_data(client)
    assert set(data) == {"items", "total", "page", "pageSize"}
    assert (data["total"], data["page"], data["pageSize"]) == (3, 1, 20)
    assert [item["messageId"] for item in data["items"]] == desc_all
    assert len({item["messageId"] for item in data["items"]}) == 3  # 一条回答一条记录
    assert d.b_eval.id not in {item["messageId"] for item in data["items"]}  # 评测恒被过滤
    assert all(set(item) == _ITEM_KEYS for item in data["items"])
    assert (
        data["items"][2]
        == {  # 逐字段全等（含恰截 50 字的 questionPreview）
            "messageId": d.b1.id,
            "createdAt": d.b1.created_at.isoformat(),
            "deptCode": "service",
            "questionPreview": _LONG_Q[:50],
            "category": "答案错误",
            "comment": "工时时间过时了",
            "badcaseStatus": "待处理",
        }
    )
    assert len(_LONG_Q) > 50 and len(data["items"][2]["questionPreview"]) == 50

    assert _ids(client, {"badcaseStatus": "待处理"}) == [d.b2.id, d.b1.id]
    assert _ids(client, {"badcaseStatus": "已修语料"}) == [d.b3.id]
    assert _ids(client, {"badcaseStatus": "已忽略"}) == []
    assert _ids(client, {"badcaseStatus": "不存在状态"}) == []  # 枚举外值：空集而非报错
    assert _ids(client, {"deptId": d.alice.dept_id}) == [d.b1.id]
    assert _ids(client, {"deptId": d.bob.dept_id}) == [d.b3.id, d.b2.id]
    assert _ids(client, {"deptId": 987654321}) == []  # 不存在的部门：空集而非报错
    # 组合 AND 有区分度：hr 部门的待处理（service 的 b1 与已处理的 b3 均不符）
    assert _ids(client, {"deptId": d.bob.dept_id, "badcaseStatus": "待处理"}) == [d.b2.id]


# ---------- ② PATCH 处置：状态落库读回 + 审计含 note + 队列随迁；忽略必填理由 ----------


def test_patch_updates_status_audit_note_readback(client, session) -> None:
    """② PATCH '已修语料'+note：响应 data 读回；messages 行 badcase_status 迁移且
    反馈三列不动（note 只入审计不入 messages）；审计 action/object/detail
    {messageId,from,to,note} 逐字段读回；队列筛选随状态迁移。'已忽略' 无 note →
    422/1003 字段级指名 note（ADM-03 冻结基线：忽略必填理由，规格优先于计划），
    带 note → 200 且审计含 note。"""
    d = _seed_dataset(session)
    assert login_as(client, "admin_b9", "right-one").status_code == 200

    resp = client.patch(
        f"/api/admin/badcases/{d.b1.id}",
        json={"badcaseStatus": "已修语料", "note": "已补 2026 排班语料并重建索引"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert (body["code"], body["message"]) == (0, "ok")
    assert body["data"] == {"messageId": d.b1.id, "badcaseStatus": "已修语料"}

    session.expire_all()  # app 侧 UPDATE 经同一 session，强制重载验证落库形态
    row = session.scalars(select(Message).where(Message.id == d.b1.id)).one()
    assert row.badcase_status == "已修语料"
    assert (row.feedback, row.feedback_category, row.feedback_comment) == (
        "down",
        "答案错误",
        "工时时间过时了",
    )

    audits = badcase_audits(session)
    assert len(audits) == 1
    audit = audits[0]
    assert (audit.actor_user_id, audit.object_type, audit.object_id) == (
        d.admin.id,
        "message",
        d.b1.id,
    )
    assert audit.detail == {
        "messageId": d.b1.id,
        "from": "待处理",
        "to": "已修语料",
        "note": "已补 2026 排班语料并重建索引",
    }

    assert _ids(client, {"badcaseStatus": "待处理"}) == [d.b2.id]  # 队列随状态迁移
    assert _ids(client, {"badcaseStatus": "已修语料"}) == [d.b3.id, d.b1.id]

    ignore = client.patch(f"/api/admin/badcases/{d.b2.id}", json={"badcaseStatus": "已忽略"})
    assert ignore.status_code == 422  # ADM-03：忽略必填理由（评审修复轮 1 裁决）
    ignore_body = ignore.json()
    assert ignore_body["code"] == ERR_PARAM_INVALID
    assert {entry["field"] for entry in ignore_body["data"]} == {"note"}
    session.expire_all()
    row_b2 = session.scalars(select(Message).where(Message.id == d.b2.id)).one()
    assert row_b2.badcase_status == "待处理"  # 校验失败零写入
    assert badcase_audits(session) == audits  # 审计仍只有已修语料一条

    ignored = client.patch(
        f"/api/admin/badcases/{d.b2.id}",
        json={"badcaseStatus": "已忽略", "note": "产品已知问题，下版本修复"},
    )
    assert ignored.status_code == 200
    assert ignored.json()["data"] == {"messageId": d.b2.id, "badcaseStatus": "已忽略"}
    session.expire_all()
    audits = badcase_audits(session)
    assert len(audits) == 2
    assert audits[1].detail == {
        "messageId": d.b2.id,
        "from": "待处理",
        "to": "已忽略",
        "note": "产品已知问题，下版本修复",
    }


# ---------- ②-b note 边界与可选分支：已忽略必填 1-500、已修语料可选 ≤500 ----------


def test_patch_note_boundaries_ignore_requires_and_fixed_optional(client, session) -> None:
    """②-b ADM-03 分支全覆盖：'已忽略' + 超 500 字 / 全空白（strip 后空）note →
    422/1003 指名 note；'已修语料' + 超 500 字 note → 422（给了也 ≤500）；无 note →
    200 且审计 note=null（可选）；恰 500 字 note → 200 且审计原文读回（§21.5 边界）。"""
    d = _seed_dataset(session)
    assert login_as(client, "admin_b9", "right-one").status_code == 200

    for payload in (
        {"badcaseStatus": "已忽略", "note": "边" * 501},  # 超长
        {"badcaseStatus": "已忽略", "note": "  \n\t "},  # strip 后空：等同未填理由
        {"badcaseStatus": "已修语料", "note": "边" * 501},  # 可选但给了须 ≤500
    ):
        resp = client.patch(f"/api/admin/badcases/{d.b1.id}", json=payload)
        assert resp.status_code == 422, payload
        body = resp.json()
        assert body["code"] == ERR_PARAM_INVALID
        assert {entry["field"] for entry in body["data"]} == {"note"}
    assert badcase_audits(session) == []  # 校验失败零审计

    optional = client.patch(f"/api/admin/badcases/{d.b1.id}", json={"badcaseStatus": "已修语料"})
    assert optional.status_code == 200
    boundary = client.patch(
        f"/api/admin/badcases/{d.b2.id}", json={"badcaseStatus": "已修语料", "note": "边" * 500}
    )
    assert boundary.status_code == 200

    session.expire_all()
    audits = badcase_audits(session)
    assert len(audits) == 2
    assert audits[0].detail == {  # 无 note：'已修语料' 下可选，审计 note=null
        "messageId": d.b1.id,
        "from": "待处理",
        "to": "已修语料",
        "note": None,
    }
    assert audits[1].detail == {
        "messageId": d.b2.id,
        "from": "待处理",
        "to": "已修语料",
        "note": "边" * 500,
    }


# ---------- ③ 处置校验：非法枚举 422 字段级 / 宇宙外 404 同文案 / 零写入 ----------


def test_patch_invalid_status_422_and_non_badcase_404(client, session) -> None:
    """③ 处置状态枚举外（含'待处理'——PATCH 只收两个终态）/ 缺字段 → 422/1003
    字段级指名 badcaseStatus；寻址宇宙外（不存在 id / 点赞消息 / 评测点踩）→
    404/3003 同文案"点踩记录不存在"；全部失败形态零写入（状态与审计均不动）。"""
    d = _seed_dataset(session)
    assert login_as(client, "admin_b9", "right-one").status_code == 200

    for payload in (
        {"badcaseStatus": "已删除"},
        {"badcaseStatus": "待处理"},  # '待处理' 非 admin 处置终态，只由点踩入口产生
        {"note": "缺状态字段"},
    ):
        resp = client.patch(f"/api/admin/badcases/{d.b1.id}", json=payload)
        assert resp.status_code == 422, payload
        body = resp.json()
        assert body["code"] == ERR_PARAM_INVALID
        assert {entry["field"] for entry in body["data"]} == {"badcaseStatus"}

    for target in (987654321, d.up_msg.id, d.b_eval.id):
        miss = client.patch(
            f"/api/admin/badcases/{target}", json={"badcaseStatus": "已忽略", "note": "不该成功"}
        )
        assert miss.status_code == 404, target
        assert (miss.json()["code"], miss.json()["message"]) == (ERR_NOT_FOUND, _MSG_NOT_FOUND)

    session.expire_all()
    rows = {
        row.id: row
        for row in session.scalars(
            select(Message).where(Message.id.in_([d.b1.id, d.b2.id, d.b3.id]))
        ).all()
    }
    assert [
        rows[d.b1.id].badcase_status,
        rows[d.b2.id].badcase_status,
        rows[d.b3.id].badcase_status,
    ] == [
        "待处理",
        "待处理",
        "已修语料",
    ]
    assert badcase_audits(session) == []  # 校验失败零写入


# ---------- ④ W3 裁决联动（API 级端到端）：忽略终态不重开、备注照常更新 ----------


def test_admin_ignore_then_user_down_keeps_ignored_but_comment_updates(client, session) -> None:
    """④ 端到端联动：用户 down → 队列'待处理'；admin PATCH '已忽略'；用户再 down
    → 状态保持'已忽略'不重开（W3 Task 1 裁决），category/comment 更新为最新值并
    在队列可见（信息不丢）。全程走真实端点（反馈 + 处置 + 队列）。"""
    make_user(session, "admin_link", "right-one", "all", role="admin")
    alice = make_user(session, "alice_link", "right-one", "service", role="member")
    conv = Conversation(user_id=alice.id, dept_id=alice.dept_id)
    session.add(conv)
    session.flush()
    answer = _ask(
        session,
        conv,
        alice,
        "客服工时是几点？",
        _at(2, 8),
        answer="答：9 点上班。",
        answer_type="rag",
    )

    assert login_as(client, "alice_link", "right-one").status_code == 200
    first = client.post(
        f"/api/messages/{answer.id}/feedback",
        json={"feedback": "down", "category": "答案错误", "comment": "第一版备注"},
    )
    assert first.status_code == 200
    assert first.json()["data"]["badcaseStatus"] == "待处理"

    assert login_as(client, "admin_link", "right-one").status_code == 200
    patched = client.patch(
        f"/api/admin/badcases/{answer.id}", json={"badcaseStatus": "已忽略", "note": "产品已知问题"}
    )
    assert patched.status_code == 200
    assert patched.json()["data"] == {"messageId": answer.id, "badcaseStatus": "已忽略"}

    assert login_as(client, "alice_link", "right-one").status_code == 200
    again = client.post(
        f"/api/messages/{answer.id}/feedback",
        json={"feedback": "down", "category": "引用无关", "comment": "第二版备注"},
    )
    assert again.status_code == 200
    assert again.json()["data"]["badcaseStatus"] == "已忽略"  # 终态不被用户重开（W3 裁决）

    assert login_as(client, "admin_link", "right-one").status_code == 200
    items = _get_data(client, {"badcaseStatus": "已忽略"})["items"]
    assert len(items) == 1
    assert items[0] == {
        "messageId": answer.id,
        "createdAt": answer.created_at.isoformat(),
        "deptCode": "service",
        "questionPreview": "客服工时是几点？",
        "category": "引用无关",  # 备注与类型照常更新为最新值
        "comment": "第二版备注",
        "badcaseStatus": "已忽略",
    }


# ---------- ⑤ 权限门（§5.5）：未登录 401、member 403 ----------


def test_member_and_anonymous_are_rejected(client, session) -> None:
    """⑤ 未登录 → 401/2003；member 已登录 → 403/2004（列表与处置一致，§5.5 依赖
    链）；被拒请求零写入（不留 badcase.update 审计）。"""
    d = _seed_dataset(session)

    rejected = (
        client.get("/api/admin/badcases"),
        client.patch(f"/api/admin/badcases/{d.b1.id}", json={"badcaseStatus": "已忽略"}),
    )
    for resp in rejected:
        assert (resp.status_code, resp.json()["code"]) == (401, ERR_UNAUTHENTICATED)

    make_user(session, "plain_member", "right-one", "service", role="member")
    assert login_as(client, "plain_member", "right-one").status_code == 200
    forbidden = (
        client.get("/api/admin/badcases"),
        client.patch(f"/api/admin/badcases/{d.b1.id}", json={"badcaseStatus": "已忽略"}),
    )
    for resp in forbidden:
        assert (resp.status_code, resp.json()["code"]) == (403, ERR_FORBIDDEN)

    session.expire_all()
    assert badcase_audits(session) == []  # 越权请求零写入
