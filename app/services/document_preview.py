r"""文档在线预览服务：统一以 PDF 形态供前端 iframe 内嵌渲染（预览评审材料 2026-10-05）。

分格式策略（见 docs/specs/2026-10-05-document-preview-review.md §3）：
- pdf 直通；老格式（doc/ppt/xls）入库时已转 PDF（F-1015 裁定），同为直通；
- docx/pptx/xlsx 调 Gotenberg 转 PDF，产物按 content_hash 寻址缓存
  previews/{doc_id}/{hash}.pdf（布局仿 images/{doc_id}/ 约定）：内容寻址天然随
  内容失效，重试/重传产生新 hash 即新缓存；缓存键不用 updated_at（无 onupdate、
  靠服务层手动刷新，逐写路径确认成本高）；
- 文本类（md/txt/csv/html）不支持 PDF 预览——前端取 /file 原字节转义渲染，
  html 绝不服务端 inline（存储型 XSS 载体）。
"""

import logging
import shutil
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from app.core.assembly import build_converter
from app.core.errors import ERR_PARAM_INVALID, ERR_PARSE_FAILED, ERR_UNSUPPORTED_FORMAT, ApiError
from app.core.llm import UpstreamError

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.db.models import Document

logger = logging.getLogger(__name__)

_PREVIEW_CONVERT_EXTS: frozenset[str] = frozenset({".docx", ".pptx", ".xlsx"})
_MSG_NOT_FOUND = "文档不存在"


def resolve_preview_pdf(doc: "Document", settings: "Settings") -> tuple[Path, str]:
    """返回可 inline 的 (预览 PDF 路径, 下载文件名)，必要时转换并写缓存。

    抛错口径：原文缺失 404；文本类格式 422（前端本就不对该类调本端点）；转换服务
    未配置 422、转换失败 502——均中文文案，前端弹窗降级给"下载原文件"兜底。"""
    path = Path(settings.storage_dir) / doc.file_path
    if not path.is_file():
        raise ApiError(ERR_PARAM_INVALID, _MSG_NOT_FOUND, 404)
    filename = f"{PureWindowsPath(doc.original_filename).stem}.pdf"
    stored_ext = PureWindowsPath(doc.file_path).suffix.lower()
    if stored_ext in _PREVIEW_CONVERT_EXTS:
        return _converted_preview(doc, path, settings), filename
    if stored_ext != ".pdf":
        raise ApiError(ERR_UNSUPPORTED_FORMAT, "该文档为文本格式，请直接查看内容或下载原文件", 422)
    return path, filename


def cleanup_previews(storage_dir: str, doc_id: int) -> None:
    """删除文档时清理其预览缓存目录（best-effort：缓存不可达即垃圾，失败不阻断删除）。"""
    shutil.rmtree(Path(storage_dir) / "previews" / str(doc_id), ignore_errors=True)


def _converted_preview(doc: "Document", path: Path, settings: "Settings") -> Path:
    """docx/pptx/xlsx → PDF：命中缓存直回；未命中转换后 xb 独占写（并发首预览防交叉写坏）。"""
    # converter 一律经装配工厂取得（findings I5）；未配置（convert_base_url 空）→
    # None → 422 中文降级，判定前置在缓存查找之前：服务未配置时即便有历史缓存也不放行
    converter = build_converter(settings)
    if converter is None:
        raise ApiError(ERR_UNSUPPORTED_FORMAT, "在线预览服务未配置，请下载原文件查看", 422)
    cache_dir = Path(settings.storage_dir) / "previews" / str(doc.id)
    cache_path = cache_dir / f"{doc.content_hash}.pdf"
    if cache_path.is_file():
        return cache_path
    try:
        pdf_bytes, _ = converter.convert_to_pdf(doc.original_filename, path.read_bytes())
    except UpstreamError as exc:
        logger.warning("预览转换失败 doc_id=%s: %s", doc.id, exc)
        raise ApiError(ERR_PARSE_FAILED, "在线预览转换失败，请下载原文件查看", 502) from exc
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        with cache_path.open("xb") as fh:
            fh.write(pdf_bytes)
    except FileExistsError:  # 并发对手已落盘，内容同源（同一 content_hash），直接读它的
        pass
    return cache_path
