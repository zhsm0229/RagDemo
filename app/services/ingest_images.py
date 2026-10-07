r"""内嵌图片 OCR 落地段（F-1017，自 ingest_pipeline.analyze_document 抽出，行数红线 §1.4）。

开关开启且解析器抽到图片时：逐图 OCR → 以"图片N："补充块并入 parsed.blocks（heading_path
挂到最后既有节，无节用标题）→ 图片文件落盘 images/{doc_id}/ 供回显端点。失败降级：单图
失败跳过计数不阻塞——纯文本正常入库 + degrade_note 标注（images_unparsed 统计口径）。
ImageAsset 为图片资产载体（ingest_commit 落 document_images 行用）；本模块只改内存与
落盘，不写库（沿用 ingest_pipeline"只读分析"不变量）。
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.llm import UpstreamError
from app.rag.chunker import Block, ParsedDoc
from app.services.ocr import ImageOcrClient

logger = logging.getLogger(__name__)


@dataclass
class ImageAsset:
    """一张成功落盘的内嵌图片：ingest_commit 写 document_images 行用。"""

    index: int
    rel_path: str
    mime: str
    page: int | None
    ocr_chars: int


@dataclass
class ImageOcrOutcome:
    """图片段产物：资产清单 + OCR 字符总数 + 本段新增未解析数（抽取期跳过数由调用方另计）。"""

    assets: list[ImageAsset]
    ocr_chars: int
    unparsed: int


_IMG_UNPARSED_NOTE = "{} 张图片未解析（OCR 不可用或格式不支持），图片文字未参与检索"


def image_degrade_note(note: str | None, images_unparsed: int) -> str | None:
    """F-1017 降级标注：有图片未解析即在原降级标注后追加计数说明（degrade_note 统计口径）。"""
    if not images_unparsed:
        return note
    prefix = f"{note}；" if note else ""
    return prefix + _IMG_UNPARSED_NOTE.format(images_unparsed)


def ocr_images(settings: Settings, doc_id: int, parsed: ParsedDoc) -> ImageOcrOutcome:
    """逐图 OCR 并以"图片N："补充块并入 parsed.blocks（原地追加，块序在全部正文之后）。

    进度上报（"图片识别中"档位）归编排层 analyze_document，本函数不持进度职责。"""
    assets: list[ImageAsset] = []
    ocr_chars_total = 0
    unparsed = 0
    ocr_client = (
        ImageOcrClient(settings.mineru_base_url, settings.mineru_timeout_s)
        if settings.mineru_base_url
        else None
    )
    last_path = list(parsed.blocks[-1].heading_path) if parsed.blocks else [parsed.title]
    last_level = parsed.blocks[-1].level if parsed.blocks else 1
    images_dir = Path(settings.storage_dir) / "images" / str(doc_id)
    images_dir.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(parsed.images, 1):
        text = ""
        if ocr_client is not None:
            try:
                text = ocr_client.ocr(image.data, f"img_{index}.{image.ext}")
            except UpstreamError as exc:
                unparsed += 1
                logger.warning("图片 OCR 失败 doc_id=%s img=%s: %s", doc_id, index, exc)
        else:
            unparsed += 1  # 开关开启但 MinerU 未配置
        rel_path = f"images/{doc_id}/img_{index}.{image.ext}"
        (images_dir / f"img_{index}.{image.ext}").write_bytes(image.data)
        assets.append(ImageAsset(index, rel_path, image.mime, image.page, len(text)))
        ocr_chars_total += len(text)
        if text:
            parsed.blocks.append(
                Block(level=last_level, heading_path=last_path, text=f"图片{index}：{text}")
            )
    logger.info(
        "图片 OCR 完成 doc_id=%d 抽取=%d 未解析=%d 字=%d",
        doc_id,
        len(parsed.images),
        unparsed,
        ocr_chars_total,
    )
    return ImageOcrOutcome(assets=assets, ocr_chars=ocr_chars_total, unparsed=unparsed)
