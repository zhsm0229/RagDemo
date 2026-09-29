r"""评测题库业务：Excel 整批导入（F-601）与模板数据。

分区预留（Task 6/7）：本文件当前只含"导入 + 模板"分区；跑分引擎（F-602）与
人工评判相关实现将在本模块依次追加新分区（单文件 ≤300 行约束下按区组织，导入
区保持文件头部不动）。

整批语义与问答对导入（services/qa_pairs.py）同构：xlsx_io.read_rows 结构级校验后，
行级业务校验在本模块聚合（部门枚举、问题 1-2000、期望答案非空 ≤10000、题型枚举，
题型空缺省 factual 与 DB server_default 同源）——任何一行不合格即 XlsxBatchError
全量报错、零入库；全部合格才单事务入库 + 审计（record_audit 只 flush，commit 归
本函数）。评测题纯文本存库（eval_questions 无 embedding 列），导入不触 embedder；
表无批次号列，成功只回 {imported}。

部门裁决（F-401 傻瓜式原则同源）：模板部门列收中文枚举名（客服/人力/全员），服务
层映射 code（service/hr/all）后查 departments 解析 id——对接人填 Excel 用中文名，
非法值行级错误"部门须为：客服/人力/全员"。

拒答类双向归一（features F-601 / 验收判定协议第 3 条，静默处理不留审计痕）：
1. 期望答案为 `[拒答]` → qtype 归一为 cross_reject（哪怕原填 factual）；
2. qtype=cross_reject 而期望答案非 `[拒答]` → 期望答案归一为 `[拒答]`。
跑分引擎（Task 6）据 `expected_answer == '[拒答]'` 单一条件识别拒答题自动判。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.db.models import Department, EvalQuestion
from app.services.audit import record_audit
from app.services.xlsx_io import XlsxBatchError, read_rows, write_template

TEMPLATE_HEADERS: list[str] = ["部门", "问题", "期望答案", "来源", "题型"]
REJECT_MARK: str = "[拒答]"  # 串库拒答题期望答案的约定格式（判定协议第 3 条）
TEMPLATE_SAMPLES: list[list[str]] = [
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
    [
        "客服",
        "人力部门的薪酬体系是怎样的？",
        REJECT_MARK,
        "",  # 串库拒答题期望行为是拒答，无期望出处
        "cross_reject",
    ],
]

# xlsx 部门中文枚举名 → departments.code（种子：客服/人力/全员 ↔ service/hr/all）
_DEPT_NAME_TO_CODE: dict[str, str] = {"客服": "service", "人力": "hr", "全员": "all"}
_QTYPES: frozenset[str] = frozenset({"factual", "multi_hop", "cross_reject"})
_MAX_IMPORT_ROWS: int = 5000  # 单批行数上限（与问答对导入 QA-03 同口径）
_QUESTION_MAX: int = 2000
_ANSWER_MAX: int = 10000


def build_template() -> bytes:
    """F-601 模板字节流：表头 + 3 行示例（事实型/多跳/串库拒答，部门列用中文名）。"""
    return write_template(headers=TEMPLATE_HEADERS, sample_rows=TEMPLATE_SAMPLES)


def import_eval_questions(
    db: Session,
    actor_user_id: int,
    content: bytes,
    *,
    clock: Clock | None = None,
) -> dict:
    """整批导入：结构校验 → 行级校验（任一错误零入库）→ 拒答归一 → 单事务入库 + 审计。

    返回 {"imported": 行数}；行级/结构级错误抛 XlsxBatchError，部门未播种抛
    ApiError，由 API 层分别转 422。评测题不向量化（纯文本存库）。
    """
    rows = read_rows(
        content, TEMPLATE_HEADERS, _MAX_IMPORT_ROWS, required={"部门", "问题", "期望答案"}
    )
    errors = _row_errors(rows)
    if errors:
        raise XlsxBatchError(errors)
    dept_ids = _resolve_depts(db, {row["部门"] for _, row in rows})
    effective_clock = clock if clock is not None else get_clock()
    for _, row in rows:
        qtype, expected = _normalize_reject(row["题型"], row["期望答案"])
        db.add(
            EvalQuestion(
                dept_id=dept_ids[row["部门"]],
                question=row["问题"],
                expected_answer=expected,
                source_ref=row["来源"] or None,  # 来源列可空：空串落 NULL 而非空文本
                qtype=qtype,
            )
        )
    record_audit(
        db,
        actor_user_id,
        "eval.import",
        "eval_question",
        None,
        {"total": len(rows)},
        clock=effective_clock,
    )
    db.commit()
    return {"imported": len(rows)}


def _row_errors(rows: list[tuple[int, dict[str, str]]]) -> list[dict]:
    """行级业务校验聚合：部门/题型枚举、问题与期望答案非空与长度上限；一次报全不短路。

    行号用 read_rows 携带的 Excel 物理行号（表头=1，数据从 2 起，空行跳过仍连续）。
    """
    errors: list[dict] = []
    for row_no, row in rows:
        if not row["部门"]:
            errors.append({"row": row_no, "field": "部门", "reason": "部门不能为空"})
        elif row["部门"] not in _DEPT_NAME_TO_CODE:
            errors.append({"row": row_no, "field": "部门", "reason": "部门须为：客服/人力/全员"})
        if not row["问题"]:
            errors.append({"row": row_no, "field": "问题", "reason": "问题不能为空"})
        elif len(row["问题"]) > _QUESTION_MAX:
            errors.append(
                {"row": row_no, "field": "问题", "reason": f"问题超过 {_QUESTION_MAX} 字上限"}
            )
        if not row["期望答案"]:
            errors.append({"row": row_no, "field": "期望答案", "reason": "期望答案不能为空"})
        elif len(row["期望答案"]) > _ANSWER_MAX:
            errors.append(
                {
                    "row": row_no,
                    "field": "期望答案",
                    "reason": f"期望答案超过 {_ANSWER_MAX} 字上限",
                }
            )
        if row["题型"] and row["题型"] not in _QTYPES:
            errors.append(
                {
                    "row": row_no,
                    "field": "题型",
                    "reason": "题型仅允许 factual/multi_hop/cross_reject",
                }
            )
    return errors


def _normalize_reject(qtype: str, expected: str) -> tuple[str, str]:
    """拒答类双向归一（判定协议第 3 条，静默处理）：返回落库用的 (qtype, expected)。

    1. 期望答案为 `[拒答]` → qtype 归一为 cross_reject；
    2. qtype=cross_reject 而期望答案非 `[拒答]` → 期望答案归一为 `[拒答]`。
    归一后跑分引擎据 `expected_answer == '[拒答]'` 单一条件识别拒答题；题型空
    在此缺省 factual（与 DB server_default 同源）。
    """
    if expected == REJECT_MARK:
        return "cross_reject", expected
    if qtype == "cross_reject":
        return qtype, REJECT_MARK
    return qtype or "factual", expected


def _resolve_depts(db: Session, dept_names: set[str]) -> dict[str, int]:
    """部门中文名 → departments.id 映射：行级枚举已校验，只查用到的 code；缺播种 422。"""
    codes = {_DEPT_NAME_TO_CODE[name] for name in dept_names}
    by_code = {
        dept.code: dept.id
        for dept in db.scalars(select(Department).where(Department.code.in_(codes))).all()
    }
    missing = sorted(codes - by_code.keys())
    if missing:
        raise ApiError(ERR_PARAM_INVALID, f"部门数据未初始化：{'、'.join(missing)}", 422)
    return {name: by_code[_DEPT_NAME_TO_CODE[name]] for name in dept_names}
