r"""MinerU 解析服务 HTTP 客户端（版面分析/表格/OCR）——自 parser.py 独立成文件（行数红线 §1.4）。

叶模块约定（同 parsers_extra.py）：不反向依赖 parser.py，避免调度器循环导入；
§6.3 上限常量在本模块以同值字面量复述并互指 parser.py 单一事实来源。
契约：POST {base_url}/file_parse（multipart 字段 files，mineru-api 2.5.x 实测），
JSON 响应 {"content": str, "pages": int?}（pages 可选，供 §6.3 页数闸）；网络/超时/
非 2xx 重试 1 次后抛 UpstreamError。W2 对接真实部署时契约如有出入只改本类。
"""

from pathlib import PureWindowsPath

import httpx

from app.core.errors import ERR_PARSE_FAILED, ApiError
from app.core.llm import UpstreamError
from app.rag.chunker import Block, ParsedDoc

_MAX_PAGES: int = 2000  # §6.3 pdf 页数上限（与 parser.MAX_PAGES 同源同值）
_MAX_OUTPUT_CHARS: int = 2_000_000  # §6.3 解析输出字符数上限（与 parser.MAX_OUTPUT_CHARS 同源同值）


def _stem(filename: str) -> str:
    """文件名主干（容忍 Windows 路径分隔），作标题兜底与单块 heading_path 根。"""
    return PureWindowsPath(filename).stem


class MinerUParser:
    """MinerU 解析服务 HTTP 客户端（版面分析/表格/OCR）。

    契约见模块 docstring；页数/输出字符上限口径与 parser.py 同源同值。"""

    engine: str = "mineru"
    degraded: bool = False

    def __init__(
        self,
        base_url: str,
        timeout_s: int = 300,
        transport: httpx.BaseTransport | None = None,
        max_pages: int = _MAX_PAGES,
        max_output_chars: int = _MAX_OUTPUT_CHARS,
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
        text: str | None = None
        if isinstance(payload, dict):
            # mineru-api 2.5.x 实测：{"results": {"<stem>": {"md_content": str, ...}}}
            results = payload.get("results")
            if isinstance(results, dict):
                for v in results.values():
                    if isinstance(v, dict) and isinstance(v.get("md_content"), str):
                        text = v["md_content"]
                        break
            if text is None and isinstance(payload.get("content"), str):
                text = payload["content"]  # 设计契约直答形态兼容
        if not isinstance(text, str):  # 缺正文/非 str 一律上游故障，不给半成品
            raise UpstreamError("mineru 响应缺少正文（md_content/content）")
        pages = payload.get("pages") if isinstance(payload, dict) else None
        if isinstance(pages, int) and pages > self.max_pages:  # §6.3 页数闸，响应未携带则跳过
            raise ApiError(ERR_PARSE_FAILED, "pdf 页数超过上限", 422)
        if len(text) > self.max_output_chars:  # §6.3 输出上限（与 parser._check_output 同口径）
            raise ApiError(ERR_PARSE_FAILED, "解析输出字符数超过上限", 422)
        stem = _stem(filename)
        return ParsedDoc(title=stem, blocks=[Block(level=1, heading_path=[stem], text=text)])

    def _post_with_retry(
        self, client: httpx.Client, filename: str, content: bytes
    ) -> httpx.Response:
        """首次 + 重试 1 次（standards：外部调用失败重试 1 次）；网络/超时/非 2xx 皆算失败。"""
        last_error: Exception | None = None
        for _ in range(2):
            try:
                # mineru-api 2.5.x 实测：multipart 字段名为 files（非 file）
                response = client.post(
                    f"{self.base_url}/file_parse", files={"files": (filename, content)}
                )
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:  # 含 ConnectError/TimeoutException/HTTPStatusError
                last_error = exc
        raise UpstreamError("mineru 解析服务调用失败（已重试 1 次）") from last_error
