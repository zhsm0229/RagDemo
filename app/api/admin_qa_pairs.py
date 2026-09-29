r"""管理端问答对路由（薄控制器）：Excel 整批导入（F-401）与模板下载（F-403）。

业务全部委托 services/qa_pairs：XlsxBatchError（结构级+行级聚合错误行列表）统一
转 422/1003，错误行列表直接放 data（standards §21.9），行号是 xlsx_io 携带的
Excel 物理行号；文件级单一错误（row=0）把 reason 提为 message，让"文件无数据/
超上限"在提示层可见；部门解析等业务错误走 ApiError 经全局异常处理器。上传早拒
（同 documents 先例）：体积 seek/tell 先拒 + 扩展名/MIME/魔数三重白名单适用
部分，坏 zip 不再漏到 500 兜底（评审 Important-2）。导入成功后整清问答精确
缓存——新问答对改变可检索事实，陈旧答案不能等 TTL 自然过期（同 W3 Task 1
文档 retry/confirm 口径）。
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.api.deps import AdminUser, DbSession
from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.core.llm import Embedder
from app.services import qa_pairs as qa_pairs_service
from app.services.xlsx_io import XlsxBatchError

router = APIRouter(prefix="/admin/qa-pairs", tags=["admin-qa-pairs"])

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TEMPLATE_FILENAME = "qa_pairs_template.xlsx"
_MAX_IMPORT_BYTES: int = (
    50 * 1024 * 1024
)  # 与文档上传同口径：容纳 5000 行×答案 10000 字的最坏合法载荷
_MSG_BAD_FORMAT = "文件格式不支持或已损坏"


def _read_upload(file: UploadFile) -> bytes:
    """上传早拒（同 documents._early_reject_oversize 先例）：体积 seek/tell 先拒，
    读全量后校验三重白名单的适用部分——扩展名 .xlsx、Content-Type（xlsx MIME 或
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


def _request_embedder(request: Request) -> Embedder:
    """问答链路同一 embedder 实例（lifespan 装配于 app.state.chat_deps）：问题向量
    必须与检索侧同指纹空间，禁止另建（装配层唯一选择实现，standards §1.2）。"""
    return request.app.state.chat_deps.embedder


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
def import_qa_pairs(
    request: Request,
    file: Annotated[UploadFile, File()],
    user: AdminUser,
    db: DbSession,
    deptCode: Annotated[str, Form()] = "all",
) -> Response:
    """Excel 整批导入（F-401）：全合格 201 {imported,batchNo}；错误行 422/1003 零入库。

    deptCode 缺省 all（全员可见）：模板不含部门列，部门由请求指定（service|hr|all），
    保证 F-204 检索 dept_id IN (用户部门, all) 语义可用。
    """
    try:
        result = qa_pairs_service.import_qa_pairs(
            db, _request_embedder(request), user.id, _read_upload(file), deptCode
        )
    except XlsxBatchError as exc:
        return _batch_rejected(exc)
    request.app.state.chat_deps.cache.clear()  # 成功响应前失效：可检索事实已变化
    return JSONResponse(status_code=201, content={"code": 0, "message": "ok", "data": result})


@router.get("/template")
def download_template(user: AdminUser) -> Response:
    """模板下载（F-403）：xlsx 字节流，Content-Disposition: attachment（§8.10）。"""
    return Response(
        content=qa_pairs_service.build_template(),
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{_TEMPLATE_FILENAME}"'},
    )
