r"""zip 批量导入（F-1007，治理"傻瓜式原则"）：安全预扫描 → 逐条目元数据解析与同步
预检 → 走单文件上传同一套库内路径入库。

安全设计（§6.3）：先做**预扫描**——流式读全部条目并计数，总量 ≤ MAX_TOTAL_BYTES、
条目数 ≤ MAX_ENTRIES、单条目 ≤ MAX_ENTRY_BYTES、嵌套 zip（条目名 .zip 结尾）与加密
条目整包拒收；任一超限读到即中止（422）。预扫描阶段不落盘、不写库，"边解压边计数
超限即中止并清理临时文件"由此被设计消解：中止时刻没有任何已写产物可残留（已入库行
无需回滚，比边导入边清理更安全）。单条目白名单复用 check_sync：zip 条目没有 HTTP
头，Content-Type 闸不适用——MIME 以扩展名规范值补齐仅为复用同一代码路径，真实闸门
= 扩展名 + 魔数双闸（docx 压缩弹与 pdf 危险声明仍由解析器处置，见 precheck docstring）。

文件名元数据（F-1007）：`部门_主题_日期.ext`，部门限库内枚举（试点 service/hr/all）、
日期白名单 YYYYMMDD / YYYY-MM-DD（严格位数 + 真实日期）、责任人=上传管理员（命名
规则无责任人字段，管理员批量操作兜底）；命名不符 → "元数据待补"不入库（该条
skipped_metadata）。合规确认（§7.4）：zip 导入是管理端管理员的显式批量操作，视同
已勾选合规确认，与单文件上传的表单勾选同口径。

并发=1：条目解析任务经 BackgroundTasks 依添加顺序串行执行（starlette 语义，每条
任务独立 SessionLocal 同步跑完再跑下一条），与单文件上传共用既有编排，无额外并发
机制。审计 document.import_zip {total, accepted, rejected, skipped}，其中 skipped
= duplicate + skipped_metadata（未入库且非白名单拒绝），total = accepted + rejected
+ skipped。
"""

import re
import zipfile
from collections.abc import Callable
from datetime import date
from pathlib import PureWindowsPath
from typing import BinaryIO, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ERR_PARAM_INVALID, ApiError
from app.core.storage import FileStorage
from app.db.models import Department, Document, User
from app.rag.precheck import MAX_SYNC_BYTES, check_sync
from app.services import ingest as ingest_service
from app.services.audit import record_audit

MAX_TOTAL_BYTES: int = 500 * 1024 * 1024  # §6.3 解压产物总量上限
MAX_ENTRIES: int = 100_000  # §6.3 条目数上限
MAX_ENTRY_BYTES: int = MAX_SYNC_BYTES  # 单条目与单文件上传同源 50MB
_READ_CHUNK: int = 1 << 20  # 流式计数块大小（内存有界，防压缩炸弹一次展开）
# zip 条目无 HTTP Content-Type：以扩展名规范值补齐仅供 check_sync 走单一路径
_MIME_BY_EXT: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_MSG_BAD_ZIP = "文件不是有效的 zip 压缩包"
_MSG_SKIPPED_METADATA = (
    "元数据待补：文件名须为 部门_主题_日期 格式"
    "（部门编码限库内枚举 service/hr/all，日期 YYYYMMDD 或 YYYY-MM-DD）"
)

# 日期白名单：先严格位数（拒 2026-9-1 这类缺前导零），再由 _parse_date 验真实日期
_DATE_PATTERNS = (re.compile(r"[0-9]{8}"), re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"))


class EntryResult(NamedTuple):
    """逐文件报告条目：status ∈ accepted/duplicate/rejected/skipped_metadata，
    documentId 仅 accepted/duplicate 有值，reason 仅非 accepted 有值。"""

    filename: str
    status: str
    document_id: int | None
    reason: str | None


def import_zip(
    session: Session,
    storage: FileStorage,
    uploader: User,
    archive: BinaryIO,
    on_accepted: Callable[[int], None],
) -> dict:
    """F-1007 主入口：预扫描 → 逐条目导入（每条独立事务提交，批量部分成功语义）
    → 审计 → 组装 {results, accepted, rejected, skipped}。

    on_accepted 在每条 accepted 后收到文档 id：api 层用它挂后台解析任务
    （test_mode 下为同步解析回调），本服务不感知调度方式。
    """
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise ApiError(ERR_PARAM_INVALID, _MSG_BAD_ZIP, 422) from exc
    with zf:
        entries, oversized = _scan_archive(zf)
        dept_ids = _dept_id_by_code(session)
        results = [
            _import_entry(session, storage, uploader, zf, info, dept_ids, oversized, on_accepted)
            for info in entries
        ]
    summary = _summarize(results)
    record_audit(
        session,
        uploader.id,
        "document.import_zip",
        "document",
        None,
        {
            "total": len(results),
            "accepted": summary["accepted"],
            "rejected": summary["rejected"],
            "skipped": summary["skipped"],
        },
    )
    session.commit()
    return summary


def _scan_archive(zf: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], frozenset[str]]:
    """§6.3 安全预扫描：条目数 / 嵌套 zip / 加密条目 / 单条目与总量字节全在此把关。

    流式逐条计数实际解压字节（file_size 可被伪造，不做信任依据）。单条目超限只标记
    不中止（该条稍后条目级 rejected，包继续）；总量超限、条目数超限、嵌套 zip、加密
    条目整包中止。返回（可导入条目, 超限条目名集合）。
    """
    infos = [info for info in zf.infolist() if not info.is_dir()]
    if len(infos) > MAX_ENTRIES:
        raise ApiError(
            ERR_PARAM_INVALID, f"压缩包含 {len(infos)} 个条目，超过 {MAX_ENTRIES} 上限", 422
        )
    for info in infos:
        if PureWindowsPath(info.filename).suffix.lower() == ".zip":
            raise ApiError(ERR_PARAM_INVALID, f"压缩包含嵌套 zip（{info.filename}），整包拒收", 422)
        if info.flag_bits & 0x1:  # 加密条目无法计数，字节预算无法保证 → 整包拒收
            raise ApiError(
                ERR_PARAM_INVALID, f"压缩包含加密条目（{info.filename}），请解密后再导入", 422
            )
    oversized: set[str] = set()
    total = 0
    for info in infos:
        size, over = _stream_count(zf, info)
        total += size
        if over:
            oversized.add(info.filename)
        if total > MAX_TOTAL_BYTES:
            raise ApiError(ERR_PARAM_INVALID, "解压产物总量超过 500MB 上限，已中止导入", 422)
    return infos, frozenset(oversized)


def _stream_count(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, bool]:
    """流式读单条目计数实际解压字节，超单条目上限即停止读（防炸弹拖垮内存与耗时）。

    返回（已读字节, 是否超单条目上限）；超限条目尾部不计入总量——该条目必被拒收
    不会入库，不占 §6.3 的 500MB 预算（预算按将入库产物计）。
    """
    count = 0
    with zf.open(info) as stream:
        while chunk := stream.read(_READ_CHUNK):
            count += len(chunk)
            if count > MAX_ENTRY_BYTES:
                return count, True
    return count, False


def _import_entry(
    session: Session,
    storage: FileStorage,
    uploader: User,
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    dept_ids: dict[str, int],
    oversized: frozenset[str],
    on_accepted: Callable[[int], None],
) -> EntryResult:
    """单条目入库：元数据解析 → 预检白名单 → 查重 → create_document（同一库内路径）。

    命名不符不入库（skipped_metadata）；白名单/大小不符条目级 rejected；完全重复
    duplicate 指向已有文档；其余走 create_document 落库并回调 on_accepted。
    """
    filename = info.filename
    if filename in oversized:
        return EntryResult(
            filename, "rejected", None, ingest_service.sync_reject_reason("too_large")
        )
    meta = parse_entry_meta(filename, dept_ids, uploader.username)
    if meta is None:
        return EntryResult(filename, "skipped_metadata", None, _MSG_SKIPPED_METADATA)
    ext = PureWindowsPath(filename).suffix.lower()
    content = zf.read(info)  # 预扫描已确认 ≤ 单条目上限，读入内存安全
    gate = check_sync(filename, _MIME_BY_EXT.get(ext, ""), content)
    if not gate.ok:
        return EntryResult(
            filename, "rejected", None, ingest_service.sync_reject_reason(gate.reason)
        )
    existing = _find_duplicate(session, gate.sha256)
    if existing is not None:
        return EntryResult(
            filename,
            "duplicate",
            existing.id,
            f"文件与已有文档完全重复（已有文档 ID {existing.id}），如需更新请上传新版本",
        )
    try:
        doc = ingest_service.create_document(
            session, storage, uploader, meta, content, filename, _MIME_BY_EXT[ext]
        )
    except ApiError as exc:  # 同会话已预检：此处只兜并发抢注等残余冲突，条目级拒绝不拖垮整包
        return EntryResult(filename, "rejected", None, exc.message)
    on_accepted(doc.id)
    return EntryResult(filename, "accepted", doc.id, None)


def parse_entry_meta(filename: str, dept_ids: dict[str, int], owner: str) -> dict | None:
    """F-1007 文件名元数据解析：`部门_主题_日期.ext` → create_document 元数据包。

    自两端向中间拆解（rsplit 日期 / split 部门），主题可含下划线；部门限库内枚举
    （试点 service/hr/all）；日期白名单 YYYYMMDD / YYYY-MM-DD（严格位数 + 真实日期）；
    责任人=上传管理员（命名规则无责任人字段，管理员批量操作兜底，§7.4 视同合规
    已确认）；expiry_date 不可推导保持空。任一段不符返回 None → skipped_metadata。
    """
    stem = PureWindowsPath(filename).stem
    left, _, date_token = stem.rpartition("_")
    if not left or not _valid_date_token(date_token):
        return None
    dept_code, sep, title = left.partition("_")
    if not sep or not title or dept_code not in dept_ids:
        return None
    return {
        "dept_id": dept_ids[dept_code],
        "title": title,
        "owner_name": owner,
        "effective_date": _parse_date(date_token),
    }


def _valid_date_token(token: str) -> bool:
    """日期白名单两道闸：严格位数正则（拒 2026-9-1 缺前导零）+ 真实存在的日期。"""
    return (
        any(pattern.fullmatch(token) for pattern in _DATE_PATTERNS)
        and _parse_date(token) is not None
    )


def _parse_date(token: str) -> date | None:
    """白名单格式解析为日期（YYYYMMDD / YYYY-MM-DD），非法日期（如 20261332）返回 None。"""
    try:
        if len(token) == 8:
            return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
        return date(int(token[:4]), int(token[5:7]), int(token[8:10]))
    except ValueError:
        return None


def _find_duplicate(session: Session, sha256: str) -> Document | None:
    """库内精确查重（同 create_document 的 _reject_duplicate 口径）：软删行不占 hash。"""
    return session.scalars(
        select(Document).where(Document.content_hash == sha256, Document.deleted_at.is_(None))
    ).first()


def _dept_id_by_code(session: Session) -> dict[str, int]:
    """部门枚举取库表（种子 service/hr/all）：单一事实来源，枚举校验与 dept_id 解析一步完成。"""
    return dict(session.execute(select(Department.code, Department.id)).all())


def _summarize(results: list[EntryResult]) -> dict:
    """逐文件报告 → 响应体：skipped = duplicate + skipped_metadata（未入库且非白名单拒绝）。"""
    accepted = sum(1 for r in results if r.status == "accepted")
    rejected = sum(1 for r in results if r.status == "rejected")
    return {
        "results": [
            {
                "filename": r.filename,
                "status": r.status,
                **({"documentId": r.document_id} if r.document_id is not None else {}),
                **({"reason": r.reason} if r.reason is not None else {}),
            }
            for r in results
        ],
        "accepted": accepted,
        "rejected": rejected,
        "skipped": len(results) - accepted - rejected,
    }
