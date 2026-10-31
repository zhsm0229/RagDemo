r"""管理端评测题库 Excel 整批导入（F-601）与模板下载 API 测试。

brief 断言 ①-④ 全覆盖：合规 3 行 201 且读回（部门中文名→dept_id 映射、题型落库、
来源匹配落 source_doc_id、题型空缺省 factual、审计 eval.import）；非法部门/题型/超长
期望答案 → 422/1003 行级错误全报告零入库（整批语义）；`[拒答]` 与 cross_reject 双向
静默归一（验收判定协议第 3 条约定格式）；模板 200 attachment 且可被 read_rows 读回。
另固化：错误行号 = Excel 物理行号（中部空行文件）、上传早拒坏字节 422 不落 500、
空表报"无数据"、member 403 门、模板示例行可直接整批导入（模板与校验规则同源）。

metrics-balance 0011（spec §4.1 冻结口径）：非拒答题来源必填且须匹配同部门唯一文档
（去扩展名归一化 / 唯一后缀段 / 跨部门同名不误报），命中落 source_doc_id（读回断言）；
拒答类豁免校验落 NULL；存量旧题（source_doc_id 空）不阻塞新导入，仅新导入强制。

评测题不向量化（eval_questions 无 embedding 列，纯文本存库），导入端点不触
embedder 与问答精确缓存——评测跑分走缓存旁路（F-602，Task 6）。
"""

import hashlib
from datetime import date
from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import func, select

from app.core.errors import ERR_FORBIDDEN, ERR_PARAM_INVALID
from app.db.models import AuditLog, Department, Document, EvalQuestion, User
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


def _dept_id(session, code: str) -> int:
    return session.scalars(select(Department.id).where(Department.code == code)).one()


def _admin_id(session) -> int:
    return session.scalars(select(User.id).where(User.username == "admin")).one()


def _add_doc(session, dept_code: str, title: str, filename: str | None = None) -> Document:
    """就绪且活跃的种子文档（来源匹配候选；filename 缺省 = title.docx，hash 按部门+标题避开查重唯一索引）。"""
    doc = Document(
        title=title,
        dept_id=_dept_id(session, dept_code),
        original_filename=filename or f"{title}.docx",
        file_path=f"seed/{title}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=16,
        content_hash=hashlib.sha256(f"{dept_code}:{title}".encode()).hexdigest(),
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        uploaded_by=_admin_id(session),
    )
    session.add(doc)
    session.flush()
    return doc


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


# ---------- ① 合规 3 行：201 + 读回（部门映射/题型/来源匹配落 source_doc_id）+ 审计 ----------


def test_导入合规三行_201读回部门映射题型来源落列与审计(client, session):
    """① 合规 3 行：201 {imported:3}；读回部门中文名→id、题型/来源落库、来源命中
    文档落 source_doc_id（标题精确/文件名去扩展名两路）、审计 eval.import。"""
    admin_login(client, session)
    doc_service = _add_doc(session, "service", "客服_门户使用手册_v1")
    doc_hr = _add_doc(session, "hr", "人力_员工手册_v2", filename="员工手册.docx")
    doc_all = _add_doc(session, "all", "OA流程库")
    rows = [
        [
            "客服",
            "员工如何重置门户登录密码？",
            "登录页点击「忘记密码」，通过绑定手机号验证后重置。",
            "客服_门户使用手册_v1",
            "factual",
        ],
        [
            "人力",
            "年假可以跨年结转吗？未休完的年假如何处理？",
            "当年度假额度次年 1 月 1 日清零，不跨年结转。",
            "员工手册.docx",
            "multi_hop",
        ],
        ["全员", "公司通勤补贴标准是多少？", "OA 流程库搜索「通勤补贴」查看标准。", "OA流程库", ""],
    ]
    resp = post_import(client, rows)

    assert resp.status_code == 201
    assert resp.json() == {"code": 0, "message": "ok", "data": {"imported": 3, "duplicates": 0}}

    questions = list(session.scalars(select(EvalQuestion).order_by(EvalQuestion.id)).all())
    assert len(questions) == 3
    dept_ids = {d.code: d.id for d in session.scalars(select(Department)).all()}
    assert [q.dept_id for q in questions] == [
        dept_ids["service"],
        dept_ids["hr"],
        dept_ids["all"],
    ]
    assert [q.qtype for q in questions] == ["factual", "multi_hop", "factual"]  # 题型空缺省
    assert [q.source_ref for q in questions] == [
        "客服_门户使用手册_v1",
        "员工手册.docx",
        "OA流程库",
    ]
    assert [q.source_doc_id for q in questions] == [doc_service.id, doc_hr.id, doc_all.id]
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


# ---------- ③c 来源校验（metrics-balance 0011，spec §4.1 冻结口径） ----------


def test_非拒答题来源为空_行级报错零入库(client, session):
    """③c-1 非拒答题（factual）来源空：422 报"非拒答题来源必填"（含行号），同文件
    拒答行豁免不报错；整批零入库。"""
    admin_login(client, session)

    resp = post_import(
        client,
        [
            ["客服", "问题一", "答案一", "", "factual"],
            ["客服", "人力部门的薪酬体系是怎样的？", "[拒答]", "", ""],
        ],
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["data"] == [{"row": 2, "field": "来源", "reason": "非拒答题来源必填"}]
    assert _eval_count(session) == 0


def test_来源未匹配本部门文档_整批退回含行号零入库(client, session):
    """③c-2 来源在别的部门有同名文档、本部门没有：按同部门口径报"未匹配"（含行号
    与原值），零入库。"""
    admin_login(client, session)
    _add_doc(session, "hr", "员工手册")

    resp = post_import(client, [["客服", "问题一", "答案一", "员工手册", "factual"]])

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["data"] == [
        {"row": 2, "field": "来源", "reason": "来源「员工手册」在本部门未匹配到文档"}
    ]
    assert _eval_count(session) == 0


def test_来源命中多篇文档_整批退回零入库(client, session):
    """③c-3 精确与后缀段各命中一篇（共 2 篇）：报"匹配到多篇"（含行号），零入库。"""
    admin_login(client, session)
    _add_doc(session, "service", "客服手册")
    _add_doc(session, "service", "客服_客服手册", filename="旧版客服手册.docx")

    resp = post_import(client, [["客服", "问题一", "答案一", "客服手册", "factual"]])

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == ERR_PARAM_INVALID
    assert body["data"] == [
        {"row": 2, "field": "来源", "reason": "来源「客服手册」匹配到多篇文档，请填写完整标题"}
    ]
    assert _eval_count(session) == 0


def test_来源去扩展名归一化命中_落列读回(client, session):
    """③c-4 来源填原始文件名「客服手册.pdf」（小写扩展名）：去扩展名后精确命中标题，
    source_doc_id 落列读回；source_ref 保留用户原值不篡改。"""
    admin_login(client, session)
    doc = _add_doc(session, "service", "客服手册", filename="客服手册.PDF")

    resp = post_import(client, [["客服", "问题一", "答案一", "客服手册.pdf", "factual"]])

    assert resp.status_code == 201
    q = session.scalar(select(EvalQuestion))
    assert q.source_ref == "客服手册.pdf"
    assert q.source_doc_id == doc.id


def test_唯一后缀段命中_落列读回(client, session):
    """③c-5 来源填去部门前缀的主题段：以分隔符为界的唯一后缀段命中，source_doc_id
    落列读回。"""
    admin_login(client, session)
    doc = _add_doc(session, "service", "客服_差旅报销制度_v2")

    resp = post_import(client, [["客服", "问题一", "答案一", "差旅报销制度_v2", "factual"]])

    assert resp.status_code == 201
    q = session.scalar(select(EvalQuestion))
    assert q.source_doc_id == doc.id


def test_后缀段必须带分隔符边界_裸子串不命中(client, session):
    """③c-6 来源是标题的无边界子串（「手册」⊂「客服手册」）：不算后缀段，报未匹配
    （防子串误命中），零入库。"""
    admin_login(client, session)
    _add_doc(session, "service", "客服手册")

    resp = post_import(client, [["客服", "问题一", "答案一", "手册", "factual"]])

    assert resp.status_code == 422
    assert resp.json()["data"] == [
        {"row": 2, "field": "来源", "reason": "来源「手册」在本部门未匹配到文档"}
    ]
    assert _eval_count(session) == 0


def test_跨部门同名文档_只匹配本部门唯一命中(client, session):
    """③c-7 两部门各有同名「员工手册」：人力行的来源只命中人力那篇（同部门口径不
    误报多篇），source_doc_id 落人力文档 id。"""
    admin_login(client, session)
    doc_service = _add_doc(session, "service", "员工手册")
    doc_hr = _add_doc(session, "hr", "员工手册")

    resp = post_import(client, [["人力", "问题一", "答案一", "员工手册", "factual"]])

    assert resp.status_code == 201
    q = session.scalar(select(EvalQuestion))
    assert q.source_doc_id == doc_hr.id
    assert doc_service.id != doc_hr.id  # 同名确实跨部门存在，命中并非巧合


def test_拒答类豁免来源校验_source_doc_id落NULL(client, session):
    """③c-8 expected=[拒答] 与 qtype=cross_reject 两行均豁免（后者来源有值但不校验
    不报错），source_doc_id 一律落 NULL 读回。"""
    admin_login(client, session)

    resp = post_import(
        client,
        [
            ["客服", "人力部门的薪酬体系是怎样的？", "[拒答]", "", ""],
            ["客服", "其他部门全员大会时间是几点？", "普通文本", "客服手册", "cross_reject"],
        ],
    )

    assert resp.status_code == 201
    questions = list(session.scalars(select(EvalQuestion).order_by(EvalQuestion.id)).all())
    assert len(questions) == 2
    assert all(q.source_doc_id is None for q in questions)
    assert all(q.qtype == "cross_reject" for q in questions)
    assert [q.expected_answer for q in questions] == ["[拒答]", "[拒答]"]
    assert [q.source_ref for q in questions] == [None, "客服手册"]  # 原值保留，仅引用不落


def test_存量旧题空source_doc_id_不阻塞新导入且不被回溯校验(client, session):
    """③c-9 兼容口径：存量旧题（source_ref 有值、source_doc_id 空、指向已不存在的
    文档）不影响新批次导入——校验只作用于本批行，不回溯存量；旧题原样保留。"""
    admin_login(client, session)
    session.add(
        EvalQuestion(
            dept_id=_dept_id(session, "service"),
            question="存量旧题：门户密码怎么改？",
            expected_answer="旧答案文本",
            source_ref="已下架的旧文档",
            qtype="factual",
        )
    )
    session.flush()
    doc = _add_doc(session, "service", "客服_门户使用手册_v1")

    resp = post_import(
        client,
        [["客服", "新题：门户密码怎么改？", "新答案文本", "客服_门户使用手册_v1", "factual"]],
    )

    assert resp.status_code == 201
    questions = list(session.scalars(select(EvalQuestion).order_by(EvalQuestion.id)).all())
    assert len(questions) == 2
    legacy, fresh = questions
    assert legacy.source_ref == "已下架的旧文档"
    assert legacy.source_doc_id is None  # 存量不回填，评测侧记 null 不计入（spec §2）
    assert fresh.source_doc_id == doc.id


# ---------- ④ 模板下载：200 + 可被 read_rows 读回 ----------


def test_模板下载_200附件且可被read_rows读回(client, session):
    """④ 模板下载：200 xlsx attachment，表头 + 3 行示例（三类题型），可被 read_rows
    读回；来源示例即合法引用形态（标题精确/文件名去扩展名/拒答留空）。"""
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
    assert rows[0][1]["来源"] == "客服_门户使用手册_v1"  # 事实型示例：完整文档标题
    assert rows[1][1]["来源"] == "员工手册.docx"  # 多跳示例：原始文件名（导入时去扩展名匹配）
    assert rows[2][1]["来源"] == ""  # 串库拒答示例无期望出处
    assert rows[2][1]["期望答案"] == "[拒答]"  # 串库拒答示例按约定格式


def test_模板示例行可直接整批导入成功(client, session):
    """模板示例行即合法数据：预置同名候选文档后原样上传导入 201（模板与校验规则同源
    不漂移）。"""
    admin_login(client, session)
    _add_doc(session, "service", "客服_门户使用手册_v1")
    _add_doc(session, "hr", "人力_员工手册_v2", filename="员工手册.docx")
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


def test_重复导入同题_幂等跳过重复行(client, session):
    """库内查重（2026-10-31 工单 17 附带修复）：同部门同题面（归一后）已存在时跳过
    该行不计入 imported、响应 data.duplicates 计数——运营重放同批文件不再堆积重复题
    （对抗用例脚本曾放大此缺陷：同题入库 4 份）。"""
    admin_login(client, session)
    rows = [
        ["客服", "重复导入验证题", "[拒答]", "", "cross_reject"],
    ]
    first = post_import(client, rows)
    assert first.status_code == 201
    assert first.json()["data"]["imported"] == 1

    again = post_import(client, rows)
    assert again.status_code == 201
    assert again.json()["data"]["imported"] == 0
    assert again.json()["data"]["duplicates"] == 1

    total = session.scalar(
        select(func.count())
        .select_from(EvalQuestion)
        .where(EvalQuestion.question == "重复导入验证题")
    )
    assert total == 1  # 库内仅一份
