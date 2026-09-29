r"""管理端问答对 Excel 整批导入（F-401）与模板下载（F-403）API 测试。

brief 断言 ①-⑥ 全覆盖：合规 3 行 201 且读回（3 行 / embedding 非空 1024 维 /
batch_no 一致 / 审计 qa.import 在）；空答案行+超长答案行 → 422 data 恰 2 条
错误行且零入库（QA-02）；5001 行文件级拒（QA-03）；单答案 10001 字行级错
（QA-03）；空表报"无数据"（QA-08）；模板 200 attachment 且可被 read_rows 读回。
另固化：状态枚举与缺省 enabled、deptCode 缺省 all / 指定 service / 非法 422
（F-204 检索 dept_id IN (用户部门, all) 语义的写入侧约定）、embed 分批 ≤256、
member 403 门、表头结构错误透传、成功导入后整清问答精确缓存。

qa_client 固定 test_mode 先于 lifespan 就绪：导入向量化取 app.state.chat_deps
的同一 StubEmbedder（与检索同指纹空间），embed 批大小可精确断言。
"""

import re
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID
from app.db.models import AuditLog, Department, QaPair, User

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HEADERS = ["问题", "答案", "状态"]


# ---------- 构造与登录辅助（口径同 tests/api/test_documents.py） ----------


def make_user(session, username: str, password: str, role: str) -> User:
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


def admin_login(client, session) -> None:
    """创建并登录 admin（cookie 由 client 保持），失败即失败在显式断言。"""
    make_user(session, "qaadmin", "right-one", role="admin")
    assert login(client, "qaadmin", "right-one").status_code == 200


def _xlsx(rows: list[list[object]]) -> bytes:
    """测试内现造 xlsx 字节流：rows[0] 视为表头行，其余为数据行。"""
    wb = Workbook()
    ws = wb.worksheets[0]
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def post_import(client, rows: list[list[object]], dept_code: str | None = None):
    """multipart 导入请求：表单键名 deptCode 与 API 契约一致，缺省不传。"""
    data = {"deptCode": dept_code} if dept_code is not None else {}
    return client.post(
        "/api/admin/qa-pairs/import",
        data=data,
        files={"file": ("qa.xlsx", _xlsx([_HEADERS, *rows]), XLSX_MIME)},
    )


def _qa_pair_count(session) -> int:
    return session.scalar(select(func.count()).select_from(QaPair))


def _vec_dims(stored: object) -> int:
    """vector 列读回形态（psycopg 未注册 pgvector 适配）：文本 "[a,b,..]"，数维度。"""
    assert isinstance(stored, str)
    return len(stored.strip("[]").split(","))


@pytest.fixture()
def qa_client(test_mode, client):
    """test_mode 先于 lifespan 就绪（fixture 参数序即装配序）：chat_deps 以 Stub 三件套
    装配挂 app.state，导入端点拿到与检索同一 StubEmbedder，embed 调用可断言。"""
    return client


# ---------- ① 合规 3 行：201 + 读回 + 审计 ----------


def test_导入合规三行_201读回向量批次与审计(qa_client, session):
    """① 合规 3 行：201；读回 3 行含非空 1024 维向量、batch_no 全一致、审计 qa.import。"""
    admin_login(qa_client, session)
    rows = [
        ["员工如何重置密码？", "登录页点击忘记密码，按短信验证后重置。", "enabled"],
        ["发票抬头是什么？", "发票抬头为公司全称，可在 OA 个人信息页查看。", None],  # 状态缺省
        ["通勤补贴怎么申请？", "OA 流程库搜索「通勤补贴」提交申请。", "disabled"],
    ]
    resp = post_import(qa_client, rows)

    assert resp.status_code == 201
    data = resp.json()["data"]
    assert set(data) == {"imported", "batchNo"}
    assert data["imported"] == 3
    assert re.fullmatch(r"QA-20261001-[A-Z]{4}", data["batchNo"])  # fake_clock 当日+4位大写

    pairs = list(session.scalars(select(QaPair).order_by(QaPair.id)).all())
    assert len(pairs) == 3
    assert {p.batch_no for p in pairs} == {data["batchNo"]}
    assert [p.status for p in pairs] == ["enabled", "enabled", "disabled"]  # 缺省 enabled
    assert [p.question for p in pairs] == [row[0] for row in rows]
    assert [p.answer for p in pairs] == [row[1] for row in rows]
    assert all(_vec_dims(p.embedding) == 1024 for p in pairs)  # 问题向量非空
    dept_all = session.scalars(select(Department).where(Department.code == "all")).first()
    assert {p.dept_id for p in pairs} == {dept_all.id}  # deptCode 缺省 → all 全员可见
    admin_id = session.scalars(select(User.id).where(User.username == "qaadmin")).one()
    assert {p.created_by for p in pairs} == {admin_id}

    audit = session.scalar(select(AuditLog).where(AuditLog.action == "qa.import"))
    assert audit is not None
    assert audit.object_type == "qa_pair"
    assert audit.detail == {"total": 3, "batchNo": data["batchNo"]}
    assert audit.actor_user_id == admin_id


def test_导入成功后清空问答精确缓存(qa_client, session):
    """新问答对改变可检索事实：成功响应前整清精确缓存（同 W3 Task 1 文档口径）。"""
    admin_login(qa_client, session)
    from app.main import app

    app.state.chat_deps.cache.put("缓存问题", {"answer": "陈旧答案"})
    assert app.state.chat_deps.cache.get("缓存问题") == {"answer": "陈旧答案"}

    resp = post_import(qa_client, [["问题A", "答案A", "enabled"]])

    assert resp.status_code == 201
    assert app.state.chat_deps.cache.get("缓存问题") is None


# ---------- ② 空答案 + 超长答案：恰 2 条错误行零入库（QA-02） ----------


def test_空答案与超长答案行_恰两条错误行且整批零入库(qa_client, session):
    """② 空答案行 + 超长答案行：422/1003，data 恰 2 条错误行（文件序），零入库。"""
    admin_login(qa_client, session)
    rows = [
        ["合规问题一", "合规答案一", "enabled"],
        ["空答案问题", "", "enabled"],
        ["超长答案问题", "长" * 10001, "enabled"],
    ]
    resp = post_import(qa_client, rows)

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["data"] == [
        {"row": 3, "field": "答案", "reason": "答案不能为空"},
        {"row": 4, "field": "答案", "reason": "答案超过 10000 字上限"},
    ]
    assert _qa_pair_count(session) == 0


# ---------- ③ 5001 行：文件级拒（QA-03） ----------


def test_超过五千行_文件级拒绝且零入库(qa_client, session):
    """③ 5001 行超单批上限：422 文件级错误（row=0），不做部分导入，零入库。"""
    admin_login(qa_client, session)
    rows = [[f"问题{i}", f"答案{i}", "enabled"] for i in range(5001)]

    resp = post_import(qa_client, rows)

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["message"] == "文件超过 5000 行上限"
    assert body["data"] == [{"row": 0, "field": None, "reason": "文件超过 5000 行上限"}]
    assert _qa_pair_count(session) == 0


# ---------- ④ 单答案 10001 字：行级错（QA-03） ----------


def test_单答案一万零一字_行级错误零入库(qa_client, session):
    """④ 单个答案 10001 字：行级错误指名 row=2/field=答案，零入库。"""
    admin_login(qa_client, session)

    resp = post_import(qa_client, [["问题", "答" * 10001, "enabled"]])

    assert resp.status_code == 422
    assert resp.json()["data"] == [{"row": 2, "field": "答案", "reason": "答案超过 10000 字上限"}]
    assert _qa_pair_count(session) == 0


# ---------- ⑤ 空表：报"无数据"（QA-08） ----------


def test_空表导入_报文件无数据(qa_client, session):
    """⑤ 表头之外无数据行：422 中文提示"无数据"，零入库。"""
    admin_login(qa_client, session)

    resp = post_import(qa_client, [])

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert "无数据" in body["message"]
    assert body["data"] == [{"row": 0, "field": None, "reason": "文件无数据"}]
    assert _qa_pair_count(session) == 0


# ---------- ⑥ 模板下载：200 + 可被 read_rows 读回 ----------


def test_模板下载_200附件且可被read_rows读回(qa_client, session):
    """⑥ 模板下载：200 xlsx 字节流 attachment，表头+3 行示例可被 read_rows 读回。"""
    from app.services.xlsx_io import read_rows

    admin_login(qa_client, session)

    resp = qa_client.get("/api/admin/qa-pairs/template")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(XLSX_MIME)
    assert "attachment" in resp.headers["content-disposition"]
    rows = read_rows(resp.content, ["问题", "答案", "状态"], 50, required={"问题", "答案"})
    assert len(rows) == 3
    assert all(row["问题"] and row["答案"] for row in rows)
    assert all(row["状态"] in ("enabled", "disabled") for row in rows)


# ---------- deptCode：指定 service / 非法 422 ----------


def test_导入指定service部门与非法部门码(qa_client, session):
    """deptCode=service → 行落客服部门；非法 code → 422 中文原因且零新增。"""
    admin_login(qa_client, session)

    resp = post_import(qa_client, [["问题A", "答案A", "enabled"]], dept_code="service")
    assert resp.status_code == 201
    dept = session.scalars(select(Department).where(Department.code == "service")).first()
    pair = session.scalar(select(QaPair))
    assert pair.dept_id == dept.id

    bad = post_import(qa_client, [["问题B", "答案B", "enabled"]], dept_code="bogus")
    assert bad.status_code == 422
    assert "部门编码无效" in bad.json()["message"]
    assert _qa_pair_count(session) == 1  # 失败请求零新增


# ---------- 状态枚举：非法值行级错 ----------


def test_状态列非法取值_行级错误(qa_client, session):
    """状态列既非 enabled/disabled 也非缺省空：行级错误指名 row/field，零入库。"""
    admin_login(qa_client, session)

    resp = post_import(qa_client, [["问题", "答案", "启用"]])

    assert resp.status_code == 422
    assert resp.json()["data"] == [
        {"row": 2, "field": "状态", "reason": "状态仅允许 enabled 或 disabled"}
    ]
    assert _qa_pair_count(session) == 0


# ---------- 表头结构错误透传（row=1） ----------


def test_表头缺列与多余列_结构错误透传(qa_client, session):
    """表头缺「答案」「状态」且多「备注」：xlsx_io 结构错误原样进 data（row=1）。"""
    admin_login(qa_client, session)

    resp = qa_client.post(
        "/api/admin/qa-pairs/import",
        files={"file": ("qa.xlsx", _xlsx([["问题", "备注"], ["问题1", "备注1"]]), XLSX_MIME)},
    )

    assert resp.status_code == 422
    # xlsx_io 聚合次序：缺列（按 headers 序）→ 多余列（按文件列序）→ 缺必填列（按字母序）；
    # 「答案」属必填列，缺列与缺必填两笔都报（结构报错不短路）
    assert resp.json()["data"] == [
        {"row": 1, "field": "答案", "reason": "表头缺少列「答案」"},
        {"row": 1, "field": "状态", "reason": "表头缺少列「状态」"},
        {"row": 1, "field": "备注", "reason": "表头多余列「备注」"},
        {"row": 1, "field": "答案", "reason": "表头缺少必填列「答案」"},
    ]
    assert _qa_pair_count(session) == 0


# ---------- embed 分批 ≤256 ----------


def test_导入三百零一行_embed分批256与45(qa_client, session, monkeypatch):
    """向量化分批 ≤256：301 行分两次 embed（256+45），用的是 chat_deps 同一实例。"""
    from app.main import app

    admin_login(qa_client, session)
    embedder = app.state.chat_deps.embedder
    sizes: list[int] = []
    real_embed = embedder.embed

    def recording_embed(texts: list[str]) -> list[list[float]]:
        sizes.append(len(texts))
        return real_embed(texts)

    monkeypatch.setattr(embedder, "embed", recording_embed)

    resp = post_import(qa_client, [[f"问题{i}", f"答案{i}", "enabled"] for i in range(301)])

    assert resp.status_code == 201
    assert resp.json()["data"]["imported"] == 301
    assert sizes == [256, 45]
    assert _qa_pair_count(session) == 301


# ---------- 权限门：member 全端点 403 ----------


def test_成员角色访问导入与模板_403(qa_client, session):
    """member 角色：导入与模板下载均 403/2004（admin-only）。"""
    make_user(session, "qamember", "right-one", role="member")
    assert login(qa_client, "qamember", "right-one").status_code == 200

    resp = post_import(qa_client, [["问题", "答案", "enabled"]])
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
    assert qa_client.get("/api/admin/qa-pairs/template").status_code == 403
