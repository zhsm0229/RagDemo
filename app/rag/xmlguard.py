r"""docx 包体 XML 安全扫描防线（standards §6.1/§6.3）：包体条目字节的 DOCTYPE/ENTITY 拦截。

为什么独立成模块：docx 是 zip 包，包体内 XML 部件（word/document.xml 等）携带
DOCTYPE/ENTITY 声明即构成 XXE 注入面；本防线不依赖下游解析器（lxml 等）的实现
细节，在读取条目字节时就地拦截。扫描必须流式分块（防内存放大 OOM：单条目声明
上限 64MB，绝不整包读入内存），块间保留重叠窗口（最长模式 len(b"<!doctype")-1，
防模式跨块边界漏检），并对声明大小谎报做实读累计第二道闸。本模块只做单条目
字节扫描判定；条目白名单与包级总量/条目数闸门留在 parser.DocxParser._validate_package。
"""

import zipfile

from app.core.errors import ERR_PARSE_FAILED, ApiError

XML_SCAN_CHUNK: int = 1 << 20  # 流式扫描块大小
SCAN_OVERLAP: int = 8  # 重叠窗口 = 最长模式 len(b"<!doctype") - 1，防模式跨块漏检


def scan_entry(
    archive: zipfile.ZipFile, name: str, declared: int, max_entry_xml_bytes: int
) -> None:
    """自读条目守规（§6.1）：声明超限即拒；流式分块扫描 DOCTYPE/ENTITY（重叠窗口防跨界漏检）。"""
    if declared > max_entry_xml_bytes:
        raise ApiError(ERR_PARSE_FAILED, "OOXML 单个条目解压后超过 64MB 上限", 422)
    tail = b""
    read = 0
    with archive.open(name) as fh:
        while chunk := fh.read(XML_SCAN_CHUNK):
            read += len(chunk)
            if read > max_entry_xml_bytes:  # 实读限量：谎报声明头的第二道闸
                raise ApiError(ERR_PARSE_FAILED, "OOXML 单个条目解压后超过 64MB 上限", 422)
            window = (tail + chunk).lower()  # 仅块级小写副本，无全量 data.lower()
            if b"<!doctype" in window or b"<!entity" in window:
                raise ApiError(
                    ERR_PARSE_FAILED, "OOXML 包内含 DOCTYPE/ENTITY 声明，已拒绝解析", 422
                )
            tail = window[-SCAN_OVERLAP:]
