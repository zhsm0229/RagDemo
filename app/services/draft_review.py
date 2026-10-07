r"""草稿审核状态机（agent-evolution #05 数据面）：draft 创建 → 待审核 → 已通过/已驳回。

红线（docs/specs/agent-evolution.md §五）：草稿绝不直接生效——LLM 产出有编造风险，
agent 场景的红线 = 无审核不落地。草稿创建只落盘+登记：status='pending' 且
is_active=False 且无 chunks 行，检索侧"仅 ready 且 is_active 文档"过滤
（services/retrieval.py 强制 JOIN documents）天然不命中——未审核草稿不可进入检索库，
该红线不依赖本模块新列，读回断言见 tests/integration/test_draft_review.py。

审核通过后由 api 层触发既有入库管道（process_document：解析/敏感扫描/近似查重/分块/
嵌入全走，无旁路），驳回则留原因归档、永不入库。创建期预检三闸（check_sync）与精确
查重与上传同源（复用 ingest 公有助手，单一事实来源）；落盘一律走装配层注入的
FileStorage（build_storage，findings I5：实现类只在装配层出现）。三个审核动作均经
audit_document 留痕并与业务写入同事务提交。
"""

from pathlib import PureWindowsPath

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.errors import ERR_NOT_FOUND, ERR_PARAM_INVALID, ApiError
from app.core.storage import FileStorage
from app.db.models import Document, User
from app.rag.precheck import check_sync as check_sync_gate
from app.services.ingest import next_version, reject_duplicate, sync_rejected
from app.services.ingest_commit import audit_document

# 审核三态（与 0007 迁移 CHECK 同源；中文状态值即产品文案，禁魔法字符串散落）
REVIEW_PENDING = "待审核"
REVIEW_APPROVED = "已通过"
REVIEW_REJECTED = "已驳回"
ALLOWED_REVIEW_STATUS: tuple[str, ...] = (REVIEW_PENDING, REVIEW_APPROVED, REVIEW_REJECTED)

# 草稿登记元数据必填项（同 ingest.create_document 的 meta 口径：草稿是待入库文档的候选，
# 生效日期/责任人等治理字段在创建期就必须齐备，审核人据此判断可否入库）
_META_REQUIRED: tuple[str, ...] = ("dept_id", "title", "owner_name", "effective_date")

_MSG_NOT_FOUND = "文档不存在"
_MSG_NOT_DRAFT = "仅 Agent 生成的草稿可以执行审核操作"
_MSG_NOT_PENDING = "仅待审核状态的草稿可以审核"
_MSG_REASON_REQUIRED = "驳回必须填写原因"


def create_draft(
    session: Session,
    storage: FileStorage,
    author: User,
    meta: dict,
    content: bytes,
    filename: str,
    mime: str,
) -> Document:
    """Agent 产物落为草稿：预检三闸/精确查重与上传同源 → FileStorage 落盘 →
    登记 draft+待审核（不触发解析，入库必须等人工审核通过）→ 审计并提交。

    查重 hint 与上传区分措辞（草稿语义下"上传新版本"不成立），判定口径单一来源
    （ingest.reject_duplicate）。file_path 复用文档列：审核通过后解析管道直接从
    该路径取件，不另设草稿存储区。"""
    missing = [key for key in _META_REQUIRED if not meta.get(key)]
    if missing:
        raise ApiError(ERR_PARAM_INVALID, f"元数据缺失：{'、'.join(missing)}", 422)
    gate = check_sync_gate(filename, mime, content)
    if not gate.ok:
        raise sync_rejected(gate.reason)
    reject_duplicate(session, gate.sha256, hint="无需重复入库")
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
        status="pending",
        is_active=False,  # 草稿不生效：不占 uq_doc_active，检索不命中（红线）
        source_type="draft",
        review_status=REVIEW_PENDING,
        uploaded_by=author.id,
    )
    session.add(doc)
    session.flush()  # 先取 id 供审计外键
    audit_document(
        session,
        author.id,
        "document.draft_submit",
        doc.id,
        {"after": {"title": doc.title, "filename": filename, "sha256": gate.sha256}},
    )
    session.commit()
    return doc


def approve_draft(session: Session, clock: Clock, doc_id: int, actor_user_id: int) -> Document:
    """审核通过：待审核 → 已通过，审计并提交；入库管道由 api 层在提交后触发
    （test_mode 同步 process_document，生产挂 BackgroundTasks——与 retry 同一编排），
    本函数不碰 status/is_active：交给管道收口，杜绝绕过敏感扫描/查重的旁路。"""
    doc = _pending_draft(session, doc_id)
    doc.review_status = REVIEW_APPROVED
    doc.updated_at = clock.now()
    audit_document(
        session,
        actor_user_id,
        "document.draft_approve",
        doc.id,
        {"before": {"reviewStatus": REVIEW_PENDING}, "after": {"reviewStatus": REVIEW_APPROVED}},
        clock=clock,
    )
    session.commit()
    return doc


def reject_draft(
    session: Session, clock: Clock, doc_id: int, actor_user_id: int, reason: str
) -> Document:
    """驳回：待审核 → 已驳回 + 原因留档（reject_reason），审计并提交。
    驳回后永不触发解析：status 停在 pending，检索侧天然不命中；文件与行保留供追溯。"""
    reason = (reason or "").strip()
    if not reason:
        raise ApiError(ERR_PARAM_INVALID, _MSG_REASON_REQUIRED, 422)
    doc = _pending_draft(session, doc_id)
    doc.review_status = REVIEW_REJECTED
    doc.reject_reason = reason
    doc.updated_at = clock.now()
    audit_document(
        session,
        actor_user_id,
        "document.draft_reject",
        doc.id,
        {
            "before": {"reviewStatus": REVIEW_PENDING},
            "after": {"reviewStatus": REVIEW_REJECTED, "reason": reason},
        },
        clock=clock,
    )
    session.commit()
    return doc


def _pending_draft(session: Session, doc_id: int) -> Document:
    """寻址 + 状态机前置校验：不存在 404/3003（I13 同判口径）；非草稿或非待审核
    422/1003——重复审核、对 upload 语料审核都是非法迁移，fail-fast 不给幂等假象。"""
    doc = session.get(Document, doc_id)
    if doc is None:
        raise ApiError(ERR_NOT_FOUND, _MSG_NOT_FOUND, 404)
    if doc.source_type != "draft":
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_DRAFT, 422)
    if doc.review_status != REVIEW_PENDING:
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_PENDING, 422)
    return doc
