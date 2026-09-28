r"""解析器测试：brief 断言逐条覆盖，断言精确到逐字。

覆盖：docx 标题层级 blocks 与 heading_path 全路径（T7 契约核验）、pypdf 解出文本
且 engine="pypdf"（降级标记 degraded=True）、MinerU stub（httpx.MockTransport）
500 两次 → 重试 1 次后抛 UpstreamError（恰好调用 2 次）、`.doc` 抛
ApiError(1001, "请另存为 docx", 422)、伪 docx（普通 zip 改名）被条目校验拒且给
中文原因、解压炸弹（谎报声明字节 500MB+1）被拒、白名单外条目跳过不影响解析、
DOCTYPE/ENTITY 的 XML 拒绝（standards §6.1）、入库样本 fixtures/samples 与工具
再生成产物解析结果一致。
"""

import importlib.util
import io
import zipfile
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.llm import UpstreamError
from app.rag.chunker import ParsedDoc
from app.rag.parser import (
    MAX_ENTRIES,
    MAX_OUTPUT_CHARS,
    MAX_UNCOMPRESSED_BYTES,
    Block,
    DocxParser,
    MinerUParser,
    Parser,
    PdfFallbackParser,
    get_parser,
)

TOOLS = Path(__file__).resolve().parents[1] / "fixtures" / "tools"
SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "samples"
FRESH_SETTINGS = Settings(jwt_secret="unit-test")


def _tool(name: str) -> ModuleType:
    """按文件路径加载 fixtures/tools 下的生成工具（无包结构，直接 spec 加载）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patch_declared_size(data: bytes, size: int) -> bytes:
    """把 zip 中央目录中每个条目声明的解压后字节量改为 size（毫秒级伪造解压炸弹声明头）。"""
    out = bytearray(data)
    idx = 0
    while (found := out.find(b"PK\x01\x02", idx)) >= 0:
        out[found + 24 : found + 28] = size.to_bytes(4, "little")
        idx = found + 4
    return bytes(out)


def _fake_docx_bytes() -> bytes:
    """伪 docx：普通 zip 改名，条目全部在 docx 白名单之外。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "just a plain zip")
        archive.writestr("sub/data.dat", b"\x00\x01")
    return buffer.getvalue()


def _expected_docx_doc() -> ParsedDoc:
    """样例 docx 的精确解析产物（两处测试共用）：表格行并入当前节，行内空格连接、行间换行。"""
    return ParsedDoc(
        title="员工手册",
        blocks=[
            Block(level=1, heading_path=["第一章 总则"], text="第一条 本手册适用于全体员工。"),
            Block(
                level=2,
                heading_path=["第一章 总则", "1.1 适用范围"],
                text="本手册覆盖总部及各分支机构。\n项目 说明\n差旅 凭发票据实报销",
            ),
            Block(level=1, heading_path=["第二章 考勤"], text="第二条 工作时间为每周一至周五。"),
        ],
    )


def test_docx_blocks_hierarchy_and_full_heading_path() -> None:
    """docx：H1/H2/正文/表格按序产出标题节，heading_path 为根到当前节全路径（T7 契约）。"""
    doc = DocxParser().parse("员工手册.docx", _tool("make_sample_docx").build_sample_docx())
    assert doc == _expected_docx_doc()
    assert doc.blocks[1].heading_path == ["第一章 总则", "1.1 适用范围"]  # 全路径，非仅当前节
    assert [b.level for b in doc.blocks] == [1, 2, 1]


def test_committed_samples_match_tool_output() -> None:
    """入库样本 fixtures/samples 与生成工具再产物的解析结果一致（防样本漂移）。"""
    cases = [
        (
            "sample.docx",
            _tool("make_sample_docx").build_sample_docx(),
            DocxParser(),
            "员工手册.docx",
        ),
        (
            "sample.pdf",
            _tool("make_sample_pdf").build_sample_pdf(),
            PdfFallbackParser(),
            "季报.pdf",
        ),
    ]
    for name, fresh, parser, filename in cases:
        committed = (SAMPLES / name).read_bytes()
        assert parser.parse(filename, committed) == parser.parse(filename, fresh)


def test_pdf_fallback_pages_and_engine() -> None:
    """pypdf 降级：逐页文本以 \\f 拼接（与 precheck.page_hint 计页约定一致），engine/degraded 标记正确。"""
    parser = PdfFallbackParser()
    doc = parser.parse("季报.pdf", _tool("make_sample_pdf").build_sample_pdf())
    assert parser.engine == "pypdf"
    assert parser.degraded is True
    assert doc == ParsedDoc(
        title="季报",
        blocks=[
            Block(
                level=1,
                heading_path=["季报"],
                text="Chapter 1 Overview. Total expense lines.\fAppendix contact table.",
            )
        ],
    )


def test_mineru_500_retries_once_then_upstream_error() -> None:
    """MinerU 500 两次：首次 + 重试 1 次后抛 UpstreamError，恰好请求 2 次 POST /file_parse。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, text="boom")

    parser = MinerUParser(
        "http://mineru.test/", timeout_s=5, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(UpstreamError):
        parser.parse("样例.pdf", b"%PDF-fake")
    assert len(requests) == 2
    assert all(r.method == "POST" and r.url.path == "/file_parse" for r in requests)


def test_mineru_success_returns_content() -> None:
    """MinerU 200：取 JSON content 产出单块 ParsedDoc（engine="mineru"，非降级），成功不重试。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"content": "全公司差旅报销标准。"})

    parser = MinerUParser("http://mineru.test", timeout_s=5, transport=httpx.MockTransport(handler))
    doc = parser.parse("样例.pdf", b"%PDF-fake")
    assert parser.engine == "mineru"
    assert parser.degraded is False
    assert doc == ParsedDoc(
        title="样例", blocks=[Block(level=1, heading_path=["样例"], text="全公司差旅报销标准。")]
    )
    assert calls["n"] == 1


def test_mineru_missing_content_key_is_upstream_error() -> None:
    """MinerU 响应缺 content 字段：按上游故障抛 UpstreamError（不给半成品文本）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    parser = MinerUParser("http://mineru.test", timeout_s=5, transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError):
        parser.parse("样例.pdf", b"%PDF-fake")


def test_doc_extension_exact_api_error() -> None:
    """`.doc` 抛 ApiError(1001, "请另存为 docx", 422)，三个字段逐字相等。"""
    with pytest.raises(ApiError) as exc_info:
        get_parser("旧文档.doc", FRESH_SETTINGS)
    error = exc_info.value
    assert (error.code, error.message, error.http_status) == (1001, "请另存为 docx", 422)


def test_get_parser_dispatch() -> None:
    """调度树：docx→DocxParser；pdf 无 mineru 配置→pypdf 降级；pdf 配置 mineru→MinerUParser（参数透传）。"""
    fallback = get_parser("a.pdf", FRESH_SETTINGS)
    assert isinstance(fallback, PdfFallbackParser) and isinstance(fallback, Parser)
    assert fallback.degraded is True
    mineru_settings = Settings(
        jwt_secret="unit-test", mineru_base_url="http://m.test", mineru_timeout_s=7
    )
    mineru = get_parser("a.pdf", mineru_settings)
    assert isinstance(mineru, MinerUParser) and isinstance(mineru, Parser)
    assert mineru.degraded is False
    assert mineru.base_url == "http://m.test"
    assert mineru.timeout_s == 7
    docx = get_parser("a.docx", FRESH_SETTINGS)
    assert isinstance(docx, DocxParser) and isinstance(docx, Parser)
    assert docx.degraded is False


def test_fake_docx_rejected_with_chinese_reason() -> None:
    """伪 docx（普通 zip 改名）：被条目校验拒，中文原因指明缺少 OPC 标记文件。"""
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse("假.docx", _fake_docx_bytes())
    assert (exc_info.value.message, exc_info.value.http_status) == (
        "缺少 [Content_Types].xml，不是有效的 docx 文档",
        422,
    )


def test_foreign_entries_skipped_not_parsed() -> None:
    """白名单外条目（hack.sh、路径穿越名）跳过不解析：合法条目照常产出与基线一致的 ParsedDoc。"""
    fresh = _tool("make_sample_docx").build_sample_docx()
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(fresh)) as src, zipfile.ZipFile(buffer, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("hack.sh", b"rm -rf /")
        dst.writestr("../evil.txt", b"pwn")
    assert DocxParser().parse("员工手册.docx", buffer.getvalue()) == _expected_docx_doc()


def test_doctype_xml_rejected() -> None:
    """条目名合法但 XML 含 DOCTYPE/ENTITY（XXE 向量）：解析前拒绝，中文原因。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///c:/win.ini">]>'
            "<foo>&xxe;</foo>",
        )
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse("员工手册.docx", buffer.getvalue())
    assert (exc_info.value.message, exc_info.value.http_status) == (
        "docx 内含 DOCTYPE/ENTITY 声明，已拒绝解析",
        422,
    )


def test_zip_bomb_declared_size_rejected() -> None:
    """解压炸弹（谎报声明字节 500MB+1）：默认上限下在解压前按声明尺寸拒绝。"""
    assert MAX_UNCOMPRESSED_BYTES == 500 * 1024 * 1024  # standards §6.3 钉死
    bomb = _patch_declared_size(
        _tool("make_sample_docx").build_sample_docx(), MAX_UNCOMPRESSED_BYTES + 1
    )
    with pytest.raises(ApiError) as exc_info:
        DocxParser().parse("员工手册.docx", bomb)
    assert (exc_info.value.message, exc_info.value.http_status) == (
        "docx 解压后总字节超过 500MB 上限",
        422,
    )


def test_entry_cap_rejected_and_defaults_pinned() -> None:
    """条目数超限拒绝（注入小上限验证闸门），默认常量钉死为 10 万（standards §6.3）。"""
    assert MAX_ENTRIES == 100_000
    with pytest.raises(ApiError) as exc_info:
        DocxParser(max_entries=1).parse(
            "假.docx", _fake_docx_bytes()
        )  # 伪样本恰有 2 条目，1 即触顶
    assert (exc_info.value.message, exc_info.value.http_status) == (
        "docx 压缩包条目数超过上限",
        422,
    )


def test_output_char_cap_rejected() -> None:
    """解析输出字符数上限（standards §6.3）：超限拒绝，默认常量钉死。"""
    assert MAX_OUTPUT_CHARS == 2_000_000
    with pytest.raises(ApiError) as exc_info:
        PdfFallbackParser(max_output_chars=10).parse(
            "季报.pdf", _tool("make_sample_pdf").build_sample_pdf()
        )
    assert (exc_info.value.message, exc_info.value.http_status) == ("解析输出字符数超过上限", 422)
