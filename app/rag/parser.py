r"""解析器：Parser 接口 + DocxParser（python-docx）+ PdfFallbackParser（pypdf 降级）+
MinerUParser（HTTP 客户端）+ get_parser 调度器（standards §10.2 解析器选择树）。

安全边界（standards §6.1/§6.3，T8 深检移交）：docx 按 zip 白名单只接受
[Content_Types].xml、word/、docProps/、_rels/ 前缀条目，其余条目跳过；解压前按
声明尺寸校验总字节 ≤500MB、条目数 ≤10 万（防解压炸弹），自读 XML 再按实际读取
量限量（防谎报声明头）并拒绝含 DOCTYPE/ENTITY 声明的内容（§6.1）；docx 包体
XML 的常规解析交由 python-docx 成熟库。解析输出字符数设上限（§6.3）。

职责边界：解析器不做扩展名/MIME/魔数白名单——那是同步预检
（rag/precheck.check_sync）的闸门；解析器只管"给字节出文本"。
"""

import io
import zipfile
from pathlib import PureWindowsPath
from typing import Protocol, runtime_checkable

import httpx
from docx import Document
from docx.document import Document as DocumentObject
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.core.config import Settings
from app.core.errors import ERR_PARSE_FAILED, ERR_UNSUPPORTED_FORMAT, ApiError
from app.core.llm import UpstreamError
from app.rag.chunker import Block, ParsedDoc

MAX_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024  # §6.3：docx 解压产物总字节上限（防解压炸弹）
MAX_ENTRIES: int = 100_000  # §6.3：docx 压缩包条目数上限
MAX_OUTPUT_CHARS: int = 2_000_000  # §6.3：解析输出字符数上限
PDF_PAGE_SEPARATOR: str = "\f"  # 全链路分页约定：rag/precheck.page_hint 按其计页

_CONTENT_TYPES: str = "[Content_Types].xml"
_ALLOWED_PREFIXES: tuple[str, ...] = ("word/", "docProps/", "_rels/")
_XML_SUFFIXES: tuple[str, ...] = (".xml", ".rels")


@runtime_checkable
class Parser(Protocol):
    """解析接口：给字节出文本（ParsedDoc）；白名单闸门在同步预检，不在本接口。"""

    def parse(self, filename: str, content: bytes) -> ParsedDoc: ...


def _stem(filename: str) -> str:
    """文件名主干（容忍 Windows 路径分隔），作标题兜底与单块 heading_path 根。"""
    return PureWindowsPath(filename).stem


def _allowed_entry(name: str) -> bool:
    """docx zip 条目白名单：OPC 标记文件 + word/、docProps/、_rels/ 三前缀。"""
    return name == _CONTENT_TYPES or name.startswith(_ALLOWED_PREFIXES)


def _heading_level(paragraph: Paragraph) -> int | None:
    """内置标题样式 Heading N → 层级 N；非标题（含自定义样式）返回 None。"""
    name = paragraph.style.name
    if not name.startswith("Heading "):
        return None
    try:
        return int(name.removeprefix("Heading "))
    except ValueError:
        return None


def _table_text(table: Table) -> str:
    """表格 → 文本：行内单元格空格连接，行间换行。"""
    return "\n".join(" ".join(cell.text.strip() for cell in row.cells) for row in table.rows)


def _check_output(total_chars: int, cap: int) -> None:
    """解析输出字符数上限（§6.3）：超限拒收。"""
    if total_chars > cap:
        raise ApiError(ERR_PARSE_FAILED, "解析输出字符数超过上限", 422)


class DocxParser:
    """python-docx 进程内解析：标题层级 + 正文 + 表格，产出 heading_path 全路径的标题节。

    条目白名单外条目跳过不读；包体非法/超限/含危险声明一律 ApiError 中文原因。
    """

    engine: str = "docx"
    degraded: bool = False

    def __init__(
        self,
        max_uncompressed_bytes: int = MAX_UNCOMPRESSED_BYTES,
        max_entries: int = MAX_ENTRIES,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        self.max_uncompressed_bytes: int = max_uncompressed_bytes
        self.max_entries: int = max_entries
        self.max_output_chars: int = max_output_chars

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        """包体校验（§6.1/§6.3）→ python-docx 解析 → 标题节序列；标题取元数据，缺省用文件名。"""
        self._validate_package(content)
        try:
            document = Document(io.BytesIO(content))
        except (KeyError, ValueError) as exc:  # python-docx 对非法 OPC 包抛 KeyError/ValueError
            raise ApiError(ERR_PARSE_FAILED, "docx 解析失败：包体结构非法", 422) from exc
        blocks = self._collect(document)
        _check_output(sum(len(block.text) for block in blocks), self.max_output_chars)
        return ParsedDoc(title=document.core_properties.title or _stem(filename), blocks=blocks)

    def _validate_package(self, content: bytes) -> None:
        """闸门按序短路：zip 可开 → 条目数 → 声明解压总量 → OPC 标记 → 白名单内 XML 无 DOCTYPE/ENTITY。"""
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise ApiError(ERR_PARSE_FAILED, "文件不是有效的 docx 压缩包", 422) from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > self.max_entries:
                raise ApiError(ERR_PARSE_FAILED, "docx 压缩包条目数超过上限", 422)
            if sum(info.file_size for info in infos) > self.max_uncompressed_bytes:
                raise ApiError(ERR_PARSE_FAILED, "docx 解压后总字节超过 500MB 上限", 422)
            if _CONTENT_TYPES not in [info.filename for info in infos]:
                raise ApiError(
                    ERR_PARSE_FAILED, "缺少 [Content_Types].xml，不是有效的 docx 文档", 422
                )
            for info in infos:
                self._scan_xml(archive, info.filename)

    def _scan_xml(self, archive: zipfile.ZipFile, name: str) -> None:
        """自读 XML 的 §6.1 守规：白名单外条目跳过；.xml/.rels 实读限量并拒绝 DOCTYPE/ENTITY。"""
        if not _allowed_entry(name) or not name.endswith(_XML_SUFFIXES):
            return  # 白名单外条目一律跳过，不读不解析
        with archive.open(name) as fh:
            data = fh.read(self.max_uncompressed_bytes + 1)  # 实读限量，防谎报声明头
        if len(data) > self.max_uncompressed_bytes:
            raise ApiError(ERR_PARSE_FAILED, "docx 解压后总字节超过 500MB 上限", 422)
        lowered = data.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise ApiError(ERR_PARSE_FAILED, "docx 内含 DOCTYPE/ENTITY 声明，已拒绝解析", 422)

    def _collect(self, document: DocumentObject) -> list[Block]:
        """按文档序遍历段落与表格：标题开新节（heading_path 置根到当前节全路径），正文/表格并入当前节。"""
        blocks: list[Block] = []
        path: list[str] = []
        current: Block | None = None

        def append(text: str) -> None:
            nonlocal current
            if not text:
                return
            if current is None:  # 标题之前出现正文：归入无标题前导块（level=0，路径为空）
                current = Block(level=0, heading_path=[], text=text)
                blocks.append(current)
            else:
                current.text = f"{current.text}\n{text}" if current.text else text

        for item in document.iter_inner_content():
            if isinstance(item, Paragraph):
                level = _heading_level(item)
                if level is not None and item.text.strip():
                    path = [*path[: level - 1], item.text.strip()]
                    current = Block(level=level, heading_path=list(path), text="")
                    blocks.append(current)
                else:
                    append(item.text.strip())
            else:
                append(_table_text(item))
        return blocks


class PdfFallbackParser:
    """pypdf 纯文本降级解析：逐页提取，页间以 \\f 拼接（分页约定见模块 docstring）。

    魔数≠内容安全：本类只做文本提取，PDF 内嵌 JS/OpenAction 的处置在 W2 生成链路复核。
    """

    engine: str = "pypdf"
    degraded: bool = True  # MinerU 不可用时的降级路径，入库状态机据此注明"降级解析"

    def __init__(self, max_output_chars: int = MAX_OUTPUT_CHARS) -> None:
        self.max_output_chars: int = max_output_chars

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        """提取全部页面文本为单块 ParsedDoc（PDF 无标题层级，标题取文件名主干）。"""
        try:
            reader = PdfReader(io.BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
        except PyPdfError as exc:  # 损坏/加密（含缺解密依赖）统一拒收
            raise ApiError(ERR_PARSE_FAILED, "pdf 解析失败：文档损坏或已加密", 422) from exc
        text = PDF_PAGE_SEPARATOR.join(pages)
        _check_output(len(text), self.max_output_chars)
        stem = _stem(filename)
        return ParsedDoc(title=stem, blocks=[Block(level=1, heading_path=[stem], text=text)])


class MinerUParser:
    """MinerU 解析服务 HTTP 客户端（版面分析/表格/OCR）。

    契约：POST {base_url}/file_parse（multipart 字段 file），JSON 响应
    {"content": 文本}；网络/超时/非 2xx 重试 1 次后抛 UpstreamError。
    W2 对接真实部署时契约如有出入只改本类。
    """

    engine: str = "mineru"
    degraded: bool = False

    def __init__(
        self, base_url: str, timeout_s: int = 300, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.timeout_s: int = timeout_s
        self._transport: httpx.BaseTransport | None = transport  # 测试注入 httpx.MockTransport

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        """上传字节取回文本为单块 ParsedDoc（标题取文件名主干）；输出受字符上限约束。"""
        with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
            response = self._post_with_retry(client, filename, content)
            try:
                text: str = response.json()["content"]
            except (KeyError, ValueError) as exc:  # 缺字段或非 JSON 均按上游故障
                raise UpstreamError("mineru 响应缺少 content 字段") from exc
        _check_output(len(text), MAX_OUTPUT_CHARS)
        stem = _stem(filename)
        return ParsedDoc(title=stem, blocks=[Block(level=1, heading_path=[stem], text=text)])

    def _post_with_retry(
        self, client: httpx.Client, filename: str, content: bytes
    ) -> httpx.Response:
        """首次 + 重试 1 次（standards：外部调用失败重试 1 次）；网络/超时/非 2xx 皆算失败。"""
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = client.post(
                    f"{self.base_url}/file_parse", files={"file": (filename, content)}
                )
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_error = exc
        raise UpstreamError("mineru 解析服务调用失败（已重试 1 次）") from last_error


def get_parser(filename: str, settings: Settings) -> Parser:
    """解析器选择树（§10.2）：docx→python-docx；pdf→配置了 mineru 用 MinerU，否则 pypdf 降级；
    `.doc` 按 §10.1 拒收并提示另存。此处为格式路由，非内容白名单（闸门在同步预检）。"""
    ext = PureWindowsPath(filename).suffix.lower()
    if ext == ".docx":
        return DocxParser()
    if ext == ".pdf":
        if settings.mineru_base_url:
            return MinerUParser(settings.mineru_base_url, timeout_s=settings.mineru_timeout_s)
        return PdfFallbackParser()
    if ext == ".doc":
        raise ApiError(ERR_UNSUPPORTED_FORMAT, "请另存为 docx", 422)
    raise ApiError(ERR_UNSUPPORTED_FORMAT, "仅支持 pdf/docx 格式", 422)
