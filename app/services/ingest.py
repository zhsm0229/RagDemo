r"""入库状态机（F1 编排核心）：同步段 create_document、异步段 process_document、confirm/expire。

同步段（上传请求内毫秒级）：check_sync 白名单 → SHA-256 精确查重（409 指向已有 id，
软删不占 hash 可重传）→ 落盘 → 登记 parsing 且 is_active=False（不占"单一生效版本"
部分索引）→ 审计。异步段编排（任何一步失败 → failed+中文原因，绝不落半成品块）：
analysis（解析/红线/清洗/乱码/敏感/simhash/分块/embed，见 ingest_pipeline.py）→
单事务写 chunks（先删本 doc 旧块，重试幂等）+ 同标题旧版归档 → ready；敏感/近似
命中 → is_active=False 待确认。confirm 激活；expire 到期下线（F-1004）。
拆分（行数红线 §1.4）：老格式转换编排见 ingest_convert.py，ready/failed 收口段
（含 document 审计薄封装）见 ingest_commit.py——本文件只留编排与状态迁移。
"""

import logging
from pathlib import PureWindowsPath

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import get_settings
from app.core.convert import _LEGACY_EXTS
from app.core.errors import (
    ERR_DUPLICATE_DOC,
    ERR_NOT_FOUND,
    ERR_PARAM_INVALID,
    ERR_UNSUPPORTED_FORMAT,
    ApiError,
)
from app.core.storage import FileStorage
from app.db.models import Chunk, Document, User
from app.rag.precheck import check_sync as check_sync_gate
from app.services.document_preview import cleanup_previews
from app.services.ingest_commit import audit_document, commit_failed, commit_ready
from app.services.ingest_convert import convert_legacy_document
from app.services.ingest_pipeline import IngestDeps, analyze_document

_MSG_NOT_FOUND = "文档不存在"
logger = logging.getLogger(__name__)
_META_REQUIRED: tuple[str, ...] = ("dept_id", "title", "owner_name", "effective_date")
_SYNC_REJECTS: dict[str, str] = {
    "ext_not_allowed": "仅支持 pdf/docx/md/txt/csv/xlsx/pptx/html 格式（doc/ppt/xls 自动转换）",
    "mime_not_allowed": "文件 MIME 类型不在白名单内",
    "too_large": "文件大小超过 50MB 上限",
    "magic_mismatch": "文件内容与扩展名不符，请勿伪造格式",
    "macro_not_allowed": "含宏格式不支持，请另存为无宏版本后上传",
    "convert_unavailable": "老格式转换服务未配置，请另存为 docx/pptx/xlsx 后上传",
}


def create_document(
    session: Session,
    storage: FileStorage,
    uploader: User,
    meta: dict,
    content: bytes,
    filename: str,
    mime: str,
) -> Document:
    """同步段：元数据必填 → 白名单 → 查重 → 落盘 → 登记 parsing → 审计并提交。

    查重双保险：reject_duplicate 挡同步可见的重复；提交段（_commit_upload）
    IntegrityError 捕 uq_doc_content_hash 的并发抢注（DOC-06），同样映射 409。
    """
    missing = [key for key in _META_REQUIRED if not meta.get(key)]
    if missing:
        raise ApiError(ERR_PARAM_INVALID, f"元数据缺失：{'、'.join(missing)}", 422)
    gate = check_sync_gate(filename, mime, content)
    if not gate.ok:
        raise sync_rejected(gate.reason)
    reject_duplicate(session, gate.sha256)
    stored = storage.save(content, PureWindowsPath(filename).suffix.lower())
    doc = Document(
        title=meta["title"],
        dept_id=meta["dept_id"],
        original_filename=filename,
        file_path=stored.rel_path,
        mime=mime,
        size_bytes=stored.size,
        content_hash=gate.sha256,
        version=next_version(session, meta["dept_id"], meta["title"]),
        effective_date=meta["effective_date"],
        expiry_date=meta.get("expiry_date"),
        owner_name=meta["owner_name"],
        status="parsing",
        is_active=False,  # parsing 期不生效：不占 uq_doc_active，检索不露半成品
        uploaded_by=uploader.id,
    )
    session.add(doc)
    session.flush()  # 先取 id 供审计外键
    detail = {"after": {"title": doc.title, "filename": filename, "sha256": gate.sha256}}
    audit_document(session, uploader.id, "document.upload", doc.id, detail)
    _commit_upload(session)
    return doc


def process_document(session: Session, deps: IngestDeps, doc_id: int) -> None:
    """异步段编排：先回 parsing（DOC-11 重试语义），失败转 failed+中文原因，成功收口 ready。

    进度：注入 progress 回调给分析管线，阶段推进即时落库（UI 30s 轮询可见真实进度）；
    收口（ready/failed）清 stage/progress——终态下进度无意义。"""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    doc.status = "parsing"
    doc.parse_stage = "排队中"
    doc.parse_progress = 0
    session.commit()  # 立即可见：轮询先看到"排队中"，进度随阶段推进
    started = deps.clock.now()

    def report(stage: str, percent: int) -> None:
        doc.parse_stage = stage
        doc.parse_progress = percent
        session.commit()

    parse_filename = doc.original_filename
    try:
        if PureWindowsPath(parse_filename).suffix.lower() in _LEGACY_EXTS:
            # 老格式前置转换（F-1015）：产物落盘替换解析输入，失败走同一兜底区
            # （编排细节与事故裁定见 ingest_convert.py 模块注释）。
            parse_filename = convert_legacy_document(deps, doc, parse_filename, report)
            session.commit()
        outcome = analyze_document(
            session, deps, doc, progress=report, parse_filename=parse_filename
        )
    except Exception as exc:
        logger.exception("解析失败 doc_id=%s title=%s", doc_id, doc.title)
        commit_failed(session, deps.clock, doc, exc)
        return
    commit_ready(session, deps, doc, outcome)
    # no_ground 种子重扫挂钩（工单 16）：补语料/重传入库成功后，同部门"待补语料"
    # 种子自动重新获得出题机会；失败不影响入库结果（审计与重扫计数独立）。
    try:
        from app.services.hardcase_mine import rescan_no_ground

        rescanned = rescan_no_ground(session, doc.dept_id)
        if rescanned:
            logger.info(
                "入库重扫：doc_id=%s dept=%s 转回 pending %s 颗种子", doc_id, doc.dept_id, rescanned
            )
    except Exception:
        logger.exception("入库重扫失败 doc_id=%s", doc_id)
    logger.info(
        "解析收口 doc_id=%s status=ready 引擎=%s 耗时=%.1fs",
        doc_id,
        outcome.engine,
        (deps.clock.now() - started).total_seconds(),
    )


def delete_document(session: Session, clock: Clock, doc_id: int, actor_user_id: int) -> None:
    """文档软删（F-305，2026-10-06 立项实施）：deleted_at+is_active=false + 级联停用其
    chunks（chunk.is_active=false——检索走 is_active 部分索引，软删即退出检索；向量与
    文本行保留便于追溯）。parsing 态 422：防与后台解析任务竞态（任务收口会覆写状态）。
    document_images 行保留（回显走文档存活闸，软删后 404）。审计留痕。"""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    if doc.status == "parsing":
        raise ApiError(ERR_PARAM_INVALID, "解析中的文档不可删除，请等待解析终态后再删除", 422)
    doc.is_active = False
    doc.deleted_at = clock.now()
    doc.updated_at = clock.now()
    # 单语句批量停用（findings I3）：不整实体加载 chunk（含 1024 维 embedding 大列）——
    # 几百块文档从"几百条 UPDATE + 数 MB 白载内存"降为一条 UPDATE。
    session.execute(update(Chunk).where(Chunk.document_id == doc.id).values(is_active=False))
    cleanup_previews(get_settings().storage_dir, doc.id)  # 预览缓存随档清理（best-effort）
    audit_document(
        session,
        actor_user_id,
        "document.delete",
        doc.id,
        {
            "before": {"title": doc.title, "isActive": True},
            "after": {"isActive": False, "deleted": True},
        },
        clock=clock,
    )
    session.commit()
    return doc


def confirm_document(session: Session, doc_id: int, actor_user_id: int) -> Document:
    """待确认激活：管理员确认敏感/近似文档 → is_active=True，审计并提交（条件 UPDATE
    判 rowcount：仅 is_active=False 可激活，B3 TOCTOU 防线——双管理员并发确认恰一次
    生效、审计恰一条；重复确认 422/1003 不再静默成功）。

    actor_user_id 必填（不回退 uploaded_by）：审计操作者必须是真实点击确认的管理员
    （T13 W2 移交项）——上传者与确认者常为两人，默认 uploaded_by 会伪造留痕。
    """
    updated = session.execute(
        update(Document)
        .where(Document.id == doc_id, Document.is_active.is_(False))
        .values(is_active=True)
    )
    if updated.rowcount == 0:  # 0 行定性：不存在 404；已生效（对手已确认）422
        doc = session.get(Document, doc_id)
        if doc is None:
            raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
        raise ApiError(ERR_PARAM_INVALID, "文档已是生效状态，无需重复确认", 422)
    audit_document(
        session,
        actor_user_id,
        "document.confirm",
        doc_id,
        {"before": {"isActive": False}, "after": {"isActive": True}},
    )
    session.commit()
    doc = session.get(Document, doc_id)
    if doc is None:  # 理论不可达（迁移刚成功）；防御读优于 assert（-O 不裁语义）
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    session.refresh(doc)  # 语句级写入的会话镜像不保证同步：回读库内真相再回传
    return doc


def expire_due_documents(session: Session, clock: Clock) -> int:
    """F-1004 到期下线：expiry_date < 时钟当日的生效未删文档逐个下线并审计，返回条数。"""
    today = clock.now().date()
    dues = list(
        session.scalars(
            select(Document).where(
                Document.expiry_date.is_not(None),
                Document.expiry_date < today,
                Document.is_active.is_(True),
                Document.deleted_at.is_(None),
            )
        )
    )
    for doc in dues:
        doc.is_active = False
        doc.updated_at = clock.now()
        audit_document(
            session,
            doc.uploaded_by,
            "document.expire",
            doc.id,
            {"after": {"isActive": False}, "expiryDate": doc.expiry_date.isoformat()},
            clock=clock,
        )
    session.commit()
    return len(dues)


def sync_reject_reason(reason: str | None) -> str:
    """check_sync 失败码 → 面向用户的中文原因：upload 的 422 消息与 zip 批量导入的
    条目级 rejected 原因共用本映射（单一事实来源，禁止两处各写一份文案）。"""
    return _SYNC_REJECTS.get(reason or "", "文件未通过同步预检")


def sync_rejected(reason: str | None) -> ApiError:
    """check_sync 失败码 → ApiError：超限走 1003，其余格式类走 1001，一律 422。
    公有化（原 _sync_rejected）：草稿创建（draft_review）与上传共用同一映射。"""
    code = ERR_PARAM_INVALID if reason == "too_large" else ERR_UNSUPPORTED_FORMAT
    return ApiError(code, sync_reject_reason(reason), 422)


def reject_duplicate(session: Session, sha256: str, *, hint: str = "如需更新请上传新版本") -> None:
    """F-1001 精确查重：命中未软删的同 hash 文档即 409，消息指向已有 id（并发由唯一索引兜底）。
    hint 随场景措辞（upload/draft_review），判定口径单一来源。"""
    existing = session.scalars(
        select(Document).where(Document.content_hash == sha256, Document.deleted_at.is_(None))
    ).first()
    if existing is not None:
        raise ApiError(
            ERR_DUPLICATE_DOC,
            f"文件与已有文档完全重复（已有文档 ID {existing.id}），{hint}",
            409,
        )


def next_version(session: Session, dept_id: int, title: str) -> int:
    """同部门同标题最大版本号 +1：不过滤软删——uq_doc_title_dept_ver 对全部行生效，
    软删行仍占其版本号，新行必须越过它（部分索引只豁免 content_hash 查重）。
    公有化（原 _next_version）：草稿登记与上传共用同部门同标题的版本序。"""
    current = session.scalar(
        select(func.max(Document.version)).where(
            Document.dept_id == dept_id, Document.title == title
        )
    )
    return (current or 0) + 1


def _commit_upload(session: Session) -> None:
    """上传事务提交段：IntegrityError = 并发抢注同 content_hash（DOC-06），回滚并转 409。

    同步查重（reject_duplicate）只挡提交前可见的重复；查重通过后、提交前对手抢注
    同 content_hash 的并发半边，由提交段撞 uq_doc_content_hash 唯一索引在此兜底。
    只捕 IntegrityError 一族：其余异常（UpstreamError 等）绝不吞。"""
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()  # 丢弃半程写入（文档行+审计）：冲突即失败，不留痕迹
        raise ApiError(ERR_DUPLICATE_DOC, "文件与已有文档重复，请勿重复上传", 409) from exc
