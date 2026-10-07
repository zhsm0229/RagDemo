r"""Gotenberg 老格式转换客户端测试（I1/I17）：MockTransport 注入（tei.py 用例为模板）。

断言表：①正常转换 POST {base_url}/forms/libreoffice/convert（multipart 字段 files），
PDF 字节透传 + stem.pdf 重命名；②首次 502 → 重试 1 次后成功且恰调用 2 次（I1：非 2xx
记录 last_error 后 continue 重试，Gotenberg 瞬时 502 不再放大为整篇失败）；③连败恰
两次后抛 UpstreamError；④200 但非 PDF 魔数 → UpstreamError（防上游行为漂移守卫）。
"""

import httpx
import pytest

from app.core.convert import DocumentConverter
from app.core.llm import UpstreamError

BASE_URL = "http://gotenberg.internal:3000"
_PDF_BYTES = b"%PDF-1.4 mock"


# ---------------------------------------------------------------------------
# 用例 ①：正常转换 → POST {base_url}/forms/libreoffice/convert，PDF 字节透传
# ---------------------------------------------------------------------------


def test_convert_posts_multipart_and_returns_stem_pdf() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, content=_PDF_BYTES)

    pdf, name = DocumentConverter(
        BASE_URL, timeout_s=5, transport=httpx.MockTransport(handler)
    ).convert_to_pdf("report.doc", b"legacy-ole-bytes")

    assert captured["url"] == f"{BASE_URL}/forms/libreoffice/convert"  # 契约路由
    assert b"report.doc" in captured["body"]  # type: ignore —— multipart 字段带原文件名
    assert b"legacy-ole-bytes" in captured["body"]  # type: ignore —— 原字节随 multipart 上送
    assert pdf == _PDF_BYTES  # PDF 字节原样返回
    assert name == "report.pdf"  # 以原名 stem+.pdf 命名（Gotenberg 随机 UUID 不采用）


# ---------------------------------------------------------------------------
# 用例 ②③：非 2xx 重试 1 次（恰 2 次独立请求）；两次皆败抛 UpstreamError
# ---------------------------------------------------------------------------


def test_convert_first_502_then_200_succeeds_after_exactly_two_calls() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502, text="bad gateway")  # Gotenberg 瞬时 502
        return httpx.Response(200, content=_PDF_BYTES)

    pdf, _ = DocumentConverter(
        BASE_URL, timeout_s=5, transport=httpx.MockTransport(handler)
    ).convert_to_pdf("制度.doc", b"legacy-ole-bytes")

    assert calls == [1, 1]  # 恰 2 次尝试：非 2xx 记录 last_error 后重试 1 次（I1）
    assert pdf == _PDF_BYTES  # 重试成功后 PDF 字节透传


def test_convert_both_attempts_fail_raises_upstream_error_exactly_two_calls() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="service unavailable")

    with pytest.raises(UpstreamError, match="重试"):
        DocumentConverter(
            BASE_URL, timeout_s=5, transport=httpx.MockTransport(handler)
        ).convert_to_pdf("制度.ppt", b"legacy-ole-bytes")

    assert len(calls) == 2  # 两次皆败：恰调用 2 次（一次重试，不更多）


# ---------------------------------------------------------------------------
# 用例 ④：200 + 非 PDF 魔数 → UpstreamError（防上游行为漂移，坏字节不入库/预览）
# ---------------------------------------------------------------------------


def test_convert_non_pdf_magic_raises_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not-a-pdf</html>")

    with pytest.raises(UpstreamError, match="非 PDF"):
        DocumentConverter(
            BASE_URL, timeout_s=5, transport=httpx.MockTransport(handler)
        ).convert_to_pdf("制度.xls", b"legacy-ole-bytes")
