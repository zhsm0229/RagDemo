r"""解析器契约测试：brief 断言逐条覆盖（攻击/畸形包拒绝矩阵见 test_parser_security.py）。

覆盖：docx 标题层级 blocks 与 heading_path 全路径（T7 契约，含跳级截断回退）、
pypdf 解出文本且 engine="pypdf"（降级标记）、MinerU stub 重试语义（首败 500→重试
200 成功；持续 500/ConnectError 重试 1 次后抛 UpstreamError，恰 2 次请求）、响应
契约（缺 content/非 str/非 JSON 一律 UpstreamError）、`.doc` 抛
ApiError(1001, "请另存为 docx", 422)、调度树、pdf 页数/输出字符上限与 MinerU 可
注入上限、入库样本与工具再产物解析一致。
"""

import importlib.util
import io
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from docx import Document

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.llm import UpstreamError
from app.rag.chunker import ParsedDoc
from app.rag.parser import (
    MAX_ENTRY_XML_BYTES,
    MAX_OUTPUT_CHARS,
    MAX_PAGES,
    Block,
    DocxParser,
    MinerUParser,
    Parser,
    PdfFallbackParser,
    get_parser,
)

TOOLS = Path(__file__).resolve().parents[1] / "fixtures" / "tools"
SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "samples"
FRESH_SETTINGS = Settings(jwt_secret="unit-test", mineru_base_url="")  # 显式钉死降级路径


def _tool(name: str) -> ModuleType:
    """按文件路径加载 fixtures/tools 下的生成工具（无包结构，直接 spec 加载）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected_docx_doc() -> ParsedDoc:
    """样例 docx 的精确解析产物（多处测试共用）：表格行并入当前节，行内空格连接、行间换行。"""
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


def _mineru(
    script: list[httpx.Response | Exception],
    calls: dict[str, int],
    seen: list[httpx.Request] | None = None,
    **kwargs: int,
) -> MinerUParser:
    """MockTransport 按脚本出牌（越界复用末项；异常项直接抛出），记录调用数与请求。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        item = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    return MinerUParser(
        "http://mineru.test", transport=httpx.MockTransport(handler), **{"timeout_s": 5, **kwargs}
    )


def test_docx_blocks_hierarchy_and_full_heading_path() -> None:
    """docx：H1/H2/正文/表格按序产出标题节，heading_path 为根到当前节全路径（T7 契约）。"""
    doc = DocxParser().parse("员工手册.docx", _tool("make_sample_docx").build_sample_docx())
    assert doc == _expected_docx_doc()
    assert doc.blocks[1].heading_path == ["第一章 总则", "1.1 适用范围"]  # 全路径，非仅当前节
    assert [b.level for b in doc.blocks] == [1, 2, 1]


def test_heading_skip_level_collapses_path() -> None:
    """跳级（H1 后直接 H3）：heading_path 截断回退到现有深度，不补空字符串占位。"""
    document = Document()
    document.add_heading("第一章", level=1)
    document.add_heading("1.1.1 细则", level=3)
    buffer = io.BytesIO()
    document.save(buffer)
    doc = DocxParser().parse("跳级.docx", buffer.getvalue())
    assert [b.level for b in doc.blocks] == [1, 3]
    assert doc.blocks[1].heading_path == ["第一章", "1.1.1 细则"]


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
    """pypdf 降级：逐页文本以 \\f 拼接（与 precheck.page_hint 计页约定一致），标记正确。"""
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


def test_mineru_success_and_first_fail_retry_succeeds() -> None:
    """MinerU 200 直取 content（恰 1 次）；首败 500→重试 200 成功（恰 2 次），契约 POST /file_parse。"""
    ok = {"content": "全公司差旅报销标准。"}
    calls: dict[str, int] = {"n": 0}
    doc = _mineru([httpx.Response(200, json=ok)], calls).parse("样例.pdf", b"%PDF-fake")
    assert doc == ParsedDoc(
        title="样例", blocks=[Block(level=1, heading_path=["样例"], text="全公司差旅报销标准。")]
    )
    assert calls["n"] == 1
    calls2: dict[str, int] = {"n": 0}
    seen: list[httpx.Request] = []
    doc2 = _mineru(
        [httpx.Response(500, text="boom"), httpx.Response(200, json=ok)], calls2, seen
    ).parse("样例.pdf", b"%PDF-fake")
    assert doc2.blocks[0].text == "全公司差旅报销标准。"
    assert calls2["n"] == 2
    assert all(r.method == "POST" and r.url.path == "/file_parse" for r in seen)


@pytest.mark.parametrize(
    "script",
    [
        pytest.param([httpx.Response(500, text="boom")], id="persistent-500"),
        pytest.param([httpx.ConnectError("connection refused")], id="connect-error"),
    ],
)
def test_mineru_retry_exhausted_raises_upstream_error(
    script: list[httpx.Response | Exception],
) -> None:
    """MinerU 持续 500 / ConnectError：首次 + 重试 1 次后抛 UpstreamError，恰好请求 2 次。"""
    calls: dict[str, int] = {"n": 0}
    with pytest.raises(UpstreamError):
        _mineru(script, calls).parse("样例.pdf", b"%PDF-fake")
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(200, json={}), id="missing-content"),
        pytest.param(httpx.Response(200, json={"content": 123}), id="non-str-content"),
        pytest.param(httpx.Response(200, text="not-json"), id="non-json-body"),
    ],
)
def test_mineru_response_contract_rejects(response: httpx.Response) -> None:
    """MinerU 响应契约：缺 content / content 非 str / 非 JSON 一律 UpstreamError（不给半成品）。"""
    with pytest.raises(UpstreamError):
        _mineru([response], {"n": 0}).parse("样例.pdf", b"%PDF-fake")


def test_resource_caps_pinned_and_rejected() -> None:
    """资源上限：常量钉死；pdf 页数/输出字符与 MinerU 页数/输出字符超限一律 422 中文原因。"""
    assert (MAX_PAGES, MAX_OUTPUT_CHARS, MAX_ENTRY_XML_BYTES) == (2000, 2_000_000, 64 * 1024 * 1024)
    sample_pdf = _tool("make_sample_pdf").build_sample_pdf()
    with pytest.raises(ApiError) as exc_info:
        PdfFallbackParser(max_pages=1).parse("季报.pdf", sample_pdf)  # 样本恰 2 页
    assert (exc_info.value.message, exc_info.value.http_status) == ("pdf 页数超过上限", 422)
    with pytest.raises(ApiError) as exc_info:
        PdfFallbackParser(max_output_chars=10).parse("季报.pdf", sample_pdf)
    assert (exc_info.value.message, exc_info.value.http_status) == ("解析输出字符数超过上限", 422)
    with pytest.raises(ApiError) as exc_info:
        _mineru(
            [httpx.Response(200, json={"content": "x", "pages": 5})], {"n": 0}, max_pages=4
        ).parse("样例.pdf", b"%PDF-fake")
    assert (exc_info.value.message, exc_info.value.http_status) == ("pdf 页数超过上限", 422)
    with pytest.raises(ApiError) as exc_info:
        _mineru(
            [httpx.Response(200, json={"content": "x" * 11})], {"n": 0}, max_output_chars=10
        ).parse("样例.pdf", b"%PDF-fake")
    assert (exc_info.value.message, exc_info.value.http_status) == ("解析输出字符数超过上限", 422)


def test_doc_extension_and_dispatch() -> None:
    """`.doc` 三字段逐字拒收；调度树：docx→DocxParser；pdf 无 mineru 配置→pypdf 降级；配置→MinerU。"""
    with pytest.raises(ApiError) as exc_info:
        get_parser("旧文档.doc", FRESH_SETTINGS)
    assert (exc_info.value.code, exc_info.value.message, exc_info.value.http_status) == (
        1001,
        "请另存为 docx",
        422,
    )
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
