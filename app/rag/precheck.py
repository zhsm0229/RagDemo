r"""预检纯函数：两段式预检的第一段闸门与敏感信息/乱码/近似查重算法层（standards §10.8）。

check_sync 做白名单闸门：扩展名（.pdf/.docx）→ MIME → 大小（<= 50MB）→ 魔数
（%PDF / PK\x03\x04），全过才放行；SHA-256 无条件计算（拒收样本也留审计指纹），
失败返回稳定 reason 码。敏感扫描全部用预编译线性正则（定长或 [0-9]{13,19} 有界
量词，无嵌套量词、回溯有界，SEC-07），命中后再过纯函数校验：身份证 18 位加权
校验码（id_card_valid）、银行卡 Luhn（luhn_valid）。乱码占比 = 控制字符
（U+0000–001F、U+007F–009F，即 Unicode Cc）与 U+FFFD 的字符占比。simhash64 为
64 位 Simhash（字符 2-gram + blake2b 8 字节指纹，进程间确定），hamming 为汉明
距离；GARBLE_THRESHOLD / SIMHASH_NEAR 为下游判定阈值常量。
"""

import hashlib
import re
from pathlib import PureWindowsPath
from typing import NamedTuple

ALLOWED_EXTS: frozenset[str] = frozenset({".pdf", ".docx"})
ALLOWED_MIMES: frozenset[str] = frozenset(
    {"application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
)
MAX_SYNC_BYTES: int = 50 * 1024 * 1024
MAGIC_BY_EXT: dict[str, bytes] = {".pdf": b"%PDF", ".docx": b"PK\x03\x04"}
GARBLE_THRESHOLD: float = 0.05
SIMHASH_NEAR: int = 3

# 全部预编译为模块常量；定长/有界量词 + 数字边界环视，回溯有界（SEC-07 ReDoS 防护）
_ID_CARD_RE = re.compile(r"(?<![0-9Xx])[0-9]{17}[0-9Xx](?![0-9Xx])")
_MOBILE_RE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
_BANK_CARD_RE = re.compile(r"(?<![0-9])[0-9]{13,19}(?![0-9])")
# 控制字符 = Unicode Cc：U+0000–001F 与 U+007F–009F，外加替换符 U+FFFD
_GARBLE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ufffd]")

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
    """同步预检闸门：扩展名 → MIME → 大小 → 魔数，按序短路，失败给稳定 reason 码。"""
    sha256 = hashlib.sha256(content).hexdigest()
    ext = PureWindowsPath(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return SyncResult(False, "ext_not_allowed", sha256)
    if mime not in ALLOWED_MIMES:
        return SyncResult(False, "mime_not_allowed", sha256)
    if len(content) > MAX_SYNC_BYTES:
        return SyncResult(False, "too_large", sha256)
    if not content.startswith(MAGIC_BY_EXT[ext]):
        return SyncResult(False, "magic_mismatch", sha256)
    return SyncResult(True, None, sha256)


async def check_async(filename: str, mime: str, content: bytes) -> SyncResult:
    """异步包装：纯函数无 IO，签名与 check_sync 一致，供异步上传链路 await。"""
    return check_sync(filename, mime, content)


def garbled_ratio(text: str) -> float:
    """乱码占比 = (控制字符 + U+FFFD) 字符数 / 总字符数；空文本返回 0.0。"""
    if not text:
        return 0.0
    return len(_GARBLE_RE.findall(text)) / len(text)


def _page_hint(text: str, offset: int) -> int:
    """页码提示：解析器以 \f（换页符）分页，命中前的 \f 计数 + 1。"""
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
