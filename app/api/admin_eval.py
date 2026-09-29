r"""管理端评测题库路由（薄控制器）：Excel 导入（F-601）与模板下载。

业务全部委托 services/eval_service：XlsxBatchError（结构级 + 行级聚合错误行列表）
统一转 422/1003，错误行列表直接放 data（standards §21.9），行号是 xlsx_io 携带的
Excel 物理行号；文件级单一错误（row=0）把 reason 提为 message。上传早拒照搬
admin_qa_pairs 先例（体积 seek/tell 先拒 + 扩展名/MIME 宽容口径 + 魔数 PK），坏
zip 不再漏到 500 兜底。评测题不参与问答检索（跑分引擎自带缓存旁路，F-602），
导入成功无需失效问答精确缓存。
"""

from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse, Response

from app.api.deps import AdminUser, DbSession
from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.services import eval_service
from app.services.xlsx_io import XlsxBatchError

router = APIRouter(prefix="/admin/eval-questions", tags=["admin-eval"])

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
