r"""问答对业务：Excel 整批导入（F-401）、模板数据（F-403）与管理面（F-402/QA-06/07）。

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

管理面（F-402/QA-05/06/07）：列表软删行不出现、关键字 ILIKE 先转义 %/_/\（§22）；
停用审计 qa.toggle {from,to}；编辑同导入规则校验后单条重嵌问题、审计 qa.update
{before,after 摘要}；删除置 is_active=false 软删（行保留可追溯）、审计 qa.delete。
commit 归本模块，缓存失效归 API 层成功响应前接线（同 documents retry/confirm 口径）。
"""

import string
from secrets import choice

from sqlalchemy import func, or_, select
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
_SUMMARY_MAX: int = 50  # 审计 detail 前后值摘要截断宽度（答案上限 10000 字，防审计行膨胀）
_MSG_QA_NOT_FOUND = "问答对不存在"
_ESCAPES = (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_"))  # §22：ILIKE 通配符按字面匹配


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


def list_qa_pairs(
    db: Session,
    *,
    dept_id: int | None = None,
    status: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """管理列表（F-402）：软删行不出现（QA-07），过滤可选，created_at 降序（同刻 id 兜底）。

    keyword 对问题/答案 ILIKE 且通配符先转义（§22）；status 枚举外 422。join
    departments 取库内编码（deptId 之外的稳定部门标识，前端部门列渲染不再依赖
    种子序 id 推导）。返回 {items, total, page, pageSize}，items 为 camelCase DTO
    （standards §3.4）。
    """
    filters = [QaPair.is_active.is_(True)]
    if dept_id is not None:
        filters.append(QaPair.dept_id == dept_id)
    if status is not None:
        if status not in _STATUSES:
            raise ApiError(ERR_PARAM_INVALID, f"状态取值无效：{status}", 422)
        filters.append(QaPair.status == status)
    if keyword:
        like = f"%{_escape_like(keyword)}%"
        filters.append(
            or_(QaPair.question.ilike(like, escape="\\"), QaPair.answer.ilike(like, escape="\\"))
        )
    stmt = select(QaPair, Department.code).join(Department, Department.id == QaPair.dept_id)
    stmt = stmt.where(*filters)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(QaPair.created_at.desc(), QaPair.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_item_payload(pair, dept_code) for pair, dept_code in rows],
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


def toggle_qa_pair(
    db: Session, qa_id: int, new_status: str, actor_user_id: int, *, clock: Clock | None = None
) -> QaPair:
    """停用/启用（QA-05）：status 枚举外 422；审计 qa.toggle {from,to} 后提交。"""
    if new_status not in _STATUSES:
        raise ApiError(ERR_PARAM_INVALID, f"状态取值无效：{new_status}", 422)
    pair = _get_active(db, qa_id)
    old_status = pair.status
    pair.status = new_status
    detail = {"from": old_status, "to": new_status}
    record_audit(db, actor_user_id, "qa.toggle", "qa_pair", pair.id, detail, clock=clock)
    db.commit()
    return pair


def update_qa_pair(
    db: Session,
    embedder: Embedder,
    qa_id: int,
    question: str,
    answer: str,
    actor_user_id: int,
    *,
    clock: Clock | None = None,
) -> QaPair:
    """编辑（QA-06）：同导入规则校验 → 单条重嵌问题（答案不向量化，与导入同口径）→
    审计 qa.update {before,after 摘要（截断防膨胀）} → 提交；缓存失效归 API 层。"""
    _validate_edit(question, answer)
    pair = _get_active(db, qa_id)
    before = {"question": _summary(pair.question), "answer": _summary(pair.answer)}
    pair.question = question
    pair.answer = answer
    pair.embedding = embedder.embed([question])[0]
    after = {"question": _summary(question), "answer": _summary(answer)}
    detail = {"before": before, "after": after}
    record_audit(db, actor_user_id, "qa.update", "qa_pair", pair.id, detail, clock=clock)
    db.commit()
    return pair


def delete_qa_pair(
    db: Session, qa_id: int, actor_user_id: int, *, clock: Clock | None = None
) -> QaPair:
    """软删（QA-07）：is_active=false 退出检索与列表，行保留可追溯；审计 qa.delete。"""
    pair = _get_active(db, qa_id)
    pair.is_active = False
    detail = {"question": _summary(pair.question)}
    record_audit(db, actor_user_id, "qa.delete", "qa_pair", pair.id, detail, clock=clock)
    db.commit()
    return pair


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


def _escape_like(keyword: str) -> str:
    """§22：`%`/`_`/`\\` 逐一转义，用户关键字按字面匹配不展开为 ILIKE 通配。"""
    for raw, safe in _ESCAPES:
        keyword = keyword.replace(raw, safe)
    return keyword


def _item_payload(pair: QaPair, dept_code: str) -> dict:
    """列表/读回 DTO（camelCase，standards §3.4）：数据库行不直接返回；
    deptCode 由 join departments 取库内编码（同 users_admin 列表口径）。"""
    return {
        "id": pair.id,
        "deptId": pair.dept_id,
        "deptCode": dept_code,
        "question": pair.question,
        "answer": pair.answer,
        "status": pair.status,
        "batchNo": pair.batch_no,
        "createdAt": pair.created_at.isoformat(),
        "isActive": pair.is_active,
    }


def _get_active(db: Session, qa_id: int) -> QaPair:
    """按 id 取未软删问答对：不存在或已软删一律 404 同文案（已删行不可再管理）。"""
    pair = db.scalars(select(QaPair).where(QaPair.id == qa_id, QaPair.is_active.is_(True))).first()
    if pair is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_QA_NOT_FOUND, 404)
    return pair


def _validate_edit(question: str, answer: str) -> None:
    """编辑校验（与导入行级规则同源同文案）：问题 1-2000、答案 1-10000 非空。"""
    if not question:
        raise ApiError(ERR_PARAM_INVALID, "问题不能为空", 422)
    if len(question) > _QUESTION_MAX:
        raise ApiError(ERR_PARAM_INVALID, f"问题超过 {_QUESTION_MAX} 字上限", 422)
    if not answer:
        raise ApiError(ERR_PARAM_INVALID, "答案不能为空", 422)
    if len(answer) > _ANSWER_MAX:
        raise ApiError(ERR_PARAM_INVALID, f"答案超过 {_ANSWER_MAX} 字上限", 422)


def _summary(text: str) -> str:
    """审计 detail 前后值摘要：超宽截断（审计快照够定位即可，不复制全文）。"""
    return text if len(text) <= _SUMMARY_MAX else text[:_SUMMARY_MAX]
