r"""解析器攻击/畸形包拒绝矩阵（审查修复回归）：全部契约化为 ApiError(1002, 中文原因, 422)。

覆盖：伪 docx（普通 zip 改名）、包级解压炸弹（全条目谎报声明 500MB+1）、单条目
声明 499MB 的内存放大攻击样本（过包总量闸、被单条目上限毫秒级拒）、白名单外
条目跳过、DOCTYPE/ENTITY（XXE 向量）、加密条目（zipfile RuntimeError）与未知压
缩法（NotImplementedError）的 422 契约、残缺 Types（pkgreader AttributeError）
与垃圾 document.xml（lxml XMLSyntaxError ⊂ SyntaxError）的 422 契约、畸形 pdf
（空/垃圾/加密）、条目数上限。
"""

import io
import zipfile
from typing import Any

import pytest
from pypdf import PdfWriter

from app.core.errors import ERR_PARSE_FAILED, ApiError
from app.rag.parser import MAX_ENTRIES, MAX_UNCOMPRESSED_BYTES, DocxParser, PdfFallbackParser

_FULL_TYPES = (
    '<?xml version="1.0"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_FULL_RELS = (
    '<?xml version="1.0"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="word/document.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
    "</Relationships>"
)


def _assert_rejected(exc_info: pytest.ExceptionInfo[ApiError], message: str) -> None:
    """统一断言：ApiError(ERR_PARSE_FAILED, message, 422) 三字段逐字（异常兜底契约）。"""
    error = exc_info.value
    assert (error.code, error.message, error.http_status) == (ERR_PARSE_FAILED, message, 422)


def _forge(
    data: bytes,
    size_of: dict[str, int] | None = None,
    *,
    encrypted: bool = False,
    method: int | None = None,
) -> bytes:
    """改写 zip 中央目录（毫秒级伪造畸形包）：指定条目声明字节 / 全局加密位 / 压缩方法。"""
    out = bytearray(data)
    idx = 0
    while (pos := out.find(b"PK\x01\x02", idx)) >= 0:
        if encrypted:
            out[pos + 8 : pos + 10] = (1).to_bytes(2, "little")
        if method is not None:
            out[pos + 10 : pos + 12] = method.to_bytes(2, "little")
        name_len = int.from_bytes(out[pos + 28 : pos + 30], "little")
        name = bytes(out[pos + 46 : pos + 46 + name_len]).decode()
        if size_of and name in size_of:
            out[pos + 24 : pos + 28] = size_of[name].to_bytes(4, "little")
        extra = int.from_bytes(out[pos + 30 : pos + 32], "little")
        comment = int.from_bytes(out[pos + 32 : pos + 34], "little")
        idx = pos + 46 + name_len + extra + comment
    if encrypted:  # 本地头通用位标记同步置位
        idx = 0
        while (pos := out.find(b"PK\x03\x04", idx)) >= 0:
            out[pos + 6 : pos + 8] = (1).to_bytes(2, "little")
            idx = pos + 4
    return bytes(out)


def _fake_docx_bytes() -> bytes:
    """伪 docx：普通 zip 改名，条目全部在 docx 白名单之外。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "just a plain zip")
        archive.writestr("sub/data.dat", b"\x00\x01")
    return buffer.getvalue()


def _opc_zip(types: str, doc: Any) -> bytes:
    """最小 OPC 包：指定 [Content_Types].xml、标准 .rels 与 word/document.xml。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", _FULL_RELS)
        archive.writestr("word/document.xml", doc)
    return buffer.getvalue()


def _tool_docx() -> bytes:
    """经工具生成的合法样本 docx 字节（与 test_parser.py 共用同一事实来源）。"""
    import importlib.util
    from pathlib import Path

    tools = Path(__file__).resolve().parents[1] / "fixtures" / "tools"
    spec = importlib.util.spec_from_file_location("make_sample_docx", tools / "make_sample_docx.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_sample_docx()


def _encrypted_pdf() -> bytes:
    """pypdf 自产加密 pdf（RC4，无额外依赖）：提取路径抛 FileNotDecryptedError。"""
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_fake_docx_rejected_with_chinese_reason() -> None:
    """伪 docx（普通 zip 改名）：被条目校验拒，中文原因指明缺少 OPC 标记文件。"""
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse("假.docx", _fake_docx_bytes())
    _assert_rejected(exc_info, "缺少 [Content_Types].xml，不是有效的 docx 文档")


def test_foreign_entries_skipped_not_parsed() -> None:
    """白名单外条目（hack.sh、路径穿越名）跳过不解析：合法条目照常产出，不受污染。"""
    fresh = _tool_docx()
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(fresh)) as src, zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("hack.sh", b"rm -rf /")
        dst.writestr("../evil.txt", b"pwn")
    parsed = DocxParser().parse("员工手册.docx", buffer.getvalue())
    assert parsed.blocks[0].text == "第一条 本手册适用于全体员工。"


def test_doctype_xml_rejected() -> None:
    """条目名合法但 XML 含 DOCTYPE/ENTITY（XXE 向量）：解析前拒绝，中文原因。"""
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse(
            "员工手册.docx",
            _opc_zip(
                _FULL_TYPES,
                b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///c:/win.ini">]>'
                b"<foo>&xxe;</foo>",
            ),
        )
    _assert_rejected(exc_info, "docx 内含 DOCTYPE/ENTITY 声明，已拒绝解析")


def test_zip_bomb_declared_size_rejected() -> None:
    """解压炸弹（全条目谎报声明字节 500MB+1）：默认上限下在解压前按声明尺寸拒绝。"""
    fresh = _tool_docx()
    with zipfile.ZipFile(io.BytesIO(fresh)) as archive:
        size_of = {name: MAX_UNCOMPRESSED_BYTES + 1 for name in archive.namelist()}
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse("员工手册.docx", _forge(fresh, size_of))
    _assert_rejected(exc_info, "docx 解压后总字节超过 500MB 上限")


def test_entry_declared_oversize_attack_rejected() -> None:
    """审查攻击样本：小压缩包诚实声明 document.xml 499MB（过包总量闸）→ 单条目上限毫秒级拒。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w/>")
    attacked = _forge(buffer.getvalue(), {"word/document.xml": 499 * 1024 * 1024})
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse("员工手册.docx", attacked)
    _assert_rejected(exc_info, "docx 单个条目解压后超过 64MB 上限")


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"encrypted": True}, id="encrypted-entry"),
        pytest.param({"method": 99}, id="unknown-method"),
    ],
)
def test_unreadable_entries_rejected_as_422(kwargs: dict[str, Any]) -> None:
    """加密条目（zipfile RuntimeError）/未知压缩法（NotImplementedError）：契约 422 而非 500。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w/>")
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse("员工手册.docx", _forge(buffer.getvalue(), None, **kwargs))
    _assert_rejected(exc_info, "docx 解析失败：条目不可读或压缩方法不支持")


@pytest.mark.parametrize(
    "types,doc",
    [
        pytest.param("<Types/>", b"<w:document/>", id="malformed-types-attributeerror"),
        pytest.param(_FULL_TYPES, b"<not-closed", id="garbage-document-xml-lxml"),
    ],
)
def test_illegal_xml_rejected_as_422(types: str, doc: Any) -> None:
    """残缺 Types（pkgreader AttributeError）/垃圾 document.xml（lxml XMLSyntaxError）：契约 422。"""
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse("员工手册.docx", _opc_zip(types, doc))
    _assert_rejected(exc_info, "docx 解析失败：包体结构非法或已加密")


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"%PDF-1.4 garbage here", id="garbage"),
        pytest.param(_encrypted_pdf(), id="encrypted"),
    ],
)
def test_pdf_malformed_rejected_as_422(data: bytes) -> None:
    """畸形 pdf（空/垃圾/加密）：契约 422 中文原因，非裸异常。"""
    with pytest.raises(ApiError) as exc_info:
        PdfFallbackParser().parse("坏.pdf", data)
    _assert_rejected(exc_info, "pdf 解析失败：文档损坏或已加密")


def test_entry_cap_rejected_and_defaults_pinned() -> None:
    """条目数超限拒绝（注入小上限验证闸门），默认常量钉死为 10 万（standards §6.3）。"""
    assert MAX_ENTRIES == 100_000
    with pytest.raises(ApiError) as exc_info:
        DocxParser(max_entries=1).parse("假.docx", _fake_docx_bytes())  # 伪样本恰 2 条目，1 即触顶
    _assert_rejected(exc_info, "docx 压缩包条目数超过上限")
