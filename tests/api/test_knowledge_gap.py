r"""知识缺口报告 API 测试（F-1010，W5）。

覆盖：周榜聚合口径（拒答 user 问题按 content 分组 count DESC——rag/qa/中断无回答
/is_eval 评测对/assistant 侧 is_eval 防御配对一律不计，total 为去重问题数、items
截前 50）；周界（UTC 周一为界的 [weekStart, weekEnd) 左闭右开，边界恰在起点的计入
、前一周的回看命中、终点行不计）；weekOffset 越界 422/1003；CSV 导出（UTF-8 BOM、
中文列头 问题/出现次数、公式首字符制表符防御、count DESC 行序、文件名
knowledge-gap-{yyyymmdd}.csv、超上限截断 X-Truncated=true——上限经模块常量注入小值
验证）；member 全端点 403 与未登录 401。
fixtures：tests/api/conftest.py 的 client/fake_clock（FakeClock 起点 2026-10-01 周四
→ 本周窗口 [2026-09-28, 2026-10-05) UTC）；问答对经真 ORM 直插（created_at 显式给值
——分区键入主键可直插，默认分区接收），插入按时间正序 → id 升序与时间序一致，
LATERAL 最小 id 配对与生产问答次序一致（同 tests/api/test_admin_messages.py 口径）。
"""

import csv
import io
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import admin_knowledge_gap as gap_api  # 截断上限注入点：端点运行期读模块常量
from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID, ERR_UNAUTHENTICATED
from app.core.security import hash_password
from app.db.models import Conversation, Department, Message, User
from app.main import app

_BASE = "/api/admin/knowledge-gap"

# FakeClock 缺省起点 2026-10-01（周四）：本周一为 09-28，窗口 [09-28, 10-05) UTC
_WEEK0_START = "2026-09-28T00:00:00+00:00"
_WEEK0_END = "2026-10-05T00:00:00+00:00"
_WEEK1_START = "2026-09-21T00:00:00+00:00"
_WEEK1_END = "2026-09-28T00:00:00+00:00"


# ---------- 构造与登录辅助（口径同 tests/api/test_admin_messages.py） ----------


def make_user(
    session: Session,
    username: str,
    password: str = "right-one",
    role: str = "member",
) -> User:
    """直插测试用户（bcrypt 哈希），挂 all 部门（本报告不按部门拆分）。"""
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
    """创建并登录 admin（cookie 由 client 保持）。"""
    admin = make_user(session, "缺口管理员", "right-one", role="admin")
    assert login(client, "缺口管理员", "right-one").status_code == 200
    return admin


def _ask(
    session: Session, conv: Conversation, user: User, question: str, asked_at: datetime, **answer_kw
) -> None:
    """插一组（user 问 + assistant 答）：与 api/chat._persist_placeholder 同形态
    （同事务先后发号，user 行 id 恒小于其回答行）；answer_kw 透传回答列
    （answer_type / is_active / is_eval）。is_eval 双行同置（评测引擎 Task 6 口径）。"""
    is_eval = answer_kw.pop("is_eval", False)
    session.add_all(
        [
            Message(
                conversation_id=conv.id,
                user_id=user.id,
                dept_id=user.dept_id,
                role="user",
                content=question,
                created_at=asked_at,
                is_eval=is_eval,
            ),
            Message(
                conversation_id=conv.id,
                user_id=user.id,
                dept_id=user.dept_id,
                role="assistant",
                content=answer_kw.pop("answer", ""),
                created_at=asked_at,
                is_eval=is_eval,
                **answer_kw,
            ),
        ]
    )
    session.flush()


def _conv(session: Session, user: User) -> Conversation:
    return Conversation(user_id=user.id, dept_id=user.dept_id)


def at(day: int, hour: int = 10, minute: int = 0, second: int = 0) -> datetime:
    """2026-09 月内 UTC 时刻构造（9 月覆盖三档周窗口）；跨月的 10-05 边界另行内联。"""
    return datetime(2026, 9, day, hour, minute, second, tzinfo=UTC)


# ---------- 聚合口径：只数配对回答为拒答的用户问题 ----------


def test_counts_only_refused_questions_grouped_by_content(client, session) -> None:
    """聚合口径：同问题跨用户/跨会话计数合并（发票 x2）；rag/qa 回答、中断无回答
    （占位行 is_active=False）一律不计；items 按 count 降序，total 为去重问题数，
    weekStart/weekEnd 为注入时钟推算的本周 UTC 周一界。"""
    admin_login(client, session)
    alice = make_user(session, "alice")
    bob = make_user(session, "bob")
    conv_a, conv_b = _conv(session, alice), _conv(session, bob)
    conv_c = _conv(session, alice)
    session.add_all([conv_a, conv_b, conv_c])
    session.flush()
    _ask(session, conv_a, alice, "发票抬头怎么改？", at(29), answer_type="refuse", answer="拒答")
    _ask(session, conv_b, bob, "发票抬头怎么改？", at(29, 11), answer_type="refuse", answer="拒答")
    _ask(session, conv_a, alice, "年假怎么请？", at(29, 12), answer_type="rag", answer="命中")
    _ask(session, conv_b, bob, "报销流程是什么？", at(29, 13), answer_type="qa", answer="命中")
    _ask(session, conv_c, alice, "班车路线有哪些？", at(29, 14))  # 中断：回答占位行未落
    # 中断占位行即使残留 refuse 也不算拒答（无有效回答 ≠ 拒答，配对防滑移口径）
    _ask(
        session,
        conv_c,
        alice,
        "中断的拒答不计",
        at(29, 15),
        answer="拒答",
        answer_type="refuse",
        is_active=False,
    )

    data = client.get(_BASE, params={"weekOffset": 0}).json()["data"]
    assert data["total"] == 1  # 去重问题数：仅发票一问
    assert data["items"] == [{"question": "发票抬头怎么改？", "count": 2}]
    assert data["weekStart"] == _WEEK0_START
    assert data["weekEnd"] == _WEEK0_END


def test_excludes_eval_pairs_and_eval_answered_pairing(client, session) -> None:
    """评测隔离（W3）：is_eval 问答对（双行同置）不计；真实问题紧邻的回答为评测行
    时（防御性：评测现走独立会话，防未来复用真实会话错配）不滑向评测行计数——
    该问配对落空即不计缺口，同 admin_views 配对口径。"""
    admin_login(client, session)
    alice = make_user(session, "alice")
    conv = _conv(session, alice)
    conv_eval = _conv(session, alice)
    session.add_all([conv, conv_eval])
    session.flush()
    _ask(
        session,
        conv_eval,
        alice,
        "【评测】发票抬头怎么改？",
        at(29),
        answer="【评测】拒答",
        answer_type="refuse",
        is_eval=True,
    )
    # 真实问题后紧跟评测回答（时间正序插入 → 评测行 id 大于问题行且为最小 assistant，
    # 但 is_eval 过滤后配对落空），其后无可配对回答
    _ask(session, conv, alice, "年假怎么请？", at(29, 10), answer="评测回答", is_eval=True)

    data = client.get(_BASE, params={"weekOffset": 0}).json()["data"]
    assert (data["total"], data["items"]) == (0, [])


def test_week_boundary_half_open_utc_monday(client, session) -> None:
    """周界：恰在 weekStart（周一 00:00）计入；上周日 23:59:59 不在本周、回看 -1
    命中；恰在 weekEnd（次周一 00:00）不计入本周。窗口 [weekStart, weekEnd) 左闭右开。"""
    admin_login(client, session)
    alice = make_user(session, "alice")
    conv = _conv(session, alice)
    session.add(conv)
    session.flush()
    _ask(session, conv, alice, "边界恰在起点", at(28, 0, 0, 0), answer_type="refuse", answer="拒答")
    _ask(
        session, conv, alice, "上周日深夜", at(27, 23, 59, 59), answer_type="refuse", answer="拒答"
    )
    _ask(
        session,
        conv,
        alice,
        "恰在终点",
        datetime(2026, 10, 5, 0, 0, 0, tzinfo=UTC),
        answer_type="refuse",
        answer="拒答",
    )

    this_week = client.get(_BASE, params={"weekOffset": 0}).json()["data"]
    assert this_week["total"] == 1  # 终点行（10-05 周一零点）与上周日行都落出本周
    assert [i["question"] for i in this_week["items"]] == ["边界恰在起点"]
    last_week = client.get(_BASE, params={"weekOffset": -1}).json()["data"]
    assert last_week["weekStart"] == _WEEK1_START
    assert last_week["weekEnd"] == _WEEK1_END
    assert [i["question"] for i in last_week["items"]] == ["上周日深夜"]


def test_week_offset_windows_are_disjoint(client, session) -> None:
    """周切换：同一问题在两周各出现一次，各窗口独立计数；前两周窗口无数据出空榜。"""
    admin_login(client, session)
    alice = make_user(session, "alice")
    conv = _conv(session, alice)
    session.add(conv)
    session.flush()
    _ask(session, conv, alice, "发票抬头怎么改？", at(22), answer_type="refuse", answer="拒答")
    _ask(session, conv, alice, "发票抬头怎么改？", at(29), answer_type="refuse", answer="拒答")
    week2 = client.get(_BASE, params={"weekOffset": -2}).json()["data"]
    assert (week2["total"], week2["items"]) == (0, [])
    week1 = client.get(_BASE, params={"weekOffset": -1}).json()["data"]
    assert week1["items"] == [{"question": "发票抬头怎么改？", "count": 1}]
    week0 = client.get(_BASE, params={"weekOffset": 0}).json()["data"]
    assert week0["items"] == [{"question": "发票抬头怎么改？", "count": 1}]


def test_top_limit_50_while_total_counts_all(client, session) -> None:
    """榜单截断：52 个去重拒答问题只出前 50（count 降序，同数按问题文本升序兜底），
    total 仍为 52。"""
    admin_login(client, session)
    alice = make_user(session, "alice")
    conv = _conv(session, alice)
    session.add(conv)
    session.flush()
    # q000 问 3 次（榜一）、q001 问 2 次（榜二）、q002..q051 各 1 次（52-2=50 个）
    for day in (15, 16, 17):
        _ask(session, conv, alice, "q000", at(day), answer_type="refuse", answer="拒答")
    _ask(session, conv, alice, "q001", at(18), answer_type="refuse", answer="拒答")
    _ask(session, conv, alice, "q001", at(19), answer_type="refuse", answer="拒答")
    for i in range(2, 52):
        _ask(
            session,
            conv,
            alice,
            f"q{i:03d}",
            at(20, hour=i % 24),
            answer_type="refuse",
            answer="拒答",
        )

    data = client.get(_BASE, params={"weekOffset": -2}).json()["data"]
    assert data["total"] == 52
    assert len(data["items"]) == 50
    assert data["items"][0] == {"question": "q000", "count": 3}
    assert data["items"][1] == {"question": "q001", "count": 2}
    assert [i["question"] for i in data["items"][2:]] == [f"q{i:03d}" for i in range(2, 50)]
    counts = [i["count"] for i in data["items"]]
    assert counts == sorted(counts, reverse=True)


def test_week_offset_out_of_range_rejected(client, session) -> None:
    """weekOffset 越界（正数未来周 / 回看超 11 周）：422/1003，两端口径一致。"""
    admin_login(client, session)
    for params in ({"weekOffset": 1}, {"weekOffset": -12}):
        resp = client.get(_BASE, params=params)
        assert resp.status_code == 422
        assert resp.json()["code"] == ERR_PARAM_INVALID
        resp_export = client.get(f"{_BASE}/export", params=params)
        assert resp_export.status_code == 422
        assert resp_export.json()["code"] == ERR_PARAM_INVALID


# ---------- CSV 导出：BOM / 中文列头 / 公式防御 / 截断 ----------


def test_export_csv_bom_headers_and_rows(client, session) -> None:
    """导出框架：200 text/csv；文件名 knowledge-gap-{yyyymmdd}.csv（注入时钟当日）；
    非截断 X-Truncated=false；正文以 UTF-8 BOM 开头（Excel 直开不乱码）；首行中文
    列头 问题/出现次数；数据行按 count 降序（导出为全量去重榜，不限前 50）。"""
    admin_login(client, session)
    alice = make_user(session, "alice")
    conv = _conv(session, alice)
    session.add(conv)
    session.flush()
    _ask(session, conv, alice, "发票抬头怎么改？", at(28), answer_type="refuse", answer="拒答")
    _ask(session, conv, alice, "发票抬头怎么改？", at(29), answer_type="refuse", answer="拒答")
    _ask(session, conv, alice, "年假怎么请？", at(29, 12), answer_type="refuse", answer="拒答")
    _ask(session, conv, alice, "不该出现的命中问题", at(29, 13), answer_type="rag", answer="命中")

    resp = client.get(f"{_BASE}/export", params={"weekOffset": 0})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    expect_name = f"knowledge-gap-{gap_api.get_clock().now().strftime('%Y%m%d')}.csv"
    assert f'filename="{expect_name}"' in resp.headers["content-disposition"]
    assert resp.headers["x-truncated"] == "false"
    assert resp.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert rows[0] == ["问题", "出现次数"]
    assert rows[1] == ["发票抬头怎么改？", "2"]
    assert rows[2] == ["年假怎么请？", "1"]
    assert len(rows) == 3  # 表头 + 2 条数据，拒答之外的行不出现


def test_export_csv_formula_prefix_defense(client, session) -> None:
    """CSV 公式注入防御：问题为用户自由文本，首字符为 = 时导出前置制表符强制按
    文本解释（audit_query 不变式在本模块落地），不改动问题原文本身（列表接口仍原样）。"""
    admin_login(client, session)
    alice = make_user(session, "alice")
    conv = _conv(session, alice)
    session.add(conv)
    session.flush()
    _ask(session, conv, alice, "=1+1 会不会算错", at(29), answer_type="refuse", answer="拒答")

    listed = client.get(_BASE).json()["data"]
    assert listed["items"][0]["question"] == "=1+1 会不会算错"  # 列表原样，不动用户原文
    rows = list(csv.reader(io.StringIO(client.get(f"{_BASE}/export").content.decode("utf-8-sig"))))
    assert rows[1] == ["\t=1+1 会不会算错", "1"]


def test_export_truncates_with_header_flag(client, session, monkeypatch) -> None:
    """导出截断（上限保护）：行数超上限时只出前 max_rows 行（count 降序取头部）并标
    X-Truncated=true；上限为可注入参数，测试传 1 验证（生产缺省 10 万）。"""
    admin_login(client, session)
    alice = make_user(session, "alice")
    conv = _conv(session, alice)
    session.add(conv)
    session.flush()
    _ask(session, conv, alice, "问题甲", at(28), answer_type="refuse", answer="拒答")
    _ask(session, conv, alice, "问题甲", at(29), answer_type="refuse", answer="拒答")
    _ask(session, conv, alice, "问题乙", at(29, 12), answer_type="refuse", answer="拒答")
    monkeypatch.setattr(gap_api, "EXPORT_LIMIT", 1)

    resp = client.get(f"{_BASE}/export")
    assert resp.status_code == 200
    assert resp.headers["x-truncated"] == "true"
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert len(rows) == 2  # 表头 + 截断后的 1 行
    assert rows[1] == ["问题甲", "2"]


# ---------- 权限门：member 403 / 未登录 401 ----------


def test_member_forbidden_and_anonymous_unauthorized(client, session) -> None:
    """member 调周榜/导出：一律 403/2004；未登录（无 cookie）一律 401/2003。"""
    make_user(session, "普通成员", "right-one", role="member")
    assert login(client, "普通成员", "right-one").status_code == 200
    for path in (_BASE, f"{_BASE}/export"):
        resp = client.get(path)
        assert resp.status_code == 403
        assert resp.json()["code"] == ERR_FORBIDDEN

    for resp in (TestClient(app).get(_BASE), TestClient(app).get(f"{_BASE}/export")):
        assert resp.status_code == 401
        assert resp.json()["code"] == ERR_UNAUTHENTICATED
