r"""文档内容回显路由（自 documents.py 按域拆出，行数红线 §1.4）：原文件 / 在线预览 / 内嵌图片。

用户侧回显三端点（ReadyUser：首登未改密 403/2004，与 citations/conversations/chat 同口径，
I12）：共用 _viewable_document 存活+隔离闸——文档存在、未软删、is_active、ready，且部门
在本部门或全员内，撤回/未就绪不泄漏内容。寻址失败/无权访问 404/3003（与全仓"不存在与
无权同判"口径一致，findings I13），文案保持"文档不存在"。独立成文件因 documents.py 已近
行数上限（同 document_rebuild.py 先例，§1.4）。
"""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import DbSession, ReadyUser
from app.core.config import get_settings
from app.core.errors import ERR_NOT_FOUND, ApiError
from app.db.models import Document, DocumentImage
from app.services.document_preview import resolve_preview_pdf

router = APIRouter(prefix="/documents", tags=["documents"])

_MSG_NOT_FOUND = "文档不存在"


def _viewable_document(db, doc_id: int, user) -> Document:
    """回显/原文件共用的存活+隔离闸（与 citations doc_content 同口径）：文档存在、
    未软删、is_active、ready，且部门在本部门或全员内——撤回/未就绪不泄漏内容。"""
    from app.services.departments import all_dept_id

    doc = db.get(Document, doc_id)
    if (
        doc is None
        or doc.deleted_at is not None
        or not doc.is_active
        or doc.status != "ready"
        or doc.dept_id not in (user.dept_id, all_dept_id(db))
    ):
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    return doc


@router.get("/{doc_id}/images/{image_index}")
def get_document_image(
    doc_id: int,
    image_index: int,
    user: ReadyUser,
    db: DbSession,
) -> FileResponse:
    """内嵌图片回显（F-1017）：切片"图片N："标记对应原图，供引用原文核对展示。
    ReadyUser：与 citations/conversations/chat 同口径，首登未改密 403/2004（I12）。"""
    doc = _viewable_document(db, doc_id, user)
    if image_index < 1:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    settings = get_settings()
    image = db.scalars(
        select(DocumentImage).where(
            DocumentImage.document_id == doc.id, DocumentImage.image_index == image_index
        )
    ).first()
    if image is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    path = Path(settings.storage_dir) / image.file_path
    if not path.is_file():
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    return FileResponse(path, media_type=image.mime)


@router.get("/{doc_id}/file")
def get_document_file(doc_id: int, user: ReadyUser, db: DbSession) -> FileResponse:
    """原文件下载/打开（F-1018）：老格式（doc/ppt/xls）返回的是入库转换产物
    （2026-10-05 起为 PDF，见 F-1015 裁定），original_filename 保留真实来源命名。
    ReadyUser：首登未改密 403/2004，与其他用户侧文档端点同口径（I12）。"""
    doc = _viewable_document(db, doc_id, user)
    settings = get_settings()
    path = Path(settings.storage_dir) / doc.file_path
    if not path.is_file():
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    quoted = quote(doc.original_filename)
    return FileResponse(
        path,
        media_type=doc.mime or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


@router.get("/{doc_id}/preview")
def get_document_preview(doc_id: int, user: ReadyUser, db: DbSession) -> FileResponse:
    """原文件在线预览：统一 PDF inline（评审材料 2026-10-05），前端 iframe 内嵌渲染。

    分格式策略与缓存见 services/document_preview.py；文本类（md/txt/csv/html）前端
    走 /file 取文本转义渲染，不调本端点。ReadyUser：首登未改密 403/2004（I12）。"""
    doc = _viewable_document(db, doc_id, user)
    path, filename = resolve_preview_pdf(doc, get_settings())
    quoted = quote(filename)
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quoted}"},
    )
