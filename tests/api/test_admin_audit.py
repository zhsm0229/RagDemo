r"""审计查询 API 测试（F-802，前端 P11 审计日志页后端）。

覆盖：列表（created_at DESC + 同刻 id 降序兜底、分页、pageSize 上限 422、item 键
camelCase 全集、actorUsername 经 join users 解析）；筛选组合（actorId/action/
objectType 与 dateFrom/dateTo 含首尾日的 UTC 整日边界）；CSV 导出（UTF-8 BOM、
中文列头、detail 紧凑 JSON 单列、objectId/ip 空值出空单元格、Content-Disposition
文件名 audit-{yyyymmdd}.csv、行数上限截断 X-Truncated=true——上限经模块常量注入
小值验证）；member 全端点 403 与未登录 401。
fixtures：tests/api/conftest.py 的 client/fake_clock；审计行经 record_audit 直插
测试事务（注入独立 FakeClock 定格 created_at）；登录不发审计（仅锁定/改密发），
total 可精确断言。
"""

import csv
import io
import json

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import admin_audit as audit_api  # 截断上限注入点：端点运行期读模块常量
from app.core.clock import FakeClock, get_clock
from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID, ERR_UNAUTHENTICATED
from app.core.security import hash_password
from app.db.models import Department, User
from app.main import app
from app.services.audit import record_audit

_BASE = "/api/admin/audit-logs"


# ---------- 构造与登录辅助（口径同 tests/api/test_admin_users.py） ----------


def make_user(
    session: Session,
    username: str,
    password: str = "right-one",
    role: str = "member",
) -> User:
    """直插测试用户（admin 测试文件同款辅助），挂在测试事务内。"""
    dept = session.scalars(select(Department).where(Department.code == "all")).first()
    user = User(
        username=username,
        password_hash=hash_password(password),
        dept_id=dept.id,
        role=role,
        must_change_password=False,  # 列 server_default=true：不显式给值会触发首登改密门
    )
    session.add(user)
    session.flush()
    return user


def login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def admin_login(client, session: Session) -> User:
    """创建并登录 admin（cookie 由 client 保持），返回该用户供 actor 断言。"""
    admin = make_user(session, "审计管理员", "right-one", role="admin")
    assert login(client, "审计管理员", "right-one").status_code == 200
    return admin


def seed_audit(
    session: Session,
    actor: User,
    action: str,
    object_type: str,
    *,
    object_id: int | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    at: str = "2026-09-10T10:00:00+00:00",
) -> None:
    """直插一条审计（record_audit 契约：只 flush，事务归测试夹具回滚兜底）。"""
    record_audit(
        session,
        actor.id,
        action,
        object_type,
        object_id,
        detail if detail is not None else {},
        ip=ip,
        clock=FakeClock(at),
    )


def seed_list_case(session: Session, admin: User, member: User) -> None:
    """列表用例共用 4 条种子：三个日期 + 同刻两行（id 兜底断言依赖后插入的 eval.run）。"""
    seed_audit(
        session,
        member,
        "user.create",
        "user",
        object_id=member.id,
        detail={"username": "被审计员工"},
        ip="10.0.0.1",
        at="2026-09-10T10:00:00+00:00",
    )
    seed_audit(
        session, admin, "document.upload", "document", object_id=7, at="2026-09-11T10:00:00+00:00"
    )
    seed_audit(
        session,
        admin,
        "qa.import",
        "qa_pair",
        object_id=21,
        detail={"count": 5},
        at="2026-09-12T10:00:00+00:00",
    )
    seed_audit(
        session,
        admin,
        "eval.run",
        "eval_run",
        object_id=33,
        detail={"runId": 9},
        at="2026-09-12T10:00:00+00:00",
    )


def seed_filter_case(session: Session) -> tuple[User, User]:
    """筛选用例共用 4 条种子：两个操作人 × 不同动作/对象类型/日期，返回两操作人。"""
    actor_a = make_user(session, "操作员甲")
    actor_b = make_user(session, "操作员乙")
    seed_audit(
        session,
        actor_a,
        "user.create",
        "user",
        object_id=actor_a.id,
        at="2026-09-10T10:00:00+00:00",
    )
    seed_audit(
        session, actor_a, "document.upload", "document", object_id=7, at="2026-09-12T10:00:00+00:00"
    )
    seed_audit(
        session, actor_b, "user.create", "user", object_id=99, at="2026-09-14T10:00:00+00:00"
    )
    seed_audit(
        session, actor_b, "user.toggle", "user", object_id=99, at="2026-09-16T10:00:00+00:00"
    )
    return actor_a, actor_b


def seed_export_case(session: Session, admin: User) -> User:
    """导出用例种子：一条全字段 user.create + 一条空 objectId/ip 的 document.upload，
    返回全字段行的操作人供单元格断言。"""
    actor = make_user(session, "操作员丙")
    seed_audit(
        session,
        actor,
        "user.create",
        "user",
        object_id=actor.id,
        detail={"username": "张三", "note": "含,逗号"},
        ip="192.168.1.9",
        at="2026-09-10T10:00:00+00:00",
    )
    seed_audit(
        session,
        admin,
        "document.upload",
        "document",
        object_id=None,
        detail={},
        at="2026-09-11T10:00:00+00:00",
    )
    return actor


# ---------- 列表：降序（同刻 id 兜底）/ 字段 / 分页 ----------


def test_list_desc_order_and_same_time_tiebreak(client, session) -> None:
    """列表排序：created_at 降序；同刻两行按 id 降序兜底（后插入的 eval.run 在前）。"""
    admin = admin_login(client, session)
    member = make_user(session, "被审计员工")
    seed_list_case(session, admin, member)
    data = client.get(_BASE).json()["data"]
    assert (data["total"], data["page"], data["pageSize"]) == (4, 1, 20)
    assert [i["action"] for i in data["items"]] == [
        "eval.run",
        "qa.import",
        "document.upload",
        "user.create",
    ]


def test_list_item_fields_camel_case(client, session) -> None:
    """item 键为 camelCase 全集；actorUsername 经 join users 解析、detail JSONB
    原样、ip 可空、createdAt ISO 格式。"""
    admin = admin_login(client, session)
    member = make_user(session, "被审计员工")
    seed_list_case(session, admin, member)
    items = client.get(_BASE).json()["data"]["items"]
    assert set(items[0]) == {
        "id",
        "actorUsername",
        "action",
        "objectType",
        "objectId",
        "detail",
        "ip",
        "createdAt",
    }
    first = items[0]  # 同刻两行中 id 更大的 eval.run
    assert first["actorUsername"] == "审计管理员"
    assert first["objectType"] == "eval_run"
    assert first["objectId"] == 33
    assert first["detail"] == {"runId": 9}
    assert first["ip"] is None
    assert first["createdAt"] == "2026-09-12T10:00:00+00:00"
    oldest = items[3]
    assert oldest["actorUsername"] == "被审计员工"
    assert oldest["objectId"] == member.id
    assert oldest["ip"] == "10.0.0.1"


def test_list_pagination_and_page_size_cap(client, session) -> None:
    """分页：page=2&pageSize=3 只剩最旧一条；pageSize 超 100 上限 422/1003。"""
    admin = admin_login(client, session)
    member = make_user(session, "被审计员工")
    seed_list_case(session, admin, member)
    page2 = client.get(_BASE, params={"page": 2, "pageSize": 3}).json()["data"]
    assert (page2["total"], len(page2["items"])) == (4, 1)
    assert page2["items"][0]["action"] == "user.create"  # 第二页只剩最旧一条
    bad = client.get(_BASE, params={"pageSize": 101})
    assert bad.status_code == 422
    assert bad.json()["code"] == ERR_PARAM_INVALID


# ---------- 筛选：actorId / action / objectType / 日期含首尾日 ----------


def test_filter_actor_action_object_type_and_combination(client, session) -> None:
    """筛选：actorId/action/objectType 单项精确命中；两项组合叠加收敛到一条。"""
    admin_login(client, session)  # 登录态仅过权限门，本用例不消费 admin 对象
    actor_a, actor_b = seed_filter_case(session)
    by_actor = client.get(_BASE, params={"actorId": actor_a.id}).json()["data"]
    assert by_actor["total"] == 2
    assert [i["action"] for i in by_actor["items"]] == ["document.upload", "user.create"]
    by_action = client.get(_BASE, params={"action": "user.create"}).json()["data"]
    assert by_action["total"] == 2
    assert {i["actorUsername"] for i in by_action["items"]} == {"操作员甲", "操作员乙"}
    by_type = client.get(_BASE, params={"objectType": "document"}).json()["data"]
    assert (by_type["total"], by_type["items"][0]["objectId"]) == (1, 7)
    combined = client.get(_BASE, params={"actorId": actor_b.id, "action": "user.create"}).json()[
        "data"
    ]
    assert combined["total"] == 1
    assert combined["items"][0]["objectId"] == 99


def test_filter_date_range_inclusive_utc_boundaries(client, session) -> None:
    """日期筛选：dateFrom/dateTo 均含首尾日（UTC 整日边界，同 admin_views 口径）；
    窗口外与未知动作返回 total=0 且 items 空。"""
    admin_login(client, session)
    seed_filter_case(session)
    from_day = client.get(_BASE, params={"dateFrom": "2026-09-12"}).json()["data"]
    assert from_day["total"] == 3  # 首日 09-12 当天记录含在内
    to_day = client.get(_BASE, params={"dateTo": "2026-09-13"}).json()["data"]
    assert to_day["total"] == 2  # 截止日 09-13 全天含在内（09-12 行不丢）
    both = client.get(_BASE, params={"dateFrom": "2026-09-12", "dateTo": "2026-09-14"}).json()[
        "data"
    ]
    assert both["total"] == 2  # 首尾日都闭区间
    outside = client.get(_BASE, params={"dateFrom": "2026-09-17", "dateTo": "2026-09-18"}).json()[
        "data"
    ]
    assert (outside["total"], outside["items"]) == (0, [])
    unknown = client.get(_BASE, params={"action": "no.such_action"}).json()["data"]
    assert (unknown["total"], unknown["items"]) == (0, [])


# ---------- CSV 导出：BOM / 中文列头 / 逐列内容 / 文件名 / 截断 ----------


def test_export_csv_bom_headers_filename_and_empty_cells(client, session) -> None:
    """导出框架：200 text/csv；文件名 audit-{yyyymmdd}.csv（注入时钟当日）；非截断
    X-Truncated=false；正文以 UTF-8 BOM 开头（Excel 直开不乱码）；首行中文列头；
    objectId/ip 为空的行出空单元格（非 "None" 字样）。"""
    admin = admin_login(client, session)
    seed_export_case(session, admin)
    resp = client.get(f"{_BASE}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    expect_name = f"audit-{get_clock().now().strftime('%Y%m%d')}.csv"
    assert f'filename="{expect_name}"' in resp.headers["content-disposition"]
    assert resp.headers["x-truncated"] == "false"
    assert resp.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert len(rows) == 3  # 表头 + 2 条数据，无空行
    assert rows[0] == ["时间", "操作人", "动作", "对象类型", "对象ID", "IP", "详情"]
    # 最新在前：objectId/ip 为空的行出空单元格，detail 空字典出 "{}"
    assert rows[1] == [
        "2026-09-11T10:00:00+00:00",
        "审计管理员",
        "document.upload",
        "document",
        "",
        "",
        "{}",
    ]


def test_export_columns_and_detail_compact_json(client, session) -> None:
    """数据行逐列：时间/操作人/动作/对象类型/对象ID/IP 依次精确；详情列为紧凑
    JSON 字符串（无空格、中文不转义），键序经 PG JSONB 规范化故按语义比对。"""
    admin = admin_login(client, session)
    actor = seed_export_case(session, admin)
    rows = list(csv.reader(io.StringIO(client.get(f"{_BASE}/export").content.decode("utf-8-sig"))))
    assert rows[2][:6] == [
        "2026-09-10T10:00:00+00:00",
        "操作员丙",
        "user.create",
        "user",
        str(actor.id),
        "192.168.1.9",
    ]
    detail_cell = rows[2][6]
    assert json.loads(detail_cell) == {"username": "张三", "note": "含,逗号"}
    assert ": " not in detail_cell and ", " not in detail_cell  # 紧凑序列化（无空格）
    assert "张三" in detail_cell  # ensure_ascii=False：中文原样可读


def test_export_truncates_with_header_flag(client, session, monkeypatch) -> None:
    """导出截断（上限保护）：行数超上限时只出前 max_rows 行（降序取最新）并标
    X-Truncated=true；上限为可注入参数，测试传 2 验证（生产缺省 10 万）。"""
    admin_login(client, session)
    actor = make_user(session, "截断对象")
    seed_audit(session, actor, "user.create", "user", at="2026-09-10T10:00:00+00:00")
    seed_audit(session, actor, "document.upload", "document", at="2026-09-12T10:00:00+00:00")
    seed_audit(session, actor, "qa.import", "qa_pair", at="2026-09-14T10:00:00+00:00")
    monkeypatch.setattr(audit_api, "EXPORT_LIMIT", 2)

    resp = client.get(f"{_BASE}/export")
    assert resp.status_code == 200
    assert resp.headers["x-truncated"] == "true"
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert len(rows) == 3  # 表头 + 截断后的 2 行
    assert [r[2] for r in rows[1:]] == ["qa.import", "document.upload"]  # 降序取最新 2 条


# ---------- 权限门：member 403 / 未登录 401 ----------


def test_member_forbidden_and_anonymous_unauthorized(client, session) -> None:
    """member 调列表/导出：一律 403/2004；未登录（无 cookie）一律 401/2003。"""
    make_user(session, "普通成员", "right-one", role="member")
    assert login(client, "普通成员", "right-one").status_code == 200
    for path in (_BASE, f"{_BASE}/export"):
        resp = client.get(path)
        assert resp.status_code == 403
        assert resp.json()["code"] == ERR_FORBIDDEN

    for resp in (TestClient(app).get(_BASE), TestClient(app).get(f"{_BASE}/export")):
        assert resp.status_code == 401
        assert resp.json()["code"] == ERR_UNAUTHENTICATED
