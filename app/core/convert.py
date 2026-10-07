r"""老格式（doc/ppt/xls）自动转换客户端（F-1015）：Gotenberg LibreOffice headless。

为什么走 Gotenberg：老二进制格式（97-2003 OLE）无可靠的纯 Python 解析库（xlrd 仅旧
.xls 且安全历史差、antiword 维护停滞），且宏/内嵌对象是真实攻击面——LibreOffice
headless 转换后复用既有解析链，把老格式的攻击面收敛为"对内网转换服务的一次
multipart 调用"。转换放异步段（10-60s 不阻塞上传请求）。

转换目标是 PDF（2026-10-05 容器实测裁定）：Gotenberg 8 libreoffice 路由只输出
PDF、无保留原格式选项——曾误设 OOXML 目标，导致 PDF 字节按 .docx 落盘、解析必败。
PDF 产物一并服务两处：入库解析走 pdf 链（文字版 pypdf 快速通道/扫描件 MinerU），
在线预览端点直通该 PDF（无需预览时再转换）。

交互：POST {base_url}/forms/libreoffice/convert，multipart 字段 files，
成功 200 返回 PDF 字节（响应文件名是 Gotenberg 随机 UUID，不采用——以原名 stem+.pdf
命名，original_filename 另行保留真实来源）。

分层归属（#12 收敛装配）：本模块是无业务状态的 Gotenberg HTTP 客户端，形态同
tei.py/openai_compat.py（超时+一次重试+transport 注入），且装配层 core/assembly
要按 Settings 装配它——原在 services 会让 core 反向依赖 services（§1.1 禁止反向），
故移入 core。
"""

import logging
from pathlib import PureWindowsPath

import httpx

from app.core.errors import ERR_PARSE_FAILED, ApiError
from app.core.llm import UpstreamError

logger = logging.getLogger(__name__)

_CONVERT_TIMEOUT_S: int = 120
_MAX_ATTEMPTS: int = 2  # 与 tei.py 同口径：失败重试 1 次
_LEGACY_EXTS: frozenset[str] = frozenset({".doc", ".ppt", ".xls"})  # 编排层判断是否需转换


class DocumentConverter:
    """Gotenberg 转换客户端：老格式字节 → PDF 字节。"""

    def __init__(
        self,
        base_url: str,
        timeout_s: int = _CONVERT_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport  # 测试注入 httpx.MockTransport（与 tei.py/parser.py 同款）

    def convert_to_pdf(self, filename: str, content: bytes) -> tuple[bytes, str]:
        """转换并返回 (PDF 字节, 新文件名 stem.pdf)。非 2xx/传输错误/非 PDF 魔数 →
        UpstreamError（重试 1 次后仍败才抛；ingest 状态机统一转 failed+中文原因；
        预览端点转 ApiError 中文降级）。"""
        last_error: Exception | None = None
        for _ in range(_MAX_ATTEMPTS):
            try:
                with httpx.Client(timeout=self.timeout_s, transport=self._transport) as client:
                    resp = client.post(
                        f"{self.base_url}/forms/libreoffice/convert",
                        files={"files": (filename, content)},
                    )
                if resp.status_code >= 300:
                    raise UpstreamError(f"gotenberg http {resp.status_code}")
                if not resp.content.startswith(b"%PDF-"):
                    # 防上游行为漂移：非 PDF 魔数按失败处理，不让坏字节流入库/预览
                    raise UpstreamError("gotenberg 返回内容非 PDF")
                return resp.content, f"{PureWindowsPath(filename).stem}.pdf"
            except (httpx.HTTPError, UpstreamError) as exc:
                # I1：非 2xx 的 UpstreamError 必须与传输错误一并记录后重试——原写法
                # raise 落在 try 内而 except 只捕 httpx.HTTPError，Gotenberg 瞬时 502
                # 被放大为整篇失败；与 tei.py/ocr.py 重试口径一致（standards §5）
                last_error = exc
        raise UpstreamError(f"gotenberg 转换服务调用失败（已重试 1 次）: {last_error}")


def raise_convert_unavailable() -> None:
    """转换服务未配置时的统一失败（process_document 编排层调用）。"""
    raise ApiError(ERR_PARSE_FAILED, "老格式转换服务未配置，请另存为 docx/pptx/xlsx 后上传", 422)
