r"""预检纯函数测试：同步闸门 + 敏感信息/乱码/近似查重算法层（standards §10.8）。

断言表来源 task-8-brief（test-cases DOC-03/04/06/08/09、SEC-06/07）：白名单扩展名
+ MIME + 魔数（%PDF / PK\x03\x04）+ 50MB 上限 + SHA-256 稳定；乱码占比（控制字符
+ U+FFFD，\n\r\t 版面字符豁免，阈值 0.05）；身份证 18 位含加权校验码、手机号 1[3-9] 定长 11 位、银行卡
13-19 位 Luhn——校验码/校验和均为手算硬断言（注释给出计算过程）；10 万字符超长
输入 1 秒内返回（ReDoS 防护，time.monotonic 计时）；simhash64 近似文本
hamming <= 3、无关文本 > 3。
"""

import asyncio
import hashlib
import re
import time

import pytest

from app.rag.precheck import (
    GARBLE_THRESHOLD,
    SIMHASH_NEAR,
    SensitiveHit,
    SyncResult,
    check_async,
    check_sync,
    garbled_ratio,
    hamming,
    id_card_valid,
    luhn_valid,
    scan_sensitive,
    simhash64,
)

_MB = 1024 * 1024
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_ID = "11010519491231002X"
_MOBILE = "13812345678"
_CARD = "4242424242424242"


# ---------------------------------------------------------------------------
# check_sync：白名单扩展名 + MIME + 魔数 + 50MB + SHA-256
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "mime", "content", "reason"),
    [
        pytest.param(
            "invoice.pdf",
            "application/pdf",
            b"MZ\x90\x00" + b"\x00" * 16,
            "magic_mismatch",
            id="doc-neg-exe-bytes-renamed-pdf-magic-mismatch",
        ),
        pytest.param(
            "big.pdf",
            "application/pdf",
            b"\x00" * (51 * _MB),
            "too_large",
            id="doc-neg-51mb-over-limit",
        ),
        pytest.param(
            "virus.exe",
            "application/pdf",
            b"%PDF-1.7\n",
            "ext_not_allowed",
            id="doc-neg-exe-extension-not-whitelisted",
        ),
        pytest.param(
            "a.pdf",
            "application/zip",
            b"%PDF-1.7\n",
            "mime_not_allowed",
            id="doc-neg-mime-not-whitelisted",
        ),
        pytest.param(
            "noext",
            "application/pdf",
            b"%PDF-1.7\n",
            "ext_not_allowed",
            id="doc-neg-missing-extension",
        ),
    ],
)
def test_check_sync_rejects(filename: str, mime: str, content: bytes, reason: str) -> None:
    result = check_sync(filename, mime, content)
    assert result.ok is False
    assert result.reason == reason


def test_check_sync_accepts_pdf_and_sha256_stable_across_saves() -> None:
    """合法 pdf 头两次保存：结果一致、sha256 为 64 位小写 hex 且等于 SHA-256(content)。"""
    content = b"%PDF-1.7\n%%EOF\n"
    first = check_sync("doc.pdf", "application/pdf", content)
    second = check_sync("doc.pdf", "application/pdf", content)
    assert first == SyncResult(ok=True, reason=None, sha256=hashlib.sha256(content).hexdigest())
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{64}", first.sha256) is not None


def test_check_sync_accepts_docx_magic_and_50mb_boundary() -> None:
    docx = check_sync("doc.docx", DOCX_MIME, b"PK\x03\x04" + b"\x00" * 16)
    assert docx.ok is True
    assert docx.reason is None
    boundary = check_sync("edge.pdf", "application/pdf", b"%PDF" + b"\x00" * (50 * _MB - 4))
    assert boundary.ok is True  # 恰好 50MB：<= 上限，放行


def test_check_async_matches_sync() -> None:
    content = b"%PDF-1.4\n"
    assert asyncio.run(check_async("a.pdf", "application/pdf", content)) == check_sync(
        "a.pdf", "application/pdf", content
    )


# ---------------------------------------------------------------------------
# garbled_ratio：控制字符 + U+FFFD 占比（\n\r\t 版面字符豁免），阈值 GARBLE_THRESHOLD=0.05
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("正常中文", 0.0, id="garble-pos-plain-chinese-zero"),
        # "\ufffdf\ufffdf\ufffd"：5 字符中 3 个 U+FFFD → 3/5 = 0.6，超过半数
        pytest.param("\ufffdf\ufffdf\ufffd", 0.6, id="garble-pos-fffd-majority-over-threshold"),
        pytest.param("\x00\x01\x02", 1.0, id="garble-pos-all-control-chars"),
        # "正文\x07b"：4 字符中 1 个控制字符 BEL → 1/4 = 0.25
        pytest.param("正文\x07b", 0.25, id="garble-pos-bell-char-quarter"),
        pytest.param("", 0.0, id="garble-neg-empty-text-zero"),
    ],
)
def test_garbled_ratio(text: str, expected: float) -> None:
    ratio = garbled_ratio(text)
    assert ratio == pytest.approx(expected)
    if expected > GARBLE_THRESHOLD:
        assert ratio > GARBLE_THRESHOLD


def test_garbled_ratio_short_line_doc_below_threshold() -> None:
    """\\n 豁免（Task 8 交接闭环）：50 行×14 字的短行密集文档，49 个换行不计乱码。

    旧行为（\\n 计入分子）下 49/749 ≈ 6.5% 会误超 5% 阈值；豁免后为 0.0。
    """
    lines = [f"第{i:02d}条 制度条款内容{i:02d}。" for i in range(50)]
    assert all(len(line) < 20 for line in lines)
    text = "\n".join(lines)
    assert len(text) == 749  # 50×14 正文 + 49 个 \n
    assert garbled_ratio(text) == 0.0
    assert garbled_ratio(text) < GARBLE_THRESHOLD


def test_garbled_ratio_real_garble_still_over_threshold_with_newlines() -> None:
    """真乱码不因换行豁免而漏报：C1 控制区 + U+FFFD 密集混排，占比远超阈值。"""
    text = "\x7f\x80\ufffd\n" * 20  # 每行 3 个乱码字符 + 1 个换行
    assert garbled_ratio(text) == pytest.approx(60 / 80)
    assert garbled_ratio(text) > GARBLE_THRESHOLD


def test_threshold_constants_match_brief() -> None:
    assert GARBLE_THRESHOLD == 0.05
    assert SIMHASH_NEAR == 3


# ---------------------------------------------------------------------------
# id_card_valid：18 位加权校验码（权重/映射手算硬断言）
# 算法：check = map(Σ(前 17 位 × 权重) mod 11)，权重 [7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2]，
# 映射 "10X98765432"（索引 0..10 → '1','0','X','9','8','7','6','5','4','3','2'）。
# "11010519491231002"：Σ = 7+9+0+5+0+20+2+9+24+27+7+18+30+5+0+0+4 = 167，167 mod 11 = 2 → 'X'
# "11010519900101001"：Σ = 7+9+0+5+0+20+2+9+54+0+0+9+0+5+0+0+2 = 122，122 mod 11 = 1 → '0'
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        pytest.param("11010519491231002X", True, id="id-pos-checksum-x"),
        pytest.param("110105199001010010", True, id="id-pos-checksum-0"),
        pytest.param("11010519491231002x", True, id="id-pos-lowercase-x-accepted"),
        pytest.param("110105194912310021", False, id="id-neg-wrong-checksum-1-vs-x"),
        pytest.param("110105199001010011", False, id="id-neg-wrong-checksum-1-vs-0"),
        pytest.param("1101051949123100", False, id="id-neg-too-short-16-digits"),
        pytest.param("11010519491231002XY", False, id="id-neg-too-long-19-chars"),
        pytest.param("11010519491231002A", False, id="id-neg-last-char-not-digit-or-x"),
        pytest.param("", False, id="id-neg-empty"),
    ],
)
def test_id_card_valid(number: str, expected: bool) -> None:
    assert id_card_valid(number) is expected


# ---------------------------------------------------------------------------
# luhn_valid：从右起奇位直接累加、偶位翻倍（>9 减 9），总和 mod 10 == 0
# "4242424242424242"：奇位（自右，含校验位）和 = 2×8 = 16；偶位翻倍后 4×2=8×8 = 64；
#                     合计 16 + 64 = 80，80 mod 10 = 0 → 合法
# "79927398713"：奇位和 3+7+9+7+9+7 = 42；偶位翻倍 2+7+6+4+9 = 28；合计 42 + 28 = 70 → 合法
# "4242424242424241"：合计 79 → 非法；"79927398714"：合计 71 → 非法
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        pytest.param("4242424242424242", True, id="luhn-pos-16-digit-test-card"),
        pytest.param("79927398713", True, id="luhn-pos-classic-11-digit"),
        pytest.param("4242424242424241", False, id="luhn-neg-last-digit-off-by-one"),
        pytest.param("79927398714", False, id="luhn-neg-check-digit-wrong"),
        pytest.param("424242424242424a", False, id="luhn-neg-non-digit-char"),
        pytest.param("1", False, id="luhn-neg-single-digit"),
        pytest.param("", False, id="luhn-neg-empty"),
    ],
)
def test_luhn_valid(number: str, expected: bool) -> None:
    assert luhn_valid(number) is expected


# ---------------------------------------------------------------------------
# scan_sensitive：身份证（含校验位）/手机号/银行卡，命中按 offset 升序
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_kinds"),
    [
        pytest.param(f"档案编号{_ID}已归档", ["id_card"], id="scan-pos-id-card-hit"),
        pytest.param("110105194912310021", [], id="scan-neg-id-checksum-wrong-no-hit"),
        pytest.param(f"联系电话{_MOBILE}", ["mobile"], id="scan-pos-mobile-hit"),
        pytest.param(f"卡号{_CARD}请核对", ["bank_card"], id="scan-pos-bank-card-hit"),
        pytest.param("无敏感信息的普通正文。", [], id="scan-neg-plain-text"),
        # 27 位连续数字：手机号/银行卡子串均被数字边界否定（防内嵌误报，线性回溯有界）
        pytest.param(
            "1381234567842424242424242", [], id="scan-neg-digit-run-too-long-no-inner-match"
        ),
        pytest.param(
            f"{_ID}，{_MOBILE}，{_CARD}",
            ["id_card", "mobile", "bank_card"],
            id="scan-pos-mixed-sorted-by-offset",
        ),
    ],
)
def test_scan_sensitive_kinds(text: str, expected_kinds: list[str]) -> None:
    assert [hit.kind for hit in scan_sensitive(text)] == expected_kinds


def test_scan_sensitive_offsets_and_page_hints() -> None:
    """offset 为字符偏移；page_hint 按 \\f（换页符）计数，首页为 1。"""
    text = f"第一页身份证{_ID}" + "\f" + f"第二页手机{_MOBILE}"
    hits = scan_sensitive(text)
    # "第一页身份证" 6 字符 → 身份证 offset=6、页 1；跨 1 个 \f 后 "第二页手机" 5 字符
    # → 手机号 offset = 6 + 18 + 1 + 5 = 30、页 2
    assert hits == [
        SensitiveHit(kind="id_card", page_hint=1, offset=6),
        SensitiveHit(kind="mobile", page_hint=2, offset=30),
    ]


@pytest.mark.parametrize(
    "huge",
    [
        pytest.param("a" * 100_000 + "身份证", id="perf-100k-letters-returns-within-1s"),
        pytest.param("1" * 100_000, id="perf-100k-digit-run-bounded-backtrack"),
    ],
)
def test_scan_sensitive_huge_input_returns_within_1s(huge: str) -> None:
    """SEC-07：超长输入限时完成不挂死（无嵌套量词，线性扫描）。"""
    start = time.monotonic()
    hits = scan_sensitive(huge)
    elapsed = time.monotonic() - start
    assert hits == []
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# simhash64 / hamming：近似查重
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        pytest.param(0, 0, 0, id="hamming-zero"),
        pytest.param(0, 1, 1, id="hamming-single-bit"),
        pytest.param(0b1010, 0b0110, 2, id="hamming-two-bits"),
        pytest.param(2**64 - 1, 0, 64, id="hamming-all-64-bits"),
    ],
)
def test_hamming_exact(a: int, b: int, expected: int) -> None:
    assert hamming(a, b) == expected


_BASE = (
    "企业智能工作台提供文档上传解析清洗分块与向量检索能力，支持多格式文件批量导入，"
    "并针对敏感信息乱码内容执行两段式预检，保证入库语料质量与检索体验稳定，"
    "为后续检索问答与语料治理提供统一的数据底座和质量基线，"
    "预检通过后文档进入解析清洗与分块流水线，最终写入向量库等待检索。"
)
# 相近文本：中部插入一个词——字符 2-gram 的局部扰动被 64 列多数投票吸收
_NEAR = _BASE[:66] + "严格" + _BASE[66:]
_FAR = "今日午后风力减弱气温回升适宜户外运动，傍晚前后西部山区有零星降雨，出行请注意交通安全。"


def test_simhash64_near_duplicate_within_threshold() -> None:
    assert hamming(simhash64(_BASE), simhash64(_NEAR)) <= SIMHASH_NEAR


def test_simhash64_unrelated_texts_above_threshold() -> None:
    assert hamming(simhash64(_BASE), simhash64(_FAR)) > SIMHASH_NEAR


def test_simhash64_deterministic_and_64bit() -> None:
    """哈希用 blake2b（进程间确定），指纹落在 64 位无符号范围。"""
    fingerprint = simhash64(_BASE)
    assert fingerprint == simhash64(_BASE)
    assert 0 <= fingerprint < 2**64
