r"""管理端评测题库 Excel 整批导入（F-601）与模板下载 API 测试。

brief 断言 ①-④ 全覆盖：合规 3 行 201 且读回（部门中文名→dept_id 映射、题型落库、
来源空→NULL、题型空缺省 factual、审计 eval.import）；非法部门/题型/超长期望答案
→ 422/1003 行级错误全报告零入库（整批语义）；`[拒答]` 与 cross_reject 双向静默
归一（验收判定协议第 3 条约定格式）；模板 200 attachment 且可被 read_rows 读回。
另固化：错误行号 = Excel 物理行号（中部空行文件）、上传早拒坏字节 422 不落 500、
空表报"无数据"、member 403 门、模板示例行可直接整批导入（模板与校验规则同源）。

评测题不向量化（eval_questions 无 embedding 列，纯文本存库），导入端点不触
embedder 与问答精确缓存——评测跑分走缓存旁路（F-602，Task 6）。
"""

from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import func, select

from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID
from app.db.models import AuditLog, Department, EvalQuestion, User
from app.services.xlsx_io import read_rows
from tests.conftest import login_as

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HEADERS = ["部门", "问题", "期望答案", "来源", "题型"]


# ---------- 构造与登录辅助（口径同 tests/api/test_admin_qa_import.py） ----------


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


def admin_login(client, session) -> None:
    """创建并登录 admin（cookie 由 client 保持），失败即失败在显式断言。"""
    make_user(session, "evaladmin", "right-one", role="admin")
    assert login_as(client, "evaladmin", "right-one").status_code == 200


def _xlsx(rows: list[list[object]]) -> bytes:
    """测试内现造 xlsx 字节流：rows[0] 视为表头行，其余为数据行；空列表即空行。"""
    wb = Workbook()
    ws = wb.worksheets[0]
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def post_import(client, rows: list[list[object]]):
    """multipart 导入请求（评测题部门在文件行内，无 deptCode 表单）。"""
    return client.post(
        "/api/admin/eval-questions/import",
        files={"file": ("eval.xlsx", _xlsx([_HEADERS, *rows]), XLSX_MIME)},
    )


def _eval_count(session) -> int:
    return session.scalar(select(func.count()).select_from(EvalQuestion))


# ---------- ① 合规 3 行：201 + 读回（部门映射/题型/来源）+ 审计 ----------


def test_导入合规三行_201读回部门映射题型与审计(client, session):
    """① 合规 3 行：201 {imported:3}；读回部门中文名→id、题型/来源落库、审计 eval.import。"""
    admin_login(client, session)
    rows = [
        [
            "客服",
            "员工如何重置门户登录密码？",
            "登录页点击「忘记密码」，通过绑定手机号验证后重置。",
            "客服手册 3.2 节",
            "factual",
        ],
        [
            "人力",
            "年假可以跨年结转吗？未休完的年假如何处理？",
            "当年度假额度次年 1 月 1 日清零，不跨年结转。",
            "员工手册 5.1 节",
            "multi_hop",
        ],
        ["全员", "公司通勤补贴标准是多少？", "OA 流程库搜索「通勤补贴」查看标准。", "", ""],
    ]
    resp = post_import(client, rows)

    assert resp.status_code == 201
    assert resp.json() == {"code": 0, "message": "ok", "data": {"imported": 3}}

    questions = list(session.scalars(select(EvalQuestion).order_by(EvalQuestion.id)).all())
    assert len(questions) == 3
    dept_ids = {d.code: d.id for d in session.scalars(select(Department)).all()}
    assert [q.dept_id for q in questions] == [
        dept_ids["service"],
        dept_ids["hr"],
        dept_ids["all"],
    ]
    assert [q.qtype for q in questions] == ["factual", "multi_hop", "factual"]  # 题型空缺省
    assert [q.source_ref for q in questions] == ["客服手册 3.2 节", "员工手册 5.1 节", None]
    assert [q.question for q in questions] == [r[1] for r in rows]
    assert [q.expected_answer for q in questions] == [r[2] for r in rows]
    assert all(q.is_active is True for q in questions)

    audit = session.scalar(select(AuditLog).where(AuditLog.action == "eval.import"))
    assert audit is not None
    assert audit.object_type == "eval_question"
    assert audit.detail == {"total": 3}
    admin_id = session.scalars(select(User.id).where(User.username == "evaladmin")).one()
    assert audit.actor_user_id == admin_id


# ---------- ② 非法部门/题型/超长：行级错误全报告零入库 ----------


def test_非法部门题型与超长答案_行级错误全报告零入库(client, session):
    """② 非法部门/题型/超长期望答案各一行：422/1003 一次报全 3 处（文件序），零入库。"""
    admin_login(client, session)
    rows = [
        ["技术部", "问题一", "答案一", "客服手册 3.2 节", "factual"],
        ["客服", "问题二", "答案二", "", "语义匹配"],
        ["人力", "问题三", "答" * 10001, "", "factual"],
    ]
    resp = post_import(client, rows)

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["message"] == "导入文件存在 3 处错误，已整批退回，未入库"
    assert body["data"] == [
        {"row": 2, "field": "部门", "reason": "部门须为：客服/人力/全员"},
        {"row": 3, "field": "题型", "reason": "题型仅允许 factual/multi_hop/cross_reject"},
        {"row": 4, "field": "期望答案", "reason": "期望答案超过 10000 字上限"},
    ]
    assert _eval_count(session) == 0


# ---------- ③ 拒答类双向归一（判定协议第 3 条约定格式） ----------


def test_拒答期望归一为cross_reject读回(client, session):
    """③a 期望答案=[拒答]：qtype 静默归一为 cross_reject 落库，读回核验。"""
    admin_login(client, session)

    resp = post_import(client, [["客服", "人力部门的薪酬体系是怎样的？", "[拒答]", "", "factual"]])

    assert resp.status_code == 201
    q = session.scalar(select(EvalQuestion))
    assert q.expected_answer == "[拒答]"
    assert q.qtype == "cross_reject"


def test_cross_reject题型期望答案归一为拒答标记读回(client, session):
    """③b qtype=cross_reject 而期望答案为普通文本：期望答案静默归一为 [拒答]。"""
    admin_login(client, session)

    resp = post_import(
        client, [["客服", "其他部门全员大会时间是几点？", "有标准答案文本", "", "cross_reject"]]
    )

    assert resp.status_code == 201
    q = session.scalar(select(EvalQuestion))
    assert q.expected_answer == "[拒答]"
    assert q.qtype == "cross_reject"


# ---------- ④ 模板下载：200 + 可被 read_rows 读回 ----------


def test_模板下载_200附件且可被read_rows读回(client, session):
    """④ 模板下载：200 xlsx attachment，表头 + 3 行示例（三类题型），可被 read_rows 读回。"""
    admin_login(client, session)

    resp = client.get("/api/admin/eval-questions/template")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(XLSX_MIME)
    assert "attachment" in resp.headers["content-disposition"]
    rows = read_rows(resp.content, _HEADERS, 50, required={"部门", "问题", "期望答案"})
    assert len(rows) == 3
    assert [row["部门"] for _, row in rows] == ["客服", "人力", "客服"]
    assert [row["题型"] for _, row in rows] == ["factual", "multi_hop", "cross_reject"]
    assert [row["问题"] for _, row in rows] == [
        "员工如何重置门户登录密码？",
        "年假可以跨年结转吗？未休完的年假如何处理？",
        "人力部门的薪酬体系是怎样的？",
    ]
    assert rows[0][1]["来源"] == "客服手册 3.2 节"  # 事实型示例给出期望出处
    assert rows[2][1]["期望答案"] == "[拒答]"  # 串库拒答示例按约定格式


def test_模板示例行可直接整批导入成功(client, session):
    """模板示例行即合法数据：原样上传导入 201（模板与校验规则同源不漂移）。"""
    admin_login(client, session)
    template = client.get("/api/admin/eval-questions/template")

    resp = client.post(
        "/api/admin/eval-questions/import",
        files={"file": ("tpl.xlsx", template.content, XLSX_MIME)},
    )

    assert resp.status_code == 201
    assert resp.json()["data"]["imported"] == 3
    assert _eval_count(session) == 3


# ---------- 修复轮先例固化：错误行号 = Excel 物理行号 ----------


def test_中部空行_错误行号为Excel物理行号(client, session):
    """文件中部有空行时错误行号必须忠实物理行：坏部门行在物理第 4 行就报 row=4。"""
    admin_login(client, session)

    resp = post_import(
        client,
        [
            ["客服", "问题一", "答案一", "", "factual"],
            [],
            ["外包部", "问题三", "答案三", "", "factual"],
        ],
    )

    assert resp.status_code == 422
    assert resp.json()["data"] == [
        {"row": 4, "field": "部门", "reason": "部门须为：客服/人力/全员"}
    ]
    assert _eval_count(session) == 0


# ---------- 上传早拒：坏字节 422 而非 500 ----------


def test_坏字节流导入_422而非500兜底(client, session):
    """非 zip 字节流（损坏/伪装 xlsx）：422/1003 中文原因，不漏 500 系统错误。"""
    admin_login(client, session)

    resp = client.post(
        "/api/admin/eval-questions/import",
        files={"file": ("eval.xlsx", b"this is not a zip archive", XLSX_MIME)},
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["message"] == "文件格式不支持或已损坏"
    assert _eval_count(session) == 0


# ---------- 空表：报"无数据" ----------


def test_空表导入_报文件无数据(client, session):
    """表头之外无数据行：422 文件级错误（row=0）"文件无数据"，零入库。"""
    admin_login(client, session)

    resp = post_import(client, [])

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["message"] == "文件无数据"
    assert body["data"] == [{"row": 0, "field": None, "reason": "文件无数据"}]
    assert _eval_count(session) == 0


# ---------- 权限门：member 403 ----------


def test_成员角色访问导入与模板_403(client, session):
    """member 角色：导入与模板下载均 403/2004（admin-only）。"""
    make_user(session, "evalmember", "right-one", role="member")
    assert login_as(client, "evalmember", "right-one").status_code == 200

    resp = post_import(client, [["客服", "问题", "答案", "", "factual"]])
    assert resp.status_code == 403
    assert resp.json()["code"] == ERR_FORBIDDEN
    assert client.get("/api/admin/eval-questions/template").status_code == 403
