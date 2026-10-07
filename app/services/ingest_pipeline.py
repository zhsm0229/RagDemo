r"""F1 异步段只读分析管线：解析→质量红线→逐块清洗→乱码/敏感/simhash→分块→批量 embed。

本模块不做任何事务写库（不变量，供入库状态机 ingest.py 在失败路径直接覆写状态）；
产出 PipelineOutcome（块+向量+降级标注+待确认判定）后由状态机单事务收口。红线分母
为清洗前全文（standards §10.3）；simhash64 与库列 BIGINT 的符号互转在本模块收口；
图片 OCR 段与 F-1017 降级标注拼接拆至 ingest_images.py。进度上报：analyze_document
接受可选 progress 回调（阶段, 0-100），由 ingest 层注入落库——本模块只调用不落库。
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
from app.core.convert import DocumentConverter
from app.core.errors import ERR_PARSE_FAILED, ApiError
from app.core.llm import Embedder, UpstreamError
from app.core.storage import FileStorage
from app.db.models import Document
from app.rag.chunker import OutChunk, ParsedDoc, split
from app.rag.clean import RAW_CHARS, clean_text, repeated_short_lines
from app.rag.parser import PdfFallbackParser, get_parser
from app.rag.precheck import (
    GARBLE_THRESHOLD,
    SIMHASH_NEAR,
    garbled_ratio,
    hamming,
    scan_sensitive,
    simhash64,
)
from app.services.ingest_images import ImageAsset, image_degrade_note, ocr_images

logger = logging.getLogger(__name__)

_U64_MASK: int = 0xFFFFFFFFFFFFFFFF  # simhash64 为无符号 64 位；库列 BIGINT 为有符号
_SIGN_BIT: int = 1 << 63


@dataclass
class IngestDeps:
    """异步段依赖包：解析器设置 / 向量器 / 时钟 / 存储 / 老格式转换器，全部可替 stub。

    converter 仅老格式（doc/ppt/xls）使用：None=转换服务未配置（编排层据此给出
    中文失败原因），测试桩场景默认 None。"""

    parser_settings: Settings
    embedder: Embedder
    clock: Clock
    storage: FileStorage
    converter: DocumentConverter | None = None  # 实现只由 core/assembly 装配（findings I5）


@dataclass
class PipelineOutcome:
    """异步段只读分析产物：提交 ready 前的全部结果（含降级标注与待确认判定）。"""

    chunks: list[OutChunk]
    embeddings: list[list[float]]
    engine: str
    degrade_note: str | None
    simhash: int
    sensitive_hits: int
    near_dup: bool
    image_assets: list = field(default_factory=list)  # list[ImageAsset]，ingest_commit 落库
    image_count: int = 0  # 成功落盘图片数
    ocr_chars: int = 0  # OCR 提取字符总数
    images_unparsed: int = 0  # 未解析图片数（抽取跳过+OCR 失败，degrade_note 统计用）


def to_signed64(value: int) -> int:  # 无符号 64 位指纹 → 有符号 int64（写库前转换）
    return value - (_U64_MASK + 1) if value & _SIGN_BIT else value


def to_unsigned64(value: int) -> int:  # 有符号 int64 → 无符号 64 位指纹（比对前还原）
    return value & _U64_MASK


def analyze_document(
    session: Session,
    deps: IngestDeps,
    doc: Document,
    progress: Callable[[str, int], None] | None = None,
    parse_filename: str | None = None,
) -> PipelineOutcome:
    """异步段只读分析：解析→红线→逐块清洗→乱码/敏感/simhash→分块→分批 embed，不写库。

    progress（可空）在阶段推进点回调 (阶段文案, 0-100)：调用方（ingest 层）负责落库，
    本模块不写库——回调里的异常一律向上抛（落库失败=解析失败，宁可重试不可静默丢进度）。
    parse_filename：老格式转换后以新扩展名分派解析器（docx 等）；缺省用 original_filename。"""
    parse_filename = parse_filename or doc.original_filename
    started = time.monotonic()
    _report(progress, "解析中", 5)
    content = (Path(deps.parser_settings.storage_dir) / doc.file_path).read_bytes()
    parsed, engine, note = _parse_with_fallback(parse_filename, content, deps.parser_settings)
    # 内嵌图片 OCR（F-1017）：开关开启且解析器抽到图片时逐图 OCR，"图片N："补充块与
    # 落盘细节见 ingest_images.py（本模块保持只读分析不变量）。
    image_assets, ocr_chars_total, images_unparsed = _ocr_stage(deps, doc, parsed, progress)
    _report(progress, "清洗分块中", 25)
    chunks, cleaned, fingerprint = _clean_and_chunk(parsed)
    logger.info(
        "解析完成 doc_id=%s engine=%s 块=%d 清洗后=%d字 耗时=%.1fs",
        doc.id,
        engine,
        len(chunks),
        len(cleaned),
        time.monotonic() - started,
    )
    embeddings = _embed_stage(deps, doc.id, chunks, progress, started)
    note = image_degrade_note(note, images_unparsed)
    return PipelineOutcome(
        chunks=chunks,
        embeddings=embeddings,
        engine=engine,
        degrade_note=note,
        simhash=fingerprint,
        sensitive_hits=len(scan_sensitive(cleaned)),
        near_dup=_find_near_dup(session, doc, fingerprint),
        image_assets=image_assets,
        image_count=len(image_assets),
        ocr_chars=ocr_chars_total,
        images_unparsed=images_unparsed,
    )


def _ocr_stage(
    deps: IngestDeps, doc: Document, parsed: ParsedDoc, progress: Callable[[str, int], None] | None
) -> tuple[list[ImageAsset], int, int]:
    """图片 OCR 段（F-1017）：返回 (资产清单, OCR 字符总数, 未解析数——含抽取期跳过)。

    单图失败跳过计数不阻塞（标注拼接见 image_degrade_note）；未配置 MinerU 视同全跳。"""
    images_unparsed = parsed.image_skipped  # 抽取期已跳过（超限/矢量/不支持格式）
    if not (deps.parser_settings.image_ocr_enabled and parsed.images):
        return [], 0, images_unparsed
    _report(progress, "图片识别中", 12)
    ocr = ocr_images(deps.parser_settings, doc.id, parsed)
    return ocr.assets, ocr.ocr_chars, images_unparsed + ocr.unparsed


def _embed_stage(
    deps: IngestDeps,
    doc_id: int,
    chunks: list[OutChunk],
    progress: Callable[[str, int], None] | None,
    started: float,
) -> list[list[float]]:
    """向量化段（批次切分与预算见 _embed_in_batches）：上报进度并记录总耗时日志。"""
    _report(progress, "向量化中", 45)
    embeddings = _embed_in_batches(deps, doc_id, chunks, progress)
    logger.info(
        "向量化完成 doc_id=%s chunks=%d 总耗时=%.1fs",
        doc_id,
        len(chunks),
        time.monotonic() - started,
    )
    return embeddings


def _clean_and_chunk(parsed: ParsedDoc) -> tuple[list[OutChunk], str, int]:
    """清洗+红线+分块（§10.3：红线分母=清洗前全文）：返回 (chunks, 清洗后全文, simhash64)。

    页眉页脚跨块聚合（task-6b 修复轮）：pdf 逐页成块后页眉单块仅 1 次、块内计数
    永不达 >=3，先对全文聚合已知页眉行，逐块清洗时经 known_repeated_lines 并集删除。
    清洗只覆写 text（dataclasses.replace）：parse 阶段打上的 Block.page 原样保留（task-6b）。"""
    raw = "\n".join(block.text for block in parsed.blocks)
    headers = repeated_short_lines(raw)
    cleaned_blocks = [
        replace(block, text=clean_text(block.text, known_repeated_lines=headers))
        for block in parsed.blocks
    ]
    cleaned = "\n".join(block.text for block in cleaned_blocks)
    _check_quality(raw, cleaned)
    return split(ParsedDoc(title=parsed.title, blocks=cleaned_blocks)), cleaned, simhash64(cleaned)


def _report(progress: Callable[[str, int], None] | None, stage: str, percent: int) -> None:
    if progress is not None:
        progress(stage, percent)


def _embed_in_batches(
    deps: IngestDeps,
    doc_id: int,
    chunks: list[OutChunk],
    progress: Callable[[str, int], None] | None,
) -> list[list[float]]:
    """分批嵌入：TEI --max-batch-tokens 2048（防 OOM 特意调低）下单请求超限即 422——
    2026-10-02 大文档全量单请求全败根因。按字符预算切批（BGE tokenizer 中文≈1字1token，
    预算留 40% 余量），逐批串行调用（TEI 单并发最稳），批间上报进度。"""
    texts = [chunk.content for chunk in chunks]
    if not texts:
        return []
    batch_chars = deps.parser_settings.embed_batch_chars
    batches = _split_by_chars(texts, batch_chars)
    logger.info(
        "向量化开始 doc_id=%s chunks=%d 批=%d 预算=%d字/批",
        doc_id,
        len(texts),
        len(batches),
        batch_chars,
    )
    vectors: list[list[float]] = []
    started = time.monotonic()
    for i, batch in enumerate(batches, 1):
        vectors.extend(deps.embedder.embed(batch))
        if progress is not None:
            progress("向量化中", 45 + int(50 * i / len(batches)))
    logger.info(
        "向量化批次完成 doc_id=%s 批=%d 耗时=%.1fs",
        doc_id,
        len(batches),
        time.monotonic() - started,
    )
    return vectors


def _split_by_chars(texts: list[str], budget: int) -> list[list[str]]:
    """按字符预算切批：超长单条独立成批（不截断内容——截断会丢向量语义）。
    budget<=0 = 不分批（仅测试桩场景）。"""
    if budget <= 0:
        return [texts]
    batches: list[list[str]] = []
    current: list[str] = []
    used = 0
    for text in texts:
        length = len(text)
        if current and used + length > budget:
            batches.append(current)
            current, used = [], 0
        current.append(text)
        used += length
    if current:
        batches.append(current)
    return batches


def _parse_with_fallback(
    filename: str, content: bytes, settings: Settings
) -> tuple[ParsedDoc, str, str | None]:
    """解析 + 场景路由 + MinerU 故障自动降级。

    路由（2026-10-05 试点反馈：文字版 pdf 在 CPU 版 MinerU 上 2-10 分钟/份）：pdf 且
    配置了 MinerU 时先做文字层预判——文字版直接走 pypdf（秒级，内容等价，损失版面/
    表格结构）；扫描件（无文字层）才需要 MinerU OCR。仅 UpstreamError 降级 pypdf：
    ApiError（包体非法/页数超限）是文档问题而非服务故障，按失败处理不降级。"""
    parser = get_parser(filename, settings)
    if parser.engine == "mineru" and _is_text_based_pdf(content):
        logger.info("文字版 pdf 预判命中：跳过 MinerU 走 pypdf 快速解析 filename=%s", filename)
        parser = PdfFallbackParser()
    try:
        return parser.parse(filename, content), parser.engine, None
    except UpstreamError:
        note = "MinerU 解析服务不可用，已自动降级 pypdf（降级解析）"
        return PdfFallbackParser().parse(filename, content), "pypdf", note


_PDF_PROBE_PAGES = 3
_PDF_PROBE_MIN_CHARS = 200  # 前 3 页有效文字 ≥200 字符视为文字版（扫描件 OCR 前文字层近空）


def _is_text_based_pdf(content: bytes) -> bool:
    """文字版预判：pypdf 提取前 3 页文字，有效字符 ≥200 即文字版。探测失败（加密/
    损坏）按扫描件处理交给 MinerU（OCR 兼容性更强，失败再走既有降级链）。"""
    try:
        reader = PdfReader(BytesIO(content))
        text = "".join((page.extract_text() or "") for page in reader.pages[:_PDF_PROBE_PAGES])
        return len(text.strip()) >= _PDF_PROBE_MIN_CHARS
    except Exception:  # noqa: BLE001 — 预判失败不影响主流程：交给 MinerU 兜底
        return False


def _check_quality(raw: str, cleaned: str) -> None:
    """质量红线（§10.3）+ 乱码率（F-1005）：不达线即抛 ApiError 中文原因，由编排转 failed。"""
    if len(cleaned) < max(100, RAW_CHARS(raw) * 0.1):
        raise ApiError(ERR_PARSE_FAILED, "疑似扫描件或空文档，有效内容不足", 422)
    ratio = garbled_ratio(cleaned)
    if ratio > GARBLE_THRESHOLD:
        raise ApiError(ERR_PARSE_FAILED, f"编码异常：乱码字符占比 {ratio:.1%} 超过阈值", 422)


def _find_near_dup(session: Session, doc: Document, fingerprint: int) -> bool:
    """F-1002：与同部门、异标题（同标题属版本关系非重复）、未软删且已有指纹的文档比对。"""
    candidates = session.scalars(
        select(Document.simhash).where(
            Document.dept_id == doc.dept_id,
            Document.title != doc.title,
            Document.id != doc.id,
            Document.deleted_at.is_(None),
            Document.simhash.is_not(None),
        )
    ).all()
    return any(hamming(fingerprint, to_unsigned64(other)) <= SIMHASH_NEAR for other in candidates)
