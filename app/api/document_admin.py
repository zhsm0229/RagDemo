r"""文档治理路由（自 documents.py 按域拆出，行数红线 §1.4）：列表 / 版本历史 / 重试 /
确认激活 / 删除 / 草稿审核 / 分型修正与回填，业务全部委托 services。

列表（admin-only）：dept/status/keyword/sourceType/reviewStatus 过滤 + 分页
（pageSize 1-100 默认 20，§22.1），created_at 降序（同刻以 id 降序兜底），响应键名全
camelCase。retry（DOC-11）仅 failed 可重跑；confirm 激活待确认文档（is_active=False →
True）；delete 软删（F-305）；draft/approve|draft/reject（agent-evolution #05）走
services/draft_review 状态机——通过后触发既有解析入库管道（同 retry 的 test_mode
分支编排），驳回弹原因、永不入库（红线：无审核不落地，agent-evolution §五）。
寻址失败 404/3003（findings I13：与全仓"不存在与无权同判"口径统一，文案保持
"文档不存在"）。状态变化成功响应前整清问答精确缓存
（request.app.state.chat_deps.cache.clear()，W3 Task 1）：文档状态变化改变可检索
事实，陈旧答案不能等 TTL 自然过期（W2 移交②；驳回不改变可检索事实，不整清）。
分型域（metrics-balance 工单 01）走独立 admin_router（/api/admin/documents，spec §3.4
钉路径）：PATCH /doc-type 仅此一字段、POST /backfill-doc-type 存量全量重算——分型只
影响查询期 top_n 档位与报告分组、不改可检索事实，故均不清问答缓存（§3.4 落锤）。
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, DbSession
from app.api.documents import build_ingest_deps, process_in_background
from app.core.clock import get_clock
from app.core.config import get_settings
from app.core.errors import ERR_NOT_FOUND, ERR_PARAM_INVALID, ApiError
from app.db.models import Document
from app.services import draft_review
from app.services import ingest as ingest_service
from app.services.departments import resolve_dept_id
from app.services.doc_type import backfill_doc_types, set_doc_type
from app.services.document_views import list_documents as list_document_rows
from app.services.document_views import list_versions
from app.services.ingest import process_document

router = APIRouter(prefix="/documents", tags=["documents"])
# 分型域独立 router：spec §3.4 钉路径 /api/admin/documents/*，与治理域 /api/documents/* 并存
admin_router = APIRouter(prefix="/admin/documents", tags=["documents"])

_ALLOWED_STATUS: tuple[str, ...] = ("pending", "parsing", "ready", "failed")  # 与 DDL CHECK 同源
_ALLOWED_SOURCE_TYPE: tuple[str, ...] = ("upload", "draft")  # 与 0007 迁移 CHECK 同源
_MSG_NOT_FOUND = "文档不存在"


def _doc_payload(doc: Document, dept_code: str) -> dict:
    """列表项（camelCase，standards §3.4）：数据库行不直接返回。
    degraded：ready 且带降级标注（MinerU 不可用走 pypdf）——前端独立灰 tag，
    与失败原因（parseError）分字段，不再混用一列（deviations #24 兑现）。"""
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
        "parseErrorDetail": doc.parse_error_detail,
        "parseStage": doc.parse_stage,
        "parseProgress": doc.parse_progress,
        "degraded": doc.status == "ready" and bool(doc.parse_error),
        "sourceType": doc.source_type,
        "reviewStatus": doc.review_status,
        "rejectReason": doc.reject_reason,
        "isActive": doc.is_active,
        "docType": doc.doc_type,
        "uploadedBy": doc.uploaded_by,
        "createdAt": doc.created_at.isoformat(),
        "updatedAt": doc.updated_at.isoformat(),
    }


class DraftRejectRequest(BaseModel):
    """驳回请求体：原因必填（≤500 字，审计与列表悬停展示用）。"""

    reason: str = Field(min_length=1, max_length=500)


class DocTypeUpdateRequest(BaseModel):
    """分型修正请求体（metrics-balance §3.4 落锤：现无文档字段编辑端点，仅此一字段）。"""

    docType: str


def _check_status(status: str | None) -> None:
    """status 枚举校验：取值外 422 中文原因（与 DDL CHECK 同源的 _ALLOWED_STATUS）。"""
    if status is not None and status not in _ALLOWED_STATUS:
        raise ApiError(ERR_PARAM_INVALID, f"状态取值无效：{status}", 422)


def _check_enum(value: str | None, allowed: tuple[str, ...], label: str) -> None:
    """query 枚举通用校验（sourceType/reviewStatus 复用）：取值外 422 中文原因。"""
    if value is not None and value not in allowed:
        raise ApiError(ERR_PARAM_INVALID, f"{label}取值无效：{value}", 422)


@router.get("")
def list_documents(
    user: AdminUser,
    db: DbSession,
    dept: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
    sourceType: Annotated[str | None, Query()] = None,
    reviewStatus: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    pageSize: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """文档列表（§22.1）：过滤条件可选，created_at 降序（同刻 id 降序兜底），分页返回。
    过滤语句与分页在 services/document_views（§1.6 api 禁直写查询），本层只做参数
    校验（dept code 枚举 / status / sourceType / reviewStatus 枚举）与 DTO 组装。"""
    _check_status(status)
    _check_enum(sourceType, _ALLOWED_SOURCE_TYPE, "来源")
    _check_enum(reviewStatus, draft_review.ALLOWED_REVIEW_STATUS, "审核状态")
    rows, total = list_document_rows(
        db,
        dept_id=resolve_dept_id(db, dept) if dept is not None else None,
        status=status,
        keyword=keyword,
        page=page,
        page_size=pageSize,
        source_type=sourceType,
        review_status=reviewStatus,
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
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    if doc.status != "failed":
        raise ApiError(ERR_PARAM_INVALID, "仅失败状态的文档可以重试", 422)
    if get_settings().test_mode:
        process_document(db, build_ingest_deps(), doc.id)
    else:
        doc.status = "parsing"  # 后台接管前先落 parsing：列表即时反馈（DOC-11 状态回 parsing）
        db.commit()
        background.add_task(process_in_background, doc.id)
    request.app.state.chat_deps.cache.clear()  # 成功响应前失效：宁可多一次生成不留旧答案
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "status": doc.status}}


@router.post("/{doc_id}/confirm")
def confirm_document(doc_id: int, user: AdminUser, db: DbSession, request: Request) -> dict:
    """待确认激活：管理员确认敏感/近似命中文档 → is_active=True（service 负责审计与提交）。
    成功响应前整清问答精确缓存：激活改变可检索事实，TTL 不可依赖（W3 Task 1）。"""
    doc = ingest_service.confirm_document(db, doc_id, actor_user_id=user.id)
    request.app.state.chat_deps.cache.clear()  # 成功响应前失效（同 retry，W2 移交②）
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "isActive": doc.is_active}}


@router.delete("/{doc_id}")
def delete_document(doc_id: int, user: AdminUser, db: DbSession, request: Request) -> dict:
    """文档删除（F-305，2026-10-06 立项实施）：软删+级联停用 chunks（service 负责审计与提交）。
    成功响应前整清问答精确缓存：删除改变可检索事实，TTL 不可依赖（同 retry/confirm，W2 移交②）。
    权限：AdminUser——语料治理是管理员职责（member 403/2004，评审材料 §1）。"""
    ingest_service.delete_document(db, get_clock(), doc_id, actor_user_id=user.id)
    request.app.state.chat_deps.cache.clear()  # 成功响应前失效（同 retry，W2 移交②）
    return {"code": 0, "message": "ok", "data": {"id": doc_id, "deleted": True}}


@router.post("/{doc_id}/draft/approve")
def approve_draft(
    doc_id: int, user: AdminUser, db: DbSession, background: BackgroundTasks, request: Request
) -> dict:
    """草稿审核通过（agent-evolution #05，admin-only）：待审核 → 已通过，审计在 service
    （与状态变更同事务）；随后触发既有解析入库管道——编排与 retry 同构：test_mode 同步
    执行（响应即可断言终态），生产置 parsing 后挂 BackgroundTasks。成功响应前整清问答
    精确缓存：通过改变可检索事实（草稿将从不可检索转为可检索），TTL 不可依赖。"""
    doc = draft_review.approve_draft(db, get_clock(), doc_id, actor_user_id=user.id)
    if get_settings().test_mode:
        process_document(db, build_ingest_deps(), doc.id)
    else:
        doc.status = "parsing"  # 后台接管前先落 parsing：列表即时反馈（同 retry 编排）
        db.commit()
        background.add_task(process_in_background, doc.id)
    request.app.state.chat_deps.cache.clear()  # 成功响应前失效（同 retry/confirm，W2 移交②）
    return {
        "code": 0,
        "message": "ok",
        "data": {"id": doc.id, "reviewStatus": doc.review_status, "status": doc.status},
    }


@router.post("/{doc_id}/draft/reject")
def reject_draft(
    doc_id: int, payload: DraftRejectRequest, user: AdminUser, db: DbSession, request: Request
) -> dict:
    """草稿驳回（agent-evolution #05，admin-only）：待审核 → 已驳回 + 原因留档，审计在
    service。驳回不改变可检索事实（草稿本就不可检索，status 停在 pending 永不解析），
    故不整清问答缓存（口径见模块注释）。"""
    doc = draft_review.reject_draft(
        db, get_clock(), doc_id, actor_user_id=user.id, reason=payload.reason
    )
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "id": doc.id,
            "reviewStatus": doc.review_status,
            "rejectReason": doc.reject_reason,
        },
    }


@admin_router.patch("/{doc_id}/doc-type")
def update_doc_type(
    doc_id: int, payload: DocTypeUpdateRequest, user: AdminUser, db: DbSession
) -> dict:
    """人工修正分型（metrics-balance §3.4，admin-only）：仅此一字段，校验/审计/提交在
    services/doc_type（枚举外 422、寻址失败 404"文档不存在"）。不清问答精确缓存：
    docType 只影响查询期 top_n 档位与报告分组，不改可检索事实（§3.4 落锤）；编辑不限
    文档状态（傻瓜式，改错可改回），审计 document.set_doctype 留痕。"""
    doc = set_doc_type(db, get_clock(), doc_id, payload.docType, actor_user_id=user.id)
    return {"code": 0, "message": "ok", "data": {"id": doc.id, "docType": doc.doc_type}}


@admin_router.post("/backfill-doc-type")
def backfill_doc_type(user: AdminUser, db: DbSession) -> dict:
    """存量分型回填（metrics-balance §3.4，admin-only）：对全部 is_active 且 ready 文档
    从 chunks 重算分型（前缀剥离适配器在 services/doc_type，与 ingest 期口径一致），
    覆盖现有值（含手工修正——前端确认文案必须写明）；返回 {updated, byType}，审计
    document.backfill_doctype。不清问答缓存（同 PATCH：不改可检索事实）。"""
    result = backfill_doc_types(db, get_clock(), actor_user_id=user.id)
    return {"code": 0, "message": "ok", "data": result}
