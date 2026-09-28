r"""文档路由（薄控制器）：multipart 上传 / 列表 / 重试 / 确认，业务全部委托 services/ingest。

上传（admin-only）：表单元数据（camelCase）→ 合规勾选与部门 code 卡口（422）→
同步段 create_document（白名单/SHA-256 查重/落盘/登记 parsing，422/409 经全局
异常处理器）→ 异步段编排：settings.test_mode=True 时同步执行 process_document
（W1 测试断言用），否则 BackgroundTasks 独立会话执行。列表（admin-only）：
dept/status/keyword 过滤 + 分页（pageSize 1-100 默认 20，§22.1），created_at
降序（同刻以 id 降序兜底），响应键名全 camelCase。retry（DOC-11）仅 failed 可
重跑；confirm 激活待确认文档（is_active=False → True）。
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import AdminUser, DbSession
from app.core.clock import get_clock
from app.core.config import get_settings
from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.core.llm import StubEmbedder
from app.core.storage import LocalFileStorage
from app.db.base import SessionLocal
from app.db.models import Department, Document
from app.services import ingest as ingest_service
from app.services.ingest import IngestDeps, process_document

router = APIRouter(prefix="/documents", tags=["documents"])

_ALLOWED_STATUS: tuple[str, ...] = ("pending", "parsing", "ready", "failed")  # 与 DDL CHECK 同源
_MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024  # 与 check_sync 的 50MB 上限同源（API 侧早拒用）
_MSG_NOT_FOUND = "文档不存在"


def _early_reject_oversize(file: UploadFile) -> None:
    """读全文之前用 seek/tell 量体积早拒超大文件（>50MB → 422/1003）。

    为什么不在同步段 check_sync 里拒：那要先 read() 整个文件进内存。seek 只动
    文件指针，51MB 的请求体在字节进业务层之前即被退回（T14 审查 Important）。
    """
    file.file.seek(0, 2)  # SEEK_END
    size = file.file.tell()
    file.file.seek(0)
    if size > _MAX_UPLOAD_BYTES:
        raise ApiError(ERR_PARAM_INVALID, "文件超过 50MB 上限", 422)


def _resolve_dept(db: Session, code: str) -> int:
    """部门 code → dept_id（W2 移交的枚举校验在本任务落地）：枚举外 422 中文原因。"""
    dept = db.scalars(select(Department).where(Department.code == code)).first()
    if dept is None:
        raise ApiError(ERR_PARAM_INVALID, f"部门编码无效：{code}（可选 service/hr/all）", 422)
    return dept.id


def _build_deps() -> IngestDeps:
    """异步段依赖包（装配口径同 T13）：embedder 暂为确定性 Stub，W2 激活 TeiEmbedder 后替换。"""
    settings = get_settings()
    return IngestDeps(
        parser_settings=settings,
        embedder=StubEmbedder(),
        clock=get_clock(),
        storage=LocalFileStorage(settings.storage_dir),
    )


def _process_in_background(doc_id: int) -> None:
    """后台进程段入口：请求会话已关，独立 SessionLocal 执行；失败由状态机落 failed。"""
    with SessionLocal() as session:
        process_document(session, _build_deps(), doc_id)


def _doc_payload(doc: Document, dept_code: str) -> dict:
    """列表项（camelCase，standards §3.4）：数据库行不直接返回。"""
    return {
        "id": doc.id,
        "title": doc.title,
        "deptId": doc.dept_id,
        "deptCode": dept_code,
        "originalFilename": doc.original_filename,
        "mime": doc.mime,
        "sizeBytes": doc.size_bytes,
        "version": doc.version,
        "effectiveDate": doc.effective_date.isoformat(),
        "expiryDate": doc.expiry_date.isoformat() if doc.expiry_date else None,
        "ownerName": doc.owner_name,
        "status": doc.status,
        "parseEngine": doc.parse_engine,
        "parseError": doc.parse_error,
        "isActive": doc.is_active,
        "uploadedBy": doc.uploaded_by,
        "createdAt": doc.created_at.isoformat(),
        "updatedAt": doc.updated_at.isoformat(),
    }


@router.post("", status_code=202)
def upload_document(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(min_length=1)],
    dept: Annotated[str, Form(min_length=1)],
    owner: Annotated[str, Form(min_length=1)],
    effectiveDate: Annotated[date, Form()],
    complianceConfirmed: Annotated[bool, Form()],
    user: AdminUser,
    db: DbSession,
    background: BackgroundTasks,
    expiryDate: Annotated[date | None, Form()] = None,
) -> dict:
    """上传（DOC-02/03/04/05 卡口在同步段）：合规未勾 422；成功 202 {id,status}。"""
    if not complianceConfirmed:
        raise ApiError(ERR_PARAM_INVALID, "请先勾选内容合规确认后再上传", 422)
    _early_reject_oversize(file)
    meta = {
        "dept_id": _resolve_dept(db, dept),
        "title": title,
        "owner_name": owner,
        "effective_date": effectiveDate,
        "expiry_date": expiryDate,
    }
    doc = ingest_service.create_document(
        db,
        LocalFileStorage(get_settings().storage_dir),
        user,
        meta,
        file.file.read(),
        file.filename or "",
        file.content_type or "",
    )
    if get_settings().test_mode:  # 测试模式同步执行，响应即可断言终态；生产走后台任务
        process_document(db, _build_deps(), doc.id)
    else:
        background.add_task(_process_in_background, doc.id)
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "status": doc.status}}


@router.get("")
def list_documents(
    user: AdminUser,
    db: DbSession,
    dept: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """文档列表（§22.1）：过滤条件可选，created_at 降序（同刻 id 降序兜底），分页返回。"""
    filters = []
    if dept is not None:
        filters.append(Document.dept_id == _resolve_dept(db, dept))
    if status is not None:
        if status not in _ALLOWED_STATUS:
            raise ApiError(ERR_PARAM_INVALID, f"状态取值无效：{status}", 422)
        filters.append(Document.status == status)
    if keyword:
        like = f"%{keyword}%"
        filters.append(or_(Document.title.ilike(like), Document.original_filename.ilike(like)))
    stmt = select(Document, Department.code).join(Department, Document.dept_id == Department.id)
    if filters:
        stmt = stmt.where(*filters)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(Document.created_at.desc(), Document.id.desc())
        .offset((page - 1) * pageSize)
        .limit(pageSize)
    ).all()
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [_doc_payload(doc, code) for doc, code in rows],
            "total": total or 0,
            "page": page,
            "pageSize": pageSize,
        },
    }


@router.post("/{doc_id}/retry")
def retry_document(
    doc_id: int, user: AdminUser, db: DbSession, background: BackgroundTasks
) -> dict:
    """失败重试（DOC-11）：仅 failed 可重跑；非测试模式先落 parsing 再交后台接管。"""
    doc = db.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_FOUND, 404)
    if doc.status != "failed":
        raise ApiError(ERR_PARAM_INVALID, "仅失败状态的文档可以重试", 422)
    if get_settings().test_mode:
        process_document(db, _build_deps(), doc.id)
    else:
        doc.status = "parsing"  # 后台接管前先落 parsing：列表即时反馈（DOC-11 状态回 parsing）
        db.commit()
        background.add_task(_process_in_background, doc.id)
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "status": doc.status}}


@router.post("/{doc_id}/confirm")
def confirm_document(doc_id: int, user: AdminUser, db: DbSession) -> dict:
    """待确认激活：管理员确认敏感/近似命中文档 → is_active=True（service 负责审计与提交）。"""
    doc = ingest_service.confirm_document(db, doc_id, actor_user_id=user.id)
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "isActive": doc.is_active}}
