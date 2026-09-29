r"""管理端运营概览（F-704）+ 语料健康度（F-1009）API 测试（Task 11，brief ①-④）。

口径（brief 钉死）：overview 聚合 assistant 且 is_eval=false 且 created_at >= now-days
（无 is_active 过滤——中断占位行也计入 questions，其 answer_type NULL 不进命中/拒答
分子）；命中率=qa+rag 占比、refuseRate=refuse 占比、downRate=down 占比、tokens=
sum(prompt+completion)，比率四舍五入 4 位小数（0-1 浮点）；byDept 按部门分组，无数据
部门不出现。health：totalDocs=deleted_at IS NULL、readyRate=ready/totalDocs、
pendingConfirm=is_active=false 且未软删、零召回=is_active+ready+created_at<now-30d
且全块 last_hit_at 均旧于 30 天（NOT EXISTS 反连接，恰好 30 天不进榜）、
expiringSoon=expiry_date<=today+7 且 active 未软删（含已到期未下线）。时间基准全部
取注入时钟（FakeClock 前拨构造 30 天/7 天边界两侧）；断言全部整包精确对比（数字手工
可算），is_eval 数据若泄漏任何一格数字即失配。
"""

import datetime as dt
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID, ERR_UNAUTHENTICATED
from app.db.models import Chunk, Conversation, Department, Document, Message, User

_CHUNK_VEC = [0.0] * 1024  # 聚合不涉向量运算：零向量直插满足 NOT NULL（W3 既有手法）
_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def make_user(session: Session, username: str, dept_code: str, role: str) -> User:
    """直插测试用户（bcrypt 哈希），指定部门编码与角色。"""
    from app.core.security import hash_password

    dept = session.scalars(select(Department).where(Department.code == dept_code)).one()
    user = User(
        username=username,
        password_hash=hash_password("right-one"),
        dept_id=dept.id,
        role=role,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _as_admin(client, session: Session) -> None:
    """建 admin（免首登改密）并登录；每个用例独立建号避免用户名冲突。"""
    make_user(session, "admin_w3", "all", role="admin")
    assert login(client, "admin_w3", "right-one").status_code == 200


def _ask(
    session: Session, conv: Conversation, user: User, asked_at: dt.datetime, **answer_kw
) -> Message:
    """插一组（user 问 + assistant 答）返回回答行：形态同 api/chat._persist_placeholder；
    is_eval 双行同置（评测引擎口径），聚合只看 assistant 行。"""
    is_eval = answer_kw.pop("is_eval", False)
    q = Message(
        conversation_id=conv.id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="user",
        content="问：" + str(answer_kw.get("answer", "")),
        created_at=asked_at,
        is_eval=is_eval,
    )
    a = Message(
        conversation_id=conv.id,
        user_id=user.id,
        dept_id=user.dept_id,
        role="assistant",
        content=str(answer_kw.pop("answer", "")),
        created_at=asked_at,
        is_eval=is_eval,
        **answer_kw,
    )
    session.add_all([q, a])
    session.flush()
    return a


def _seed_overview_dataset(session: Session) -> None:
    """①② 混合数据集（时钟 2026-10-01T08:00Z，days=7 窗口起点 09-24T08:00）：

    service 5 条：恰在窗口起点（>= 含边界）、点踩、拒答（3 天前——days=1 窗口外）、
    点赞、中断占位行（answer_type NULL，计入 questions 不进分子）；hr 5 条全在窗口
    内；另插恰在窗口起点前 1 秒的 hr 行（days=7 也不含）；is_eval 行若被计入将使 hr
    的 questions/tokens 全部失配（9999 tokens 故意夸大）。
    """
    alice = make_user(session, "alice_ov", "service", role="member")
    bob = make_user(session, "bob_ov", "hr", role="member")
    conv_svc = Conversation(user_id=alice.id, dept_id=alice.dept_id)
    conv_hr = Conversation(user_id=bob.id, dept_id=bob.dept_id)
    session.add_all([conv_svc, conv_hr])
    session.flush()

    def at(day: int, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
        return dt.datetime(2026, 9, day, hour, minute, second, tzinfo=dt.UTC)

    _ask(
        session,
        conv_svc,
        alice,
        at(24, 8),
        answer="a",
        answer_type="rag",
        prompt_tokens=5,
        completion_tokens=5,
    )  # 恰在 now-7d：>= 含边界
    _ask(
        session,
        conv_svc,
        alice,
        at(25, 10),
        answer="a",
        answer_type="rag",
        feedback="down",
        prompt_tokens=100,
        completion_tokens=50,
    )
    _ask(
        session,
        conv_svc,
        alice,
        at(28, 10),
        answer="a",
        answer_type="refuse",
        prompt_tokens=80,
        completion_tokens=20,
    )  # 3 天前：days=1 不含
    _ask(
        session,
        conv_svc,
        alice,
        at(30, 9),
        answer="a",
        answer_type="qa",
        feedback="up",
        prompt_tokens=30,
        completion_tokens=12,
    )
    _ask(session, conv_svc, alice, at(30, 9, 30), answer="占位", is_active=False)  # 中断行
    _ask(
        session,
        conv_hr,
        bob,
        at(26, 8),
        answer="a",
        answer_type="qa",
        feedback="down",
        prompt_tokens=200,
        completion_tokens=100,
    )
    _ask(
        session,
        conv_hr,
        bob,
        at(27, 8),
        answer="a",
        answer_type="rag",
        prompt_tokens=1,
        completion_tokens=4,
    )
    _ask(
        session,
        conv_hr,
        bob,
        at(29, 8),
        answer="a",
        answer_type="rag",
        prompt_tokens=10,
        completion_tokens=5,
    )
    _ask(session, conv_hr, bob, at(29, 10), answer="a", answer_type="qa", feedback="up")
    _ask(session, conv_hr, bob, at(29, 12), answer="a", answer_type="rag")
    _ask(
        session,
        conv_hr,
        bob,
        at(24, 7, 59, 59),
        answer="a",
        answer_type="qa",
        prompt_tokens=2,
        completion_tokens=3,
    )  # 窗口起点前 1 秒：days=7 也不含
    _ask(
        session,
        conv_hr,
        bob,
        at(29, 7),
        answer="a",
        answer_type="refuse",
        feedback="down",
        prompt_tokens=9999,
        completion_tokens=9999,
        is_eval=True,
    )  # 评测：恒不计入


def _overview_data(client, params: dict | None = None) -> dict:
    resp = client.get("/api/admin/overview", params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["code"], body["message"]) == (0, "ok")
    return body["data"]


# ---------- ① totals 与 byDept 逐项手工核对、is_eval 不计入 ----------


def test_overview_totals_and_by_dept_hand_computed(client, session) -> None:
    """① days=7 整包精确对比：totals questions=10、命中率 0.8、拒答率 0.1、点踩率
    0.2、tokens=622；byDept 仅出现有数据的 hr/service（hr 在前：deptCode 升序），
    中断行计入 questions、is_eval 行整体不可见；缺省 days 即近 7 天。"""
    _seed_overview_dataset(session)
    _as_admin(client, session)

    expected = {
        "totals": {
            "questions": 10,
            "hitRate": 0.8,
            "refuseRate": 0.1,
            "downRate": 0.2,
            "tokens": 622,
        },
        "byDept": [
            {
                "deptCode": "hr",
                "questions": 5,
                "hitRate": 1.0,
                "refuseRate": 0.0,
                "downRate": 0.2,
                "tokens": 320,
            },
            {
                "deptCode": "service",
                "questions": 5,
                "hitRate": 0.6,
                "refuseRate": 0.2,
                "downRate": 0.2,
                "tokens": 302,
            },
        ],
    }
    assert _overview_data(client, {"days": 7}) == expected
    assert _overview_data(client) == expected  # 缺省即近 7 天


# ---------- ② days=1 不含 3 天前消息、days=7 含窗口起点边界；非法 days 422 ----------


def test_overview_days_window_and_param_validation(client, session) -> None:
    """② days=1（窗口 09-30T08:00 起）仅剩 service 的 qa+中断两条，3 天前拒答与整个
    hr 部门不出现（无数据部门不出现）；days=7 恰含窗口起点消息（起点前 1 秒不含）；
    days=0/3/8/x → 422/1003。"""
    _seed_overview_dataset(session)
    _as_admin(client, session)

    assert _overview_data(client, {"days": 1}) == {
        "totals": {
            "questions": 2,
            "hitRate": 0.5,
            "refuseRate": 0.0,
            "downRate": 0.0,
            "tokens": 42,
        },
        "byDept": [
            {
                "deptCode": "service",
                "questions": 2,
                "hitRate": 0.5,
                "refuseRate": 0.0,
                "downRate": 0.0,
                "tokens": 42,
            },
        ],
    }
    by_dept = _overview_data(client, {"days": 7})["byDept"]
    assert by_dept[0]["deptCode"] == "hr" and by_dept[0]["questions"] == 5  # 起点 1 秒前不含
    assert by_dept[1]["deptCode"] == "service" and by_dept[1]["questions"] == 5  # 起点恰含
    for bad in ("0", "3", "8", "x"):
        resp = client.get("/api/admin/overview", params={"days": bad})
        assert resp.status_code == 422, bad
        assert resp.json()["code"] == ERR_PARAM_INVALID


def _make_doc(
    session: Session,
    title: str,
    dept_code: str,
    *,
    status: str = "ready",
    is_active: bool = True,
    created_at: dt.datetime | None = None,
    expiry_date: dt.date | None = None,
    deleted_at: dt.datetime | None = None,
) -> Document:
    """直插文档（content_hash 以标题派生保唯一——软删不占 hash 的部分唯一索引）。"""
    admin_id = session.scalar(select(User.id).where(User.username == "admin_w3"))
    dept_id = session.scalar(select(Department.id).where(Department.code == dept_code))
    doc = Document(
        title=title,
        dept_id=dept_id,
        original_filename=f"{title}.docx",
        file_path=f"t11/{hashlib.sha256(title.encode()).hexdigest()}.docx",
        mime=_DOC_MIME,
        size_bytes=16,
        content_hash=hashlib.sha256(title.encode()).hexdigest(),
        effective_date=dt.date(2026, 1, 1),
        expiry_date=expiry_date,
        owner_name="T11责任人",
        status=status,
        is_active=is_active,
        deleted_at=deleted_at,
        uploaded_by=admin_id,
    )
    if created_at is not None:
        doc.created_at = created_at
    session.add(doc)
    session.flush()
    return doc


def _add_chunk(session: Session, doc: Document, last_hit_at: dt.datetime | None) -> Chunk:
    """直插单块：last_hit_at 为 None 即从未被召回（零召回口径的块侧）。"""
    chunk = Chunk(
        document_id=doc.id,
        dept_id=doc.dept_id,
        title_path=doc.title,
        content=f"{doc.title}-块0",
        content_chars=len(f"{doc.title}-块0"),
        position=0,
        embedding=_CHUNK_VEC,
        last_hit_at=last_hit_at,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _seed_health_dataset(session: Session) -> dict[str, Document]:
    """③ 健康度数据集（时钟 2026-10-01T08:00Z：now-30d=09-01T08:00，today=10-01）：

    d1 就绪 42 天+近 3 天有召回（不进零召回榜）；d2 同龄从未召回（进榜）；d3 就绪 3 天
    新文档+expiry=+7（只进待过期）；d4 待确认 47 天零召回（inactive 不进零召回，进
    pendingConfirm）；d5 pending+expiry=+9；d6 failed；d7 软删（任何口径都不计）；
    d8 就绪 92 天仅剩 30 天线前的陈旧召回（进榜——旧命中救不了）；d9 已到期未下线
    （近 3 天有召回不进零召回榜，进待过期）；d10 expiry=+8 差一天；d11 待确认+临过期
    （inactive 不进待过期）。
    """
    docs = {
        "d1": _make_doc(
            session,
            "T11H-就绪近期召回",
            "hr",
            created_at=dt.datetime(2026, 8, 20, 8, tzinfo=dt.UTC),
        ),
        "d2": _make_doc(
            session,
            "T11H-零召回从未命中",
            "hr",
            created_at=dt.datetime(2026, 8, 20, 8, tzinfo=dt.UTC),
        ),
        "d3": _make_doc(
            session,
            "T11H-新文档临过期",
            "service",
            created_at=dt.datetime(2026, 9, 28, tzinfo=dt.UTC),
            expiry_date=dt.date(2026, 10, 8),
        ),
        "d4": _make_doc(
            session,
            "T11H-待确认零召回",
            "service",
            created_at=dt.datetime(2026, 8, 15, tzinfo=dt.UTC),
            is_active=False,
        ),
        "d5": _make_doc(
            session,
            "T11H-解析中",
            "all",
            created_at=dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
            status="pending",
            expiry_date=dt.date(2026, 10, 10),
        ),
        "d6": _make_doc(
            session,
            "T11H-解析失败",
            "all",
            created_at=dt.datetime(2026, 9, 20, 1, tzinfo=dt.UTC),
            status="failed",
        ),
        "d7": _make_doc(
            session,
            "T11H-已软删",
            "all",
            created_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
            is_active=False,
            deleted_at=dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
        ),
        "d8": _make_doc(
            session,
            "T11H-零召回陈旧命中",
            "service",
            created_at=dt.datetime(2026, 7, 1, 8, tzinfo=dt.UTC),
        ),
        "d9": _make_doc(
            session,
            "T11H-已到期未下线",
            "hr",
            created_at=dt.datetime(2026, 8, 1, 8, tzinfo=dt.UTC),
            expiry_date=dt.date(2026, 9, 25),
        ),
        "d10": _make_doc(
            session,
            "T11H-差一天临期",
            "hr",
            created_at=dt.datetime(2026, 9, 5, tzinfo=dt.UTC),
            expiry_date=dt.date(2026, 10, 9),
        ),
        "d11": _make_doc(
            session,
            "T11H-待确认临期",
            "service",
            created_at=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
            is_active=False,
            expiry_date=dt.date(2026, 10, 5),
        ),
    }
    _add_chunk(session, docs["d1"], dt.datetime(2026, 9, 28, tzinfo=dt.UTC))  # 近 3 天召回
    _add_chunk(session, docs["d2"], None)  # 从未召回
    _add_chunk(session, docs["d8"], dt.datetime(2026, 8, 1, tzinfo=dt.UTC))  # 陈旧召回
    _add_chunk(session, docs["d9"], dt.datetime(2026, 9, 28, tzinfo=dt.UTC))  # 近期召回
    return docs


def _health_data(client) -> dict:
    resp = client.get("/api/admin/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["code"], body["message"]) == (0, "ok")
    return body["data"]


# ---------- ③ 健康度：ready/待确认/零召回/待过期 整包手工核对 ----------


def test_health_counters_zero_recall_and_expiring_lists(client, session) -> None:
    """③ totalDocs=10（软删不计）、readyCount=8（待确认的 ready 也计——口径只看
    status+deleted_at）、readyRate=0.8、pendingConfirm=2；零召回榜按 created_at 升序
    [d8(service), d2(hr)]——近期召回/太新/inactive/软删都不进；待过期按 expiry 升序
    [d9(已到期未下线), d3(恰 today+7)]，+8/pending/inactive 不进。"""
    _as_admin(client, session)
    d = _seed_health_dataset(session)

    assert _health_data(client) == {
        "totalDocs": 10,
        "readyCount": 8,
        "readyRate": 0.8,
        "pendingConfirm": 2,
        "zeroRecall": [
            {
                "id": d["d8"].id,
                "title": "T11H-零召回陈旧命中",
                "deptCode": "service",
                "createdAt": d["d8"].created_at.isoformat(),
            },
            {
                "id": d["d2"].id,
                "title": "T11H-零召回从未命中",
                "deptCode": "hr",
                "createdAt": d["d2"].created_at.isoformat(),
            },
        ],
        "expiringSoon": [
            {"id": d["d9"].id, "title": "T11H-已到期未下线", "expiryDate": "2026-09-25"},
            {"id": d["d3"].id, "title": "T11H-新文档临过期", "expiryDate": "2026-10-08"},
        ],
    }


# ---------- ③b 恰好 30 天双边界：FakeClock 前拨构造榜单两侧 ----------


def test_zero_recall_exactly_30_days_boundary_via_clock(session, client, fake_clock) -> None:
    """恰好 30 天双边界（created_at < now-30d 严格小于、chunk last_hit_at >= now-30d
    即算近期召回）：时钟钉在 10-01T00:00 时，恰 30 天前建的文档与恰在 30 天线被召回
    过的文档都不进榜；前拨 1 秒后两者同时进榜（created_at 升序）。"""
    fake_clock.set("2026-10-01T00:00:00+00:00")
    _as_admin(client, session)
    edge = _make_doc(
        session, "T11H-恰好30天", "hr", created_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    )
    hit = _make_doc(
        session, "T11H-边界召回", "hr", created_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    )
    _add_chunk(session, hit, dt.datetime(2026, 9, 1, tzinfo=dt.UTC))  # 恰在 now-30d 召回

    assert _health_data(client)["zeroRecall"] == []  # 两侧都恰好压线：不进榜

    fake_clock.advance(seconds=1)  # 前拨 1 秒：created_at/last_hit_at 同时落线另一侧
    listed = _health_data(client)["zeroRecall"]
    assert [item["id"] for item in listed] == [hit.id, edge.id]


# ---------- ④ expiringSoon 边界：today+7 进、+8 不进 ----------


def test_expiring_soon_seven_day_boundary(client, session, fake_clock) -> None:
    """expiry=today+7（含当日）进榜、today+8 差一天不进；today 取注入时钟当日。"""
    fake_clock.set("2026-10-01T08:00:00+00:00")
    _as_admin(client, session)
    inside = _make_doc(session, "T11E-恰七天", "hr", expiry_date=dt.date(2026, 10, 8))
    _make_doc(session, "T11E-八天", "hr", expiry_date=dt.date(2026, 10, 9))

    assert _health_data(client)["expiringSoon"] == [
        {"id": inside.id, "title": "T11E-恰七天", "expiryDate": "2026-10-08"},
    ]


# ---------- ⑤ 权限门（§5.5）：未登录 401、member 403 ----------


def test_member_and_anonymous_are_rejected(client, session) -> None:
    """两端点一致：未登录 401/2003；member 已登录 403/2004（§5.5 依赖链）。"""
    for path in ("/api/admin/overview", "/api/admin/health"):
        resp = client.get(path)
        assert (resp.status_code, resp.json()["code"]) == (401, ERR_UNAUTHENTICATED), path

    make_user(session, "plain_ov", "service", role="member")
    assert login(client, "plain_ov", "right-one").status_code == 200
    for path in ("/api/admin/overview", "/api/admin/health"):
        resp = client.get(path)
        assert (resp.status_code, resp.json()["code"]) == (403, ERR_FORBIDDEN), path
