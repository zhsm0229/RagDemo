r"""管理端评测路由（薄控制器）：题库 Excel 导入（F-601）+ 模板下载 + 跑分（F-602）
+ 人工评判录分（F-602/F-603 评判提交）+ 分组报告（F-603）。

业务全部委托 services 层：导入走 eval_service（XlsxBatchError 统一转 422/1003，错误
行列表直接放 data（standards §21.9），行号是 xlsx_io 携带的 Excel 物理行号；文件级
单一错误（row=0）把 reason 提为 message）；跑分走 eval_engine（创建即 commit running
行 + 审计，BackgroundTasks 执行 run_eval_run——任务体自管短会话只能看到已提交行）。
上传早拒照搬 admin_qa_pairs 先例（体积 seek/tell 先拒 + 扩展名/MIME 宽容口径 + 魔数
PK），坏 zip 不再漏到 500 兜底。评测题不参与问答检索（跑分引擎自带缓存旁路，F-602），
导入成功无需失效问答精确缓存。

跑分端点挂在独立 runs_router（prefix=/admin/eval-runs）：202 异步语义同 documents
上传先例，前端以 GET /{id} 轮询 detail；deps/视角部门经 add_task 实参注入——后台
任务拿不到 request/app.state，run 行也不存部门。
"""

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import AdminUser, DbSession
from app.core.errors import ERR_NOT_FOUND, ERR_PARAM_INVALID, ApiError
from app.db.models import Department, EvalRun
from app.services import eval_engine, eval_report, eval_service
from app.services.eval_service import RUN_NOT_FOUND_MSG
from app.services.xlsx_io import XlsxBatchError

router = APIRouter(prefix="/admin/eval-questions", tags=["admin-eval"])
runs_router = APIRouter(prefix="/admin/eval-runs", tags=["admin-eval"])

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TEMPLATE_FILENAME = "eval_questions_template.xlsx"
_MAX_IMPORT_BYTES: int = (
    50 * 1024 * 1024
)  # 与问答对/文档上传同口径：容纳 5000 行×期望答案 10000 字的最坏合法载荷
_MSG_BAD_FORMAT = "文件格式不支持或已损坏"


def _read_upload(file: UploadFile) -> bytes:
    """上传早拒（照搬 admin_qa_pairs._read_upload 先例）：体积 seek/tell 先拒，读全量
    后校验三重白名单的适用部分——扩展名 .xlsx、Content-Type（xlsx MIME 或
    octet-stream 宽容口径）、魔数 PK\\x03\\x04；任一不过 422/1003，返回文件字节流。"""
    file.file.seek(0, 2)  # SEEK_END
    if file.file.tell() > _MAX_IMPORT_BYTES:
        raise ApiError(ERR_PARAM_INVALID, "文件超过 50MB 上限", 422)
    file.file.seek(0)
    content = file.file.read()
    mime = file.content_type or ""
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise ApiError(ERR_PARAM_INVALID, _MSG_BAD_FORMAT, 422)
    if mime not in (_XLSX_MIME, "application/octet-stream", ""):
        raise ApiError(ERR_PARAM_INVALID, _MSG_BAD_FORMAT, 422)
    if not content.startswith(b"PK\x03\x04"):
        raise ApiError(ERR_PARAM_INVALID, _MSG_BAD_FORMAT, 422)
    return content


def _batch_rejected(exc: XlsxBatchError) -> JSONResponse:
    """XlsxBatchError → 422/1003：错误行列表整体放 data；文件级单一错误（row=0）
    的 reason 提为 message（用户首先要知道"文件无数据/超过行上限"这一层结论）。"""
    file_level = len(exc.errors) == 1 and exc.errors[0]["row"] == 0
    message = (
        str(exc.errors[0]["reason"])
        if file_level
        else f"导入文件存在 {len(exc.errors)} 处错误，已整批退回，未入库"
    )
    return JSONResponse(
        status_code=422,
        content={"code": ERR_PARAM_INVALID, "message": message, "data": exc.errors},
    )


@router.post("/import", status_code=201)
def import_eval_questions(
    file: Annotated[UploadFile, File()],
    user: AdminUser,
    db: DbSession,
) -> Response:
    """Excel 整批导入（F-601）：全合格 201 {imported}；错误行 422/1003 零入库。

    部门在文件行内（中文名 客服/人力/全员），与问答对导入的表单 deptCode 口径不同；
    题型与 `[拒答]` 双向归一在 service（判定协议第 3 条）。
    """
    try:
        result = eval_service.import_eval_questions(db, user.id, _read_upload(file))
    except XlsxBatchError as exc:
        return _batch_rejected(exc)
    return JSONResponse(status_code=201, content={"code": 0, "message": "ok", "data": result})


@router.get("/template")
def download_template(user: AdminUser) -> Response:
    """模板下载（F-601）：xlsx 字节流，Content-Disposition: attachment（§8.10）。"""
    return Response(
        content=eval_service.build_template(),
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{_TEMPLATE_FILENAME}"'},
    )


# ---------------------------------------------------------------------------
# 跑分（F-602）：POST 发起（202 异步）+ GET 列表 / 详情（轮询）
# ---------------------------------------------------------------------------

_RUN_NAME_MAX = 100  # 任务名单行文本，页面输入框量级；超限 422/1003 字段级


def _resolve_perspective_dept(db: Session, code: str) -> int:
    """视角部门 code → id：未播种 422（与导入 _resolve_depts"部门数据未初始化"同口径）。

    放 api 层的先例：api/chat.py._all_dept_id 同款单查 + 错误转换；engine 收 id，
    code 只在创建时被人读（审计 perspective）。
    """
    dept_id = db.scalar(select(Department.id).where(Department.code == code))
    if dept_id is None:
        raise ApiError(ERR_PARAM_INVALID, f"部门数据未初始化：{code}", 422)
    return dept_id


class EvalRunCreate(BaseModel):
    """POST /api/admin/eval-runs 请求体（camelCase §3.4）：name 非空 ≤100。"""

    name: str
    perspectiveDeptCode: str

    @field_validator("name", "perspectiveDeptCode")
    @classmethod
    def _strip_nonempty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped

    @field_validator("name")
    @classmethod
    def _check_name_length(cls, value: str) -> str:
        if len(value) > _RUN_NAME_MAX:
            raise ValueError(f"名称超过 {_RUN_NAME_MAX} 字上限")
        return value


def _run_summary(run: EvalRun) -> dict:
    """列表项 DTO（§3.4 camelCase，brief 钉字段）：Decimal 列转 float（correct 可半分）。"""
    return {
        "id": run.id,
        "name": run.name,
        "status": run.status,
        "total": run.total,
        "correct": float(run.correct),
        "accuracy": float(run.accuracy),
        "createdAt": run.created_at.isoformat(),
    }


@runs_router.post("", status_code=202)
def create_eval_run(
    body: EvalRunCreate, user: AdminUser, db: DbSession, request: Request, tasks: BackgroundTasks
) -> Response:
    """发起跑分（F-602）：running 行 + 审计 eval.run 落库提交后，后台执行 run_eval_run。

    202 {id}：跑分异步进行，前端轮询 GET /{id}；视角部门未播种 422（创建前置校验，
    不留无法执行的 running 行）。
    """
    dept_id = _resolve_perspective_dept(db, body.perspectiveDeptCode)
    run_id = eval_service.create_eval_run(db, user.id, body.name, dept_id)
    tasks.add_task(eval_engine.run_eval_run, run_id, request.app.state.chat_deps, dept_id)
    return JSONResponse(
        status_code=202, content={"code": 0, "message": "ok", "data": {"id": run_id}}
    )


@runs_router.get("")
def list_eval_runs(
    user: AdminUser,
    db: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """跑分任务列表：created_at DESC（同刻 id 降序兜底），分页 §3.5（§22.1 同口径）。"""
    total = db.scalar(select(func.count()).select_from(EvalRun))
    rows = (
        db.execute(
            select(EvalRun)
            .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
            .offset((page - 1) * pageSize)
            .limit(pageSize)
        )
        .scalars()
        .all()
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [_run_summary(run) for run in rows],
            "total": total,
            "page": page,
            "pageSize": pageSize,
        },
    }


@runs_router.get("/{run_id}")
def get_eval_run(run_id: int, user: AdminUser, db: DbSession) -> dict:
    """跑分详情：含 config 快照与 detail 全量（逐题判定与引用），轮询用；不存在 404。"""
    run = db.get(EvalRun, run_id)
    if run is None:
        raise ApiError(ERR_NOT_FOUND, RUN_NOT_FOUND_MSG, 404)
    return {
        "code": 0,
        "message": "ok",
        "data": {**_run_summary(run), "config": run.config, "detail": run.detail},
    }


# ---------------------------------------------------------------------------
# 人工评判（F-602 录分）+ 分组报告（F-603）
# ---------------------------------------------------------------------------


class JudgmentItem(BaseModel):
    """单题录分：judgment 三档枚举锁 API（判定协议第 1 条，防口径漂移）。"""

    questionId: int
    judgment: Literal["correct", "partial", "wrong"]


class JudgmentSubmit(BaseModel):
    """POST /api/admin/eval-runs/{id}/judgments 请求体；items 非空（空提交无意义
    且会写审计痕，Pydantic 先拒 422/1003）。"""

    items: list[JudgmentItem] = Field(min_length=1)


@runs_router.post("/{run_id}/judgments")
def submit_eval_judgments(
    run_id: int, body: JudgmentSubmit, user: AdminUser, db: DbSession
) -> Response:
    """人工评判录分（F-602）：detail 逐题 judgment + correct/accuracy 重算，200 回
    收口后的 summary + pendingCount（前端即时刷新）。

    仅 status='done' 可评：running/failed → 422/3004（ERR_EVAL_STATE，running 时
    后台任务正逐题写 detail，并发读-改-写互相覆盖）；questionId 不在明细 →
    422/1003 指名题号；不存在 → 404/3003。审计 eval.judge {runId, judged}。
    """
    run = eval_service.submit_judgments(
        db, run_id, user.id, [(item.questionId, item.judgment) for item in body.items]
    )
    return JSONResponse(
        status_code=200,
        content={
            "code": 0,
            "message": "ok",
            "data": {
                **_run_summary(run),
                "pendingCount": (run.detail or {}).get("pendingCount", 0),
            },
        },
    )


@runs_router.get("/{run_id}/report")
def get_eval_report(run_id: int, user: AdminUser, db: DbSession) -> dict:
    """分组报告（F-603）：{config, status, totals, byDept, byQtype}，明细从 detail
    聚合（合并口径=人工 judgment 优先否则 autoJudgment，见 eval_report 模块
    docstring）；不存在 404/3003。"""
    run = db.get(EvalRun, run_id)
    if run is None:
        raise ApiError(ERR_NOT_FOUND, RUN_NOT_FOUND_MSG, 404)
    return {"code": 0, "message": "ok", "data": eval_report.build_report(db, run)}
