r"""老格式前置转换编排（F-1015，自 ingest.process_document 拆出，行数红线 §1.4）。

doc/ppt/xls → PDF：Gotenberg libreoffice 路由只输出 PDF（2026-10-05 容器实测；曾误设
OOXML 目标，PDF 字节按 .docx 落盘解析必败）。PDF 产物落盘替换解析输入 → analyze_document
按新扩展名走 pdf 链（文字版 pypdf 快速通道/扫描件 MinerU），同份 PDF 即预览端点直通
来源；original_filename 保留真实来源。失败（含转换服务未配置）向上抛，由 ingest 状态机
统一落 failed+中文原因——转换与解析同属一个失败兜底区。
"""

from collections.abc import Callable
from pathlib import Path as FsPath
from pathlib import PureWindowsPath

from app.core.convert import raise_convert_unavailable
from app.db.models import Document
from app.services.ingest_pipeline import IngestDeps


def convert_legacy_document(
    deps: IngestDeps,
    doc: Document,
    parse_filename: str,
    report: Callable[[str, int], None],
) -> str:
    """老格式（doc/ppt/xls）前置转换：产物落盘并回写 doc.file_path，返回新解析文件名。

    report 用于"转换中"进度上报（回调异常向上抛：落库失败=解析失败，同管线口径）。"""
    report("转换中", 3)
    if deps.converter is None:
        raise_convert_unavailable()
    raw = (FsPath(deps.parser_settings.storage_dir) / doc.file_path).read_bytes()
    new_content, new_name = deps.converter.convert_to_pdf(parse_filename, raw)
    stored = deps.storage.save(new_content, PureWindowsPath(new_name).suffix.lower())
    doc.file_path = stored.rel_path
    return new_name
