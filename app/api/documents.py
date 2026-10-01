r"""文档路由（薄控制器）：multipart 上传 / zip 批量导入 / 文件名建议 / 列表 / 重试 / 确认，业务全部委托 services。

上传（admin-only）：表单元数据（camelCase）→ 合规勾选与部门 code 卡口（422）→
同步段 create_document（白名单/SHA-256 查重/落盘/登记 parsing，422/409 经全局
异常处理器）→ 异步段编排：settings.test_mode=True 时同步执行 process_document
（W1 测试断言用），否则 BackgroundTasks 独立会话执行。zip 批量导入（F-1007，
admin-only）：安全预扫描（§6.3）与逐条目元数据解析在 services/zip_import，
本层只接线调度回调——test_mode 同步解析，否则逐条挂 BackgroundTasks（依添加
顺序串行，并发=1 与单文件共用餐票）。列表（admin-only）：dept/status/keyword
过滤 + 分页（pageSize 1-100 默认 20，§22.1），created_at 降序（同刻以 id 降序
兜底），响应键名全 camelCase。retry（DOC-11）仅 failed 可重跑；confirm 激活
待确认文档（is_active=False → True）。两者成功响应前整清问答精确缓存
（request.app.state.chat_deps.cache.clear()，W3 Task 1）：文档状态变化改变可
检索事实，陈旧答案不能等 TTL 自然过期（W2 移交②）。
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, Request, UploadFile

from app.api.deps import AdminUser, DbSession
from app.core.assembly import build_embedder
from app.core.clock import get_clock
from app.core.config import get_settings
from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.core.storage import LocalFileStorage
from app.db.base import SessionLocal
from app.db.models import Document
from app.services import ingest as ingest_service
from app.services import metadata_suggest, zip_import
from app.services.departments import resolve_dept_id
from app.services.document_views import list_documents as list_document_rows
from app.services.document_views import list_versions
from app.services.ingest import IngestDeps, process_document

router = APIRouter(prefix="/documents", tags=["documents"])

_ALLOWED_STATUS: tuple[str, ...] = ("pending", "parsing", "ready", "failed")  # 与 DDL CHECK 同源
_MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024  # 与 check_sync 的 50MB 上限同源（API 侧早拒用）
_MSG_NOT_FOUND = "文档不存在"


def build_ingest_deps() -> IngestDeps:
    """异步段依赖包（装配口径同 T13，供本文件与 document_rebuild 共用）：
    embedder 走装配工厂（真实 TEI；test_mode 回 Stub）。"""
    settings = get_settings()
    return IngestDeps(
        parser_settings=settings,
        embedder=build_embedder(settings),
        clock=get_clock(),
        storage=LocalFileStorage(settings.storage_dir),
    )


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


def _process_in_background(doc_id: int) -> None:
    """后台进程段入口：请求会话已关，独立 SessionLocal 执行；失败由状态机落 failed。"""
    with SessionLocal() as session:
        process_document(session, build_ingest_deps(), doc_id)


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
        "dept_id": resolve_dept_id(db, dept),
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
        process_document(db, build_ingest_deps(), doc.id)
    else:
        background.add_task(_process_in_background, doc.id)
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "status": doc.status}}


@router.post("/import-zip", status_code=202)
def import_documents_zip(
    file: Annotated[UploadFile, File()],
    user: AdminUser,
    db: DbSession,
    background: BackgroundTasks,
) -> dict:
    """zip 批量导入（F-1007，admin-only）：202 {results, accepted, rejected, skipped}。

    合规确认（§7.4）：zip 导入是管理端管理员的显式批量操作，视同已勾选合规确认，
    不再要表单字段。安全上限与元数据解析在 services/zip_import（§6.3），本层只
    接线调度回调：test_mode 同步解析（响应可断言终态，同单文件口径），否则逐条
    挂 BackgroundTasks——依添加顺序串行执行（并发=1，与单文件共用既有编排）。
    """

    def on_accepted(doc_id: int) -> None:
        if get_settings().test_mode:
            process_document(db, build_ingest_deps(), doc_id)
        else:
            background.add_task(_process_in_background, doc_id)

    data = zip_import.import_zip(
        db,
        LocalFileStorage(get_settings().storage_dir),
        user,
        file.file,
        on_accepted,
    )
    return {"code": 0, "message": "ok", "data": data}


@router.post("/metadata-suggest")
def suggest_metadata(
    file: Annotated[UploadFile, File()],
    user: AdminUser,
) -> dict:
    """元数据自动建议（F-1008，admin-only）：只解析文件名、不读文件内容，返回上传
    表单预填建议。命名不符返回空建议 {}（不报错、不产出错误建议）；部门仅限库内
    枚举且限上传人管辖部门——AdminUser 门即管辖闸（试点 admin 管辖全部部门），
    文件名声明部门不得造成越权注入（§F-1008，详见 services/metadata_suggest）。"""
    meta = metadata_suggest.parse_filename_metadata(file.filename or "")
    if meta is None:
        return {"code": 0, "message": "ok", "data": {}}
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "suggestedDeptCode": meta.dept_code,
            "suggestedTitle": meta.title,
            "suggestedEffectiveDate": meta.effective_date.isoformat(),
        },
    }


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
    """文档列表（§22.1）：过滤条件可选，created_at 降序（同刻 id 降序兜底），分页返回。
    过滤语句与分页在 services/document_views（§1.6 api 禁直写查询），本层只做参数
    校验（dept code 枚举 / status 枚举）与 DTO 组装。"""
    _check_status(status)
    rows, total = list_document_rows(
        db,
        dept_id=resolve_dept_id(db, dept) if dept is not None else None,
        status=status,
        keyword=keyword,
        page=page,
        page_size=pageSize,
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [_doc_payload(doc, code) for doc, code in rows],
            "total": total,
            "page": page,
            "pageSize": pageSize,
        },
    }


def _check_status(status: str | None) -> None:
    """status 枚举校验：取值外 422 中文原因（与 DDL CHECK 同源的 _ALLOWED_STATUS）。"""
    if status is not None and status not in _ALLOWED_STATUS:
        raise ApiError(ERR_PARAM_INVALID, f"状态取值无效：{status}", 422)


@router.get("/{doc_id}/versions")
def get_document_versions(doc_id: int, user: AdminUser, db: DbSession) -> dict:
    """版本历史（F-303，W7 补齐）：同部门同主题全部代次按 version 降序（含当前行），
    归档版本在案可追溯；寻址文档不存在 404。查询在 services/document_views。"""
    rows = list_versions(db, doc_id)
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": [
                {
                    "id": row.id,
                    "version": row.version,
                    "status": row.status,
                    "isActive": row.is_active,
                    "effectiveDate": row.effective_date.isoformat(),
                    "sizeBytes": row.size_bytes,
                    "ownerName": row.owner_name,
                    "uploadedBy": row.uploaded_by,
                    "createdAt": row.created_at.isoformat(),
                }
                for row in rows
            ]
        },
    }


@router.post("/{doc_id}/retry")
def retry_document(
    doc_id: int, user: AdminUser, db: DbSession, background: BackgroundTasks, request: Request
) -> dict:
    """失败重试（DOC-11）：仅 failed 可重跑；非测试模式先落 parsing 再交后台接管。
    成功响应前整清问答精确缓存：重试改变文档可检索事实，TTL 不可依赖（W3 Task 1）。"""
    doc = db.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_FOUND, 404)
    if doc.status != "failed":
        raise ApiError(ERR_PARAM_INVALID, "仅失败状态的文档可以重试", 422)
    if get_settings().test_mode:
        process_document(db, build_ingest_deps(), doc.id)
    else:
        doc.status = "parsing"  # 后台接管前先落 parsing：列表即时反馈（DOC-11 状态回 parsing）
        db.commit()
        background.add_task(_process_in_background, doc.id)
    request.app.state.chat_deps.cache.clear()  # 成功响应前失效：宁可多一次生成不留旧答案
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "status": doc.status}}


@router.post("/{doc_id}/confirm")
def confirm_document(doc_id: int, user: AdminUser, db: DbSession, request: Request) -> dict:
    """待确认激活：管理员确认敏感/近似命中文档 → is_active=True（service 负责审计与提交）。
    成功响应前整清问答精确缓存：激活改变可检索事实，TTL 不可依赖（W3 Task 1）。"""
    doc = ingest_service.confirm_document(db, doc_id, actor_user_id=user.id)
    request.app.state.chat_deps.cache.clear()  # 成功响应前失效（同 retry，W2 移交②）
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "isActive": doc.is_active}}
