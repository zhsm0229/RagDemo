r"""重建索引路由（F-304，W6 后追加）：POST /documents/rebuild-index（全库级）+
POST /documents/{doc_id}/rebuild-index（文档级）——独立成文件因 documents.py 已近
行数上限（同 admin_eval.py/admin_retrieval_test.py 先例，§1.4）。

admin-only（AdminUser 门）；202 异步语义同 documents 上传/retry 先例：test_mode 同步
执行便于断言终态，生产 BackgroundTasks 逐文档独立会话（依添加顺序串行，并发=1 与
入库共用餐票）。全库级先同步受理计数再交后台；受理校验与执行同源
（services.reindex.ensure_rebuildable），拒绝在受理段即返回，不留无法执行的后台任务。
成功响应前整清问答精确缓存（重建改变可检索事实，TTL 不可依赖，同 retry 口径）。
"""

from fastapi import APIRouter, BackgroundTasks, Request
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession
from app.api.documents import build_ingest_deps
from app.core.config import get_settings
from app.db.base import SessionLocal
from app.db.models import Document
from app.services.reindex import ensure_rebuildable, rebuild_document_index, rebuild_ready_indexes

router = APIRouter(prefix="/documents", tags=["documents"])


def _rebuild_one_in_background(doc_id: int, actor_id: int) -> None:
    """后台任务体：请求会话已关，独立 SessionLocal 执行；失败由状态机落 failed。"""
    with SessionLocal() as session:
        rebuild_document_index(session, build_ingest_deps(), doc_id, actor_id)


def _rebuild_all_in_background(actor_id: int) -> None:
    """全库后台任务体：逐文档独立事务（services 层循环），单文档失败不中断批次。"""
    with SessionLocal() as session:
        rebuild_ready_indexes(session, build_ingest_deps(), actor_id)


@router.post("/rebuild-index", status_code=202)
def rebuild_library(
    user: AdminUser, db: DbSession, background: BackgroundTasks, request: Request
) -> dict:
    """全库级重建（F-304）：受理全部就绪文档 202 {accepted}，后台逐个重刷。"""
    accepted = len(
        list(
            db.scalars(
                select(Document.id).where(Document.status == "ready", Document.deleted_at.is_(None))
            )
        )
    )
    if get_settings().test_mode:
        rebuild_ready_indexes(db, build_ingest_deps(), user.id)
    else:
        background.add_task(_rebuild_all_in_background, user.id)
    request.app.state.chat_deps.cache.clear()  # 全库重建：全部文档变更，整清（工单 23 保留整清语义）
    return {"code": 0, "message": "ok", "data": {"accepted": accepted}}


@router.post("/{doc_id}/rebuild-index", status_code=202)
def rebuild_document(
    doc_id: int, user: AdminUser, db: DbSession, background: BackgroundTasks, request: Request
) -> dict:
    """文档级重建（F-304）：202 {id, status}；受理校验拒绝同步返回（404/422）。"""
    if get_settings().test_mode:
        doc = rebuild_document_index(db, build_ingest_deps(), doc_id, user.id)
        status = doc.status
    else:
        ensure_rebuildable(db.get(Document, doc_id))  # 受理段校验，执行段后台同口径复验
        status = "parsing"  # 与 retry 同款即时反馈：后台接管前先示意处理中
        background.add_task(_rebuild_one_in_background, doc_id, user.id)
    request.app.state.chat_deps.cache.clear_by_ref("doc", doc_id)  # 文档级受理即精准失效（工单 23）
    return {"code": 0, "message": "ok", "data": {"id": doc_id, "status": status}}
