r"""问答对业务：Excel 整批导入（F-401）与模板数据（F-403）。

整批语义（QA-02/03/08）：xlsx_io.read_rows 做结构级校验后，行级业务校验在本模块
聚合（问题 1-2000、答案 1-10000、状态枚举，状态空缺省 enabled）——任何一行不
合格即 XlsxBatchError 全量报错、零入库；全部合格才单事务入库：embedding 只向
量化问题列（qa_pairs.embedding 是问题向量，答案不向量化），分批 ≤256 调与检索
同一 embedder 实例（同指纹空间），batch_no=QA-YYYYMMDD-4位随机大写，审计
qa.import 与业务写入同事务提交（record_audit 只 flush，commit 归本函数）。

部门裁决：xlsx 模板不含部门列，导入部门由请求表单 deptCode 指定
（service|hr|all，缺省 all=全员可见），从 departments 表解析，枚举外 422——
保证 F-204 检索的 dept_id IN (用户部门, all) 过滤语义可用。

行级错误复用 XlsxBatchError 作载体（{row, field, reason} 同形），API 层单一
catch 出口转 422/1003；行号取 read_rows 携带的 Excel 物理行号（空行跳过后仍
连续，与用户屏幕所见一致，评审 Important-1）。
"""

import string
from secrets import choice

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.core.llm import Embedder
from app.db.models import Department, QaPair
from app.services.audit import record_audit
from app.services.xlsx_io import XlsxBatchError, read_rows, write_template

TEMPLATE_HEADERS: list[str] = ["问题", "答案", "状态"]
TEMPLATE_SAMPLES: list[list[str]] = [
    ["员工如何重置门户登录密码？", "登录页点击「忘记密码」，通过绑定手机号验证后重置。", "enabled"],
    ["年假可以跨年结转吗？", "当年度假额度次年 1 月 1 日清零，请提前安排休假。", "enabled"],
    ["试用期员工是否缴纳社保？", "试用期员工自入职当月起依法缴纳社会保险。", "disabled"],
]

_MAX_IMPORT_ROWS: int = 5000  # QA-03 单批行数上限（文件级）
_EMBED_BATCH: int = 256  # 单次 embed 调用文本数上限
_QUESTION_MAX: int = 2000  # 与 qa_pairs_question_check 同源
_ANSWER_MAX: int = 10000  # 与 qa_pairs_answer_check 同源
_STATUSES: frozenset[str] = frozenset({"enabled", "disabled"})


def build_template() -> bytes:
    """F-403 模板字节流：表头 + 3 行示例（QA-模板交付 §8.10）。"""
    return write_template(headers=TEMPLATE_HEADERS, sample_rows=TEMPLATE_SAMPLES)


def import_qa_pairs(
    db: Session,
    embedder: Embedder,
    actor_user_id: int,
    content: bytes,
    dept_code: str = "all",
    *,
    clock: Clock | None = None,
) -> dict:
    """整批导入：结构校验 → 行级校验（任一错误零入库）→ 单事务入库 + 审计。

    返回 {"imported": 行数, "batchNo": 批次号}；行级/结构级错误抛 XlsxBatchError，
    部门解析失败抛 ApiError，由 API 层分别转 422。
    """
    rows = read_rows(content, TEMPLATE_HEADERS, _MAX_IMPORT_ROWS, required={"问题", "答案"})
    errors = _row_errors(rows)
    if errors:
        raise XlsxBatchError(errors)
    dept_id = _resolve_dept(db, dept_code)
    effective_clock = clock if clock is not None else get_clock()
    batch_no = _make_batch_no(effective_clock)
    vectors = _embed_questions(embedder, rows)
    for (_, row), vector in zip(rows, vectors, strict=True):
        db.add(
            QaPair(
                dept_id=dept_id,
                question=row["问题"],
                answer=row["答案"],
                embedding=vector,
                status=row["状态"] or "enabled",  # 状态列空缺省 enabled
                batch_no=batch_no,
                created_by=actor_user_id,
            )
        )
    record_audit(
        db,
        actor_user_id,
        "qa.import",
        "qa_pair",
        None,
        {"total": len(rows), "batchNo": batch_no},
        clock=effective_clock,
    )
    db.commit()
    return {"imported": len(rows), "batchNo": batch_no}


def _row_errors(rows: list[tuple[int, dict[str, str]]]) -> list[dict]:
    """行级业务校验聚合：问题/答案非空与长度上限、状态枚举；一次报全不短路。

    行号用 read_rows 携带的 Excel 物理行号（跳过空行后仍连续）。
    """
    errors: list[dict] = []
    for row_no, row in rows:
        if not row["问题"]:
            errors.append({"row": row_no, "field": "问题", "reason": "问题不能为空"})
        elif len(row["问题"]) > _QUESTION_MAX:
            errors.append(
                {"row": row_no, "field": "问题", "reason": f"问题超过 {_QUESTION_MAX} 字上限"}
            )
        if not row["答案"]:
            errors.append({"row": row_no, "field": "答案", "reason": "答案不能为空"})
        elif len(row["答案"]) > _ANSWER_MAX:
            errors.append(
                {"row": row_no, "field": "答案", "reason": f"答案超过 {_ANSWER_MAX} 字上限"}
            )
        if row["状态"] and row["状态"] not in _STATUSES:
            errors.append(
                {"row": row_no, "field": "状态", "reason": "状态仅允许 enabled 或 disabled"}
            )
    return errors


def _resolve_dept(db: Session, code: str) -> int:
    """deptCode → departments.id：枚举外（或未播种）422，与文档上传同文案口径。"""
    dept = db.scalars(select(Department).where(Department.code == code)).first()
    if dept is None:
        raise ApiError(ERR_PARAM_INVALID, f"部门编码无效：{code}（可选 service/hr/all）", 422)
    return dept.id


def _embed_questions(
    embedder: Embedder, rows: list[tuple[int, dict[str, str]]]
) -> list[list[float]]:
    """问题列向量：分批 ≤256 调 embedder，返回顺序与 rows 对齐（答案不向量化）。"""
    questions = [row["问题"] for _, row in rows]
    vectors: list[list[float]] = []
    for start in range(0, len(questions), _EMBED_BATCH):
        vectors.extend(embedder.embed(questions[start : start + _EMBED_BATCH]))
    return vectors


def _make_batch_no(clock: Clock) -> str:
    """QA-YYYYMMDD-4位随机大写：日期取注入时钟（与审计同刻度），随机段降低碰撞概率。"""
    suffix = "".join(choice(string.ascii_uppercase) for _ in range(4))
    return f"QA-{clock.now().strftime('%Y%m%d')}-{suffix}"
