r"""F1 异步段只读分析管线：解析→质量红线→逐块清洗→乱码/敏感/simhash→分块→批量 embed。

本模块不做任何事务写库（不变量，供入库状态机 ingest.py 在失败路径直接覆写状态）；
产出 PipelineOutcome（块+向量+降级标注+待确认判定）后由状态机单事务收口。
红线分母为清洗前全文（standards §10.3）；simhash64 为无符号 64 位，与库列 BIGINT
（有符号）的互转在本模块收口。
"""

from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.config import Settings
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

_U64_MASK: int = 0xFFFFFFFFFFFFFFFF  # simhash64 为无符号 64 位；库列 BIGINT 为有符号
_SIGN_BIT: int = 1 << 63


@dataclass
class IngestDeps:
    """异步段依赖包：解析器设置 / 向量器 / 时钟 / 存储，全部可替 stub。"""

    parser_settings: Settings
    embedder: Embedder
    clock: Clock
    storage: FileStorage


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


def to_signed64(value: int) -> int:  # 无符号 64 位指纹 → 有符号 int64（写库前转换）
    return value - (_U64_MASK + 1) if value & _SIGN_BIT else value


def to_unsigned64(value: int) -> int:  # 有符号 int64 → 无符号 64 位指纹（比对前还原）
    return value & _U64_MASK


def analyze_document(session: Session, deps: IngestDeps, doc: Document) -> PipelineOutcome:
    """异步段只读分析：解析→红线→逐块清洗→乱码/敏感/simhash→分块→批量 embed，不写库。"""
    content = (Path(deps.parser_settings.storage_dir) / doc.file_path).read_bytes()
    parsed, engine, note = _parse_with_fallback(
        doc.original_filename, content, deps.parser_settings
    )
    raw = "\n".join(block.text for block in parsed.blocks)  # 清洗前全文＝红线分母（§10.3）
    # 页眉页脚跨块聚合（task-6b 修复轮）：pdf 逐页成块后页眉单块仅 1 次、块内计数
    # 永不达 >=3，先对全文聚合已知页眉行，逐块清洗时经 known_repeated_lines 并集删除。
    headers = repeated_short_lines(raw)
    # 清洗只覆写 text（dataclasses.replace）：parse 阶段打上的 Block.page 原样保留（task-6b）
    cleaned_blocks = [
        replace(block, text=clean_text(block.text, known_repeated_lines=headers))
        for block in parsed.blocks
    ]
    cleaned = "\n".join(block.text for block in cleaned_blocks)
    _check_quality(raw, cleaned)
    fingerprint = simhash64(cleaned)
    chunks = split(ParsedDoc(title=parsed.title, blocks=cleaned_blocks))
    embeddings = deps.embedder.embed([chunk.content for chunk in chunks]) if chunks else []
    return PipelineOutcome(
        chunks=chunks,
        embeddings=embeddings,
        engine=engine,
        degrade_note=note,
        simhash=fingerprint,
        sensitive_hits=len(scan_sensitive(cleaned)),
        near_dup=_find_near_dup(session, doc, fingerprint),
    )


def _parse_with_fallback(
    filename: str, content: bytes, settings: Settings
) -> tuple[ParsedDoc, str, str | None]:
    """解析 + MinerU 故障自动降级：返回（产物, 引擎名, 降级标注）。仅 UpstreamError 降级：
    ApiError（包体非法/页数超限）是文档问题而非服务故障，按失败处理不降级。"""
    parser = get_parser(filename, settings)
    try:
        return parser.parse(filename, content), parser.engine, None
    except UpstreamError:
        note = "MinerU 解析服务不可用，已自动降级 pypdf（降级解析）"
        return PdfFallbackParser().parse(filename, content), "pypdf", note


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
