r"""解析器：Parser 接口 + DocxParser（python-docx）+ PdfFallbackParser（pypdf 降级）+
MinerUParser（HTTP 客户端）+ get_parser 调度器（standards §10.2 解析器选择树）。

安全边界（standards §6.1/§6.3）：docx zip 条目白名单（其余跳过）；包总量声明
≤500MB、条目 ≤10 万；单条目 MAX_ENTRY_XML_BYTES 实读上限 + DOCTYPE/ENTITY 流式
重叠窗口字节扫描（防线实现在 rag/xmlguard.py）；页数/输出字符数上限。

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
from app.rag.xmlguard import scan_entry

MAX_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024  # §6.3：包声明解压总量上限（防解压炸弹）
MAX_ENTRIES: int = 100_000  # §6.3：docx 压缩包条目数上限
MAX_ENTRY_XML_BYTES: int = 64 * 1024 * 1024  # 单个自读条目的声明/实读上限（防内存放大 OOM）
MAX_PAGES: int = 2000  # §6.3：pdf 页数上限
MAX_OUTPUT_CHARS: int = 2_000_000  # §6.3：解析输出字符数上限
# 全链路分页约定（task-6b）：pdf 页间以 \f 相接；precheck.page_hint 按其计页（敏感
# 扫描定位），_page_blocks 按其切页为 Block.page 打页码（引用定位）——两处并存互指。
PDF_PAGE_SEPARATOR: str = "\f"

_CONTENT_TYPES: str = "[Content_Types].xml"
_ALLOWED_PREFIXES: tuple[str, ...] = ("word/", "docProps/", "_rels/")
# 非法包体底层异常族：加密条目(RuntimeError)、压缩方法不支持(NotImplementedError)、
# lxml 语法错误(⊂SyntaxError)、缺部件(KeyError/ValueError)、残缺包属性访问(AttributeError)
_DOCX_PARSE_ERRORS: tuple[type[Exception], ...] = (
    KeyError,
    ValueError,
    RuntimeError,
    NotImplementedError,
    SyntaxError,
    AttributeError,
)


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


def _page_blocks(stem: str, text: str) -> list[Block]:
    r"""text→分页 Block 的唯一转换点：按 PDF_PAGE_SEPARATOR（\f）切页，逐页打页码（1 起）。

    与 precheck.page_hint 互为表里：page_hint 数原文 \f 计页（敏感扫描定位），
    本函数按同一约定为 Block.page 打页码（引用定位），两条线并存不冲突（task-6b）。"""
    return [
        Block(level=1, heading_path=[stem], text=page_text, page=number)
        for number, page_text in enumerate(text.split(PDF_PAGE_SEPARATOR), start=1)
    ]


class DocxParser:
    """python-docx 进程内解析：标题层级 + 正文 + 表格，产出 heading_path 全路径的标题节。

    白名单外条目跳过不读；包体非法/超限/加密/含危险声明一律 ApiError 中文原因。"""

    engine: str = "docx"
    degraded: bool = False

    def __init__(
        self,
        max_uncompressed_bytes: int = MAX_UNCOMPRESSED_BYTES,
        max_entries: int = MAX_ENTRIES,
        max_entry_xml_bytes: int = MAX_ENTRY_XML_BYTES,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        self.max_uncompressed_bytes: int = max_uncompressed_bytes
        self.max_entries: int = max_entries
        self.max_entry_xml_bytes: int = max_entry_xml_bytes
        self.max_output_chars: int = max_output_chars

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        """包体校验（§6.1/§6.3）→ python-docx 解析 → 标题节序列；标题取元数据，缺省用文件名。"""
        try:
            self._validate_package(content)
        except zipfile.BadZipFile as exc:
            raise ApiError(ERR_PARSE_FAILED, "文件不是有效的 docx 压缩包", 422) from exc
        try:
            document = Document(io.BytesIO(content))
            blocks = self._collect(document)
            title = document.core_properties.title or _stem(filename)
        except _DOCX_PARSE_ERRORS as exc:  # 含加密条目/压缩方法不支持/lxml 语法错误
            raise ApiError(ERR_PARSE_FAILED, "docx 解析失败：包体结构非法或已加密", 422) from exc
        _check_output(sum(len(block.text) for block in blocks), self.max_output_chars)
        return ParsedDoc(title=title, blocks=blocks)

    def _validate_package(self, content: bytes) -> None:
        """闸门按序短路：条目数 → 声明解压总量 → OPC 标记 → 逐白名单条目（声明上限 + 流式扫描）。"""
        archive = zipfile.ZipFile(io.BytesIO(content))
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
            try:
                for info in infos:
                    if _allowed_entry(info.filename):
                        scan_entry(archive, info.filename, info.file_size, self.max_entry_xml_bytes)
            except (
                _DOCX_PARSE_ERRORS
            ) as exc:  # 条目加密(RuntimeError)/压缩方法不支持(NotImplementedError)
                raise ApiError(
                    ERR_PARSE_FAILED, "docx 解析失败：条目不可读或压缩方法不支持", 422
                ) from exc

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
                    path = [*path[: level - 1], item.text.strip()]  # 跳级截断回退，不补空占位
                    current = Block(level=level, heading_path=list(path), text="")
                    blocks.append(current)
                else:
                    append(item.text.strip())
            else:
                append(_table_text(item))
        return blocks


class PdfFallbackParser:
    """pypdf 纯文本降级解析：逐页提取，按 \\f 分页约定为每页 Block 打页码（1 起）。

    魔数≠内容安全：本类只做文本提取，PDF 内嵌 JS/OpenAction 的处置在 W2 生成链路复核。
    """

    engine: str = "pypdf"
    degraded: bool = True  # MinerU 不可用时的降级路径，入库状态机据此注明"降级解析"

    def __init__(
        self, max_pages: int = MAX_PAGES, max_output_chars: int = MAX_OUTPUT_CHARS
    ) -> None:
        self.max_pages: int = max_pages
        self.max_output_chars: int = max_output_chars

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        """逐页提取后经 _page_blocks 产出带页码的标题节块（PDF 无标题层级，标题取文件名主干）。"""
        try:
            reader = PdfReader(io.BytesIO(content))
            if len(reader.pages) > self.max_pages:  # §6.3 页数上限，先于逐页提取
                raise ApiError(ERR_PARSE_FAILED, "pdf 页数超过上限", 422)
            pages = [page.extract_text() or "" for page in reader.pages]
        except (PyPdfError, ValueError, KeyError) as exc:  # 损坏/加密/截断等一律契约 422
            raise ApiError(ERR_PARSE_FAILED, "pdf 解析失败：文档损坏或已加密", 422) from exc
        text = PDF_PAGE_SEPARATOR.join(pages)
        _check_output(len(text), self.max_output_chars)
        stem = _stem(filename)
        return ParsedDoc(title=stem, blocks=_page_blocks(stem, text))


class MinerUParser:
    """MinerU 解析服务 HTTP 客户端（版面分析/表格/OCR）。

    契约：POST {base_url}/file_parse（multipart 字段 file），JSON 响应
    {"content": str, "pages": int?}（pages 可选，供 §6.3 页数闸）；网络/超时/非 2xx
    重试 1 次后抛 UpstreamError。W2 对接真实部署时契约如有出入只改本类。"""

    engine: str = "mineru"
    degraded: bool = False

    def __init__(
        self,
        base_url: str,
        timeout_s: int = 300,
        transport: httpx.BaseTransport | None = None,
        max_pages: int = MAX_PAGES,
        max_output_chars: int = MAX_OUTPUT_CHARS,
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.timeout_s: int = timeout_s
        self.max_pages: int = max_pages
        self.max_output_chars: int = max_output_chars
        self._transport: httpx.BaseTransport | None = transport  # 测试注入 httpx.MockTransport

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        """上传字节取回文本为单块 ParsedDoc；响应须为 {"content": str, "pages": int?}。

        content 无 \\f 分页 → 单块 Block.page=None 不崩；真实部署的页码契约为 W1 移交项。"""
        with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
            response = self._post_with_retry(client, filename, content)
            try:
                payload = response.json()
            except ValueError as exc:  # 含 JSONDecodeError
                raise UpstreamError("mineru 响应不是合法 JSON") from exc
        text = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(text, str):  # 缺字段/非 str/非 dict 一律上游故障，不给半成品
            raise UpstreamError("mineru 响应缺少 content 字段")
        pages = payload.get("pages") if isinstance(payload, dict) else None
        if isinstance(pages, int) and pages > self.max_pages:  # §6.3 页数闸，响应未携带则跳过
            raise ApiError(ERR_PARSE_FAILED, "pdf 页数超过上限", 422)
        _check_output(len(text), self.max_output_chars)
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
            except httpx.HTTPError as exc:  # 含 ConnectError/TimeoutException/HTTPStatusError
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
