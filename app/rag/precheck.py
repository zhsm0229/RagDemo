r"""预检纯函数：两段式预检的第一段闸门与敏感信息/乱码/近似查重算法层（standards §10.8）。

check_sync 做白名单闸门：扩展名（11 类白名单，见本文件 ALLOWED_EXTS）→ MIME → 大小（<= 50MB）→ 魔数
（%PDF / PK\x03\x04），全过才放行；SHA-256 无条件计算（拒收样本也留审计指纹），
失败返回稳定 reason 码。敏感扫描全部用预编译线性正则（定长或 [0-9]{13,19} 有界
量词，无嵌套量词、回溯有界，SEC-07），命中后再过纯函数校验：身份证 18 位加权
校验码（id_card_valid）、银行卡 Luhn（luhn_valid）。乱码占比 = 控制字符
（U+0000–001F、U+007F–009F，即 Unicode Cc；换行/回车/制表是版面结构字符，豁免）与
U+FFFD 的字符占比。simhash64 为
64 位 Simhash（字符 2-gram + blake2b 8 字节指纹，进程间确定），hamming 为汉明
距离；GARBLE_THRESHOLD / SIMHASH_NEAR 为下游判定阈值常量。
"""

import hashlib
import io
import re
import zipfile
from pathlib import PureWindowsPath
from typing import NamedTuple

# 支持格式（2026-10-05 扩展决策，standards §10.1）：知识文档八类 + 老格式三类（转换链）
ALLOWED_EXTS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".md", ".txt", ".csv", ".xlsx", ".pptx", ".html", ".doc", ".ppt", ".xls"}
)
# 宏格式一律拒收（宏是 OOXML 主要攻击面；F-1014）——先于白名单检查以给出专属文案
MACRO_EXTS: frozenset[str] = frozenset({".xlsm", ".docm", ".pptm"})
# 文本类：跳过魔数（无二进制头），改校验 UTF-8 可解码且无 NUL 字节
TEXT_EXTS: frozenset[str] = frozenset({".txt", ".md", ".csv", ".html"})
MAX_SYNC_BYTES: int = 50 * 1024 * 1024

# 按扩展名给合法 MIME 集合：单上传的 Content-Type 由客户端给出（存在变体），
# zip 导入由调用方按扩展名补齐——集合判定兼容变体（如 md 的 text/markdown 与 text/plain）
_MIMES_BY_EXT: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    ".xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    ".pptx": frozenset(
        {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    ),
    ".doc": frozenset({"application/msword"}),
    ".xls": frozenset({"application/vnd.ms-excel"}),
    ".ppt": frozenset({"application/vnd.ms-powerpoint"}),
    ".md": frozenset({"text/markdown", "text/plain"}),
    ".txt": frozenset({"text/plain"}),
    ".csv": frozenset({"text/csv", "application/csv", "text/plain"}),
    ".html": frozenset({"text/html"}),
}
ALLOWED_MIMES: frozenset[str] = frozenset().union(*_MIMES_BY_EXT.values())  # 兼容旧引用
MAGIC_BY_EXT: dict[str, bytes] = {
    ".pdf": b"%PDF",
    **{ext: b"PK\x03\x04" for ext in (".docx", ".xlsx", ".pptx")},  # OOXML 家族同为 zip 头
    **{ext: b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" for ext in (".doc", ".ppt", ".xls")},  # OLE2
}
# OOXML 子类型判型：扩展名 → [Content_Types].xml 必含的家族标记（防改名混入，F-1014）
_OOXML_FAMILY_BY_EXT: dict[str, str] = {
    ".docx": "wordprocessingml",
    ".xlsx": "spreadsheetml",
    ".pptx": "presentationml",
}
SIMHASH_NEAR: int = 3
GARBLE_THRESHOLD: float = 0.05  # 乱码率阈值（清洗后全文判定，ingest_pipeline 消费）

# 全部预编译为模块常量；定长/有界量词 + 数字边界环视，回溯有界（SEC-07 ReDoS 防护）
_ID_CARD_RE = re.compile(r"(?<![0-9Xx])[0-9]{17}[0-9Xx](?![0-9Xx])")
_MOBILE_RE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
_BANK_CARD_RE = re.compile(r"(?<![0-9])[0-9]{13,19}(?![0-9])")
# 控制字符 = Unicode Cc：U+0000–001F 与 U+007F–009F，外加替换符 U+FFFD；
# \n\r\t（U+0009/000A/000D）是版面结构字符而非乱码，从分子豁免（短行密集文档
# 每行一个换行即可把占比推过阈值，属误报——Task 8 交接事项闭环）
_GARBLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffd]")

_ID_WEIGHTS: tuple[int, ...] = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_MAP: str = "10X98765432"  # Σ mod 11 = 0..10 → 校验码


class SyncResult(NamedTuple):
    """check_sync 产物：ok 全过为 True；reason 为稳定失败码（放行为 None）；sha256 恒有值。"""

    ok: bool
    reason: str | None
    sha256: str


class SensitiveHit(NamedTuple):
    """敏感命中：kind 为 id_card/mobile/bank_card；page_hint 按 \f 计页（首页 1）；offset 为字符偏移。"""

    kind: str
    page_hint: int
    offset: int


def id_card_valid(number: str) -> bool:
    """GB 11643 校验码：前 17 位加权求和 mod 11 映射校验码，末位 X 大小写均可。"""
    if len(number) != 18:
        return False
    body, check = number[:17], number[17]
    if not (body.isascii() and body.isdigit()) or check not in "0123456789Xx":
        return False
    total = sum(int(digit) * weight for digit, weight in zip(body, _ID_WEIGHTS))
    return _ID_CHECK_MAP[total % 11] == check.upper()


def luhn_valid(number: str) -> bool:
    """Luhn 纯校验（不限长度，13-19 位窗口由调用方约束）：总和 mod 10 == 0 为合法。"""
    if len(number) < 2 or not (number.isascii() and number.isdigit()):
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        digit = int(ch)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def check_sync(filename: str, mime: str, content: bytes) -> SyncResult:
    """同步预检闸门：宏扩展名 → 扩展名 → MIME → 大小 → 内容判型，按序短路，失败给稳定 reason 码。

    内容判型分三类：OOXML（PK 头 → 读 [Content_Types].xml 判家族并拒宏条目，防改名
    混入，F-1014）；文本类（UTF-8 可解码且无 NUL 字节）；其余按魔数头。损坏 zip 放行
    给解析器报"包非法"（与现状一致）；魔数≠内容安全：内容级风险由解析器与生成链路处置。
    """
    sha256 = hashlib.sha256(content).hexdigest()
    ext = PureWindowsPath(filename).suffix.lower()
    if ext in MACRO_EXTS:
        return SyncResult(False, "macro_not_allowed", sha256)
    if ext not in ALLOWED_EXTS:
        return SyncResult(False, "ext_not_allowed", sha256)
    if mime not in _MIMES_BY_EXT.get(ext, frozenset()):
        return SyncResult(False, "mime_not_allowed", sha256)
    if len(content) > MAX_SYNC_BYTES:
        return SyncResult(False, "too_large", sha256)
    if ext in TEXT_EXTS:
        if _is_valid_text(content):
            return SyncResult(True, None, sha256)
        return SyncResult(False, "magic_mismatch", sha256)
    if not content.startswith(MAGIC_BY_EXT[ext]):
        return SyncResult(False, "magic_mismatch", sha256)
    if ext in _OOXML_FAMILY_BY_EXT:
        ok, reason = _validate_ooxml_family(ext, content)
        if not ok:
            return SyncResult(False, reason, sha256)
    return SyncResult(True, None, sha256)


def _is_valid_text(content: bytes) -> bool:
    """文本类校验：UTF-8 严格解码成功且不含 NUL 字节（二进制伪装文本的最廉信号）。"""
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _validate_ooxml_family(ext: str, content: bytes) -> tuple[bool, str | None]:
    """OOXML 子类型判型（F-1014）：zip 可读时校验 ① 包内无 vbaProject.bin（宏拒收）
    ② [Content_Types].xml 含本扩展名家族标记（防改名混入）。zip 损坏放行给解析器
    报"包非法"（与现状一致）；判型不通过给 magic_mismatch。"""
    family = _OOXML_FAMILY_BY_EXT[ext]
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            if any(name.endswith("vbaProject.bin") for name in names):
                return False, "macro_not_allowed"
            content_types = archive.read("[Content_Types].xml").decode("utf-8", "ignore")
    except zipfile.BadZipFile:
        return True, None  # 非法 zip 由 parser._validate_package 给中文原因
    if family not in content_types:
        return False, "magic_mismatch"
    return True, None


async def check_async(filename: str, mime: str, content: bytes) -> SyncResult:
    """异步包装：纯函数无 IO，签名与 check_sync 一致，供异步上传链路 await。"""
    return check_sync(filename, mime, content)


def garbled_ratio(text: str) -> float:
    """乱码占比 = (控制字符 + U+FFFD) 字符数 / 总字符数；\\n\\r\\t 版面字符豁免；空文本 0.0。"""
    if not text:
        return 0.0
    return len(_GARBLE_RE.findall(text)) / len(text)


def _page_hint(text: str, offset: int) -> int:
    """页码提示：解析器以 \f（换页符）分页，命中前的 \f 计数 + 1。

    与 parser._page_blocks 并存不冲突（task-6b 互指）：后者按同一 \f 约定为
    Block.page 打引用定位页码；本函数只服务敏感扫描命中的原文定位。"""
    return text.count("\f", 0, offset) + 1


def scan_sensitive(text: str) -> list[SensitiveHit]:
    """敏感扫描：身份证（过校验码）/手机号/银行卡（过 Luhn），命中按 offset 升序。"""
    hits: list[SensitiveHit] = []
    for match in _ID_CARD_RE.finditer(text):
        if id_card_valid(match.group()):
            hits.append(SensitiveHit("id_card", _page_hint(text, match.start()), match.start()))
    for match in _MOBILE_RE.finditer(text):
        hits.append(SensitiveHit("mobile", _page_hint(text, match.start()), match.start()))
    for match in _BANK_CARD_RE.finditer(text):
        candidate = match.group()
        if len(candidate) == 18 and id_card_valid(candidate):
            continue  # 已按身份证命中上报，不再重复计为银行卡
        if luhn_valid(candidate):
            hits.append(SensitiveHit("bank_card", _page_hint(text, match.start()), match.start()))
    hits.sort(key=lambda hit: hit.offset)
    return hits


def _token_hash64(token: str) -> int:
    """定哈希：blake2b 8 字节（进程间确定，内置 hash 受 PYTHONHASHSEED 影响不可用）。"""
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def simhash64(text: str) -> int:
    """64 位 Simhash：字符 2-gram 加权投票，单字符文本退化为单 token，空文本为 0。"""
    counts = [0] * 64
    if len(text) >= 2:
        tokens = (text[i : i + 2] for i in range(len(text) - 1))
    else:
        tokens = iter((text,) if text else ())
    for token in tokens:
        digest = _token_hash64(token)
        for bit in range(64):
            counts[bit] += 1 if (digest >> bit) & 1 else -1
    fingerprint = 0
    for bit in range(64):
        if counts[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming(a: int, b: int) -> int:
    """汉明距离：异或后按位计数（int.bit_count，Python 3.10+）。"""
    return (a ^ b).bit_count()
