r"""内嵌图片 OCR 客户端（F-1017）：复用 MinerU /file_parse（图片直传原生支持）。

交互与 MinerUParser 同源：POST {base_url}/file_parse，multipart 字段名 files，
响应 {"results": {"<stem>": {"md_content": ...}}}（兼容 {"content": str}）。
差异：输入是单张图片（png/jpg 等，MinerU 2.5.4 原生支持）；产物剥离
`![](images/...)` 占位行（图片本体即输入，占位是 MinerU 的输出副本，属死文本）。
失败语义：调用方（ingest_pipeline）按"单图失败跳过计数"处理，本模块抛 UpstreamError。
"""

import logging
import re

import httpx

from app.core.llm import UpstreamError

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS: int = 2  # 与 tei/convert 同口径：失败重试 1 次
# MinerU md 产物的图片占位行（整行即占位）：![](images/xxx.jpg) 或 ![alt](任何相对路径)
_PLACEHOLDER_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$", re.MULTILINE)


def strip_image_placeholders(markdown: str) -> str:
    """剥离 MinerU md 里的图片占位行（输入图片自身的输出副本，死文本不入库）。"""
    return _PLACEHOLDER_RE.sub("", markdown).strip()


class ImageOcrClient:
    """单图 OCR：图片字节 → 文字（markdown 去占位行）。"""

    def __init__(
        self,
        base_url: str,
        timeout_s: int = 300,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport  # 测试注入 httpx.MockTransport（与 tei.py/parser.py 同款）

    def ocr(self, image_bytes: bytes, filename: str) -> str:
        last_error: Exception | None = None
        for _ in range(_MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
                    resp = client.post(
                        f"{self.base_url}/file_parse",
                        files={"files": (filename, image_bytes)},
                    )
                if resp.status_code >= 300:
                    raise UpstreamError(f"mineru http {resp.status_code}")
                try:
                    payload = resp.json()
                except ValueError as exc:  # 含 JSONDecodeError/UnicodeDecodeError（同族收口）
                    # I2：坏 JSON 必须收口为 UpstreamError（与 parser.py 同款）——
                    # 裸 ValueError 会同时逃过本模块重试与 ingest_pipeline 的
                    # "只捕 UpstreamError→单图跳过计数"降级轨，把整篇拖成 failed
                    raise UpstreamError("mineru 图片 OCR 响应不是合法 JSON") from exc
                text = self._extract(payload, filename)
                return strip_image_placeholders(text)
            except (httpx.HTTPError, UpstreamError) as exc:
                # UpstreamError（非 2xx/坏 JSON/响应缺正文）一并重试：5xx 与 TEI 同口径可重试
                last_error = exc
        raise UpstreamError(f"mineru 图片 OCR 调用失败（已重试 1 次）: {last_error}")

    @staticmethod
    def _extract(payload: dict, filename: str) -> str:
        """响应双形态兼容（与 MinerUParser 同源）：results.<stem>.md_content 优先。"""
        results = payload.get("results")
        if isinstance(results, dict) and results:
            first = next(iter(results.values()))
            if isinstance(first, dict):
                content = first.get("md_content")
                if isinstance(content, str):
                    return content
        content = payload.get("content")
        if isinstance(content, str):
            return content
        raise UpstreamError(f"mineru 图片 OCR 响应缺正文: {filename}")
