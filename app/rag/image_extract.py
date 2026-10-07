r"""文档内嵌图片抽取（F-1017）：从各格式解析产物中提取图片字节，统一闸门。

设计：各解析器只负责"收集图片字节"，闸门与归一化集中在本模块——单图 ≤10MB、
单文档 ≤50 张、总量 ≤100MB、仅位图（png/jpg/jpeg/bmp/webp/gif；emf/wmf 矢量图
MinerU 不支持，跳过并计数）。抽取的图片**先落盘、OCR 后入库**（见 ingest_pipeline），
落盘失败与 OCR 失败同语义：跳过该图并计数，不阻塞文档。
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES: int = 10 * 1024 * 1024  # 单图 10MB
MAX_IMAGES_PER_DOC: int = 50  # 单文档张数上限
MAX_TOTAL_IMAGE_BYTES: int = 100 * 1024 * 1024  # 单文档图片总量上限
SUPPORTED_MIMES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "gif": "image/gif",
}
# Office 矢量图（MinerU 不支持），命中即跳过并计入 skipped
_VECTOR_EXTS: frozenset[str] = frozenset({"emf", "wmf"})


@dataclass
class ExtractedImage:
    """一张内嵌图片：原始字节 + 所属页（docx/表类为 None）+ 扩展名（小写无点）。"""

    data: bytes
    page: int | None
    ext: str

    @property
    def mime(self) -> str:
        return SUPPORTED_MIMES.get(self.ext, "application/octet-stream")


@dataclass
class ExtractionResult:
    """抽取产物：通过闸门的图片 + 各类跳过计数（degrade_note 统计用）。"""

    images: list[ExtractedImage]
    skipped_oversize: int = 0
    skipped_vector: int = 0
    skipped_total_limit: int = 0

    @property
    def skipped(self) -> int:
        """抽取期跳过总数（超限+矢量/不支持+总量限），degrade 统计用。"""
        return self.skipped_oversize + self.skipped_vector + self.skipped_total_limit


def new_result() -> ExtractionResult:
    return ExtractionResult(images=[])


def _admit(result: ExtractionResult, data: bytes, page: int | None, ext: str) -> None:
    """闸门判定（集中一处）：格式 → 单图体积 → 张数 → 总量；跳过均计数不抛错。"""
    ext = ext.lower().lstrip(".")
    if ext in _VECTOR_EXTS:
        result.skipped_vector += 1
        return
    if ext not in SUPPORTED_MIMES:
        result.skipped_vector += 1  # 未知/不支持格式与矢量图同口径：跳过计数
        return
    if len(data) > MAX_IMAGE_BYTES:
        result.skipped_oversize += 1
        return
    if len(result.images) >= MAX_IMAGES_PER_DOC:
        result.skipped_total_limit += 1
        return
    if sum(len(img.data) for img in result.images) + len(data) > MAX_TOTAL_IMAGE_BYTES:
        result.skipped_total_limit += 1
        return
    result.images.append(ExtractedImage(data=data, page=page, ext=ext))


def collect_docx_images(document) -> ExtractionResult:
    """docx：遍历 part.rels 全部 image 部件（覆盖 inline/浮动/页眉图）。"""
    result = new_result()
    for rel in document.part.rels.values():
        if "image" not in rel.reltype:
            continue
        part = rel.target_part
        ext = part.partname.ext.lstrip(".") if hasattr(part.partname, "ext") else ""
        _admit(result, part.blob, None, ext)
    return result


def collect_pptx_images(slide, page: int) -> ExtractionResult:
    """pptx：单页内 picture shape 的 shape.image.blob。"""
    result = new_result()
    for shape in slide.shapes:
        image = getattr(shape, "image", None)  # Picture 形才有；占位符等返回 None
        if image is None:
            continue
        _admit(result, image.blob, page, image.ext)
    return result


def collect_ooxml_media_images(content: bytes, prefix: str) -> ExtractionResult:
    """从 OOXML zip 的 media/ 目录直取图片字节（xl/、ppt/ 通用）。

    为什么不走 openpyxl：重载后 ws._images 的 Image.image 为 None（openpyxl 加载
    时不解码图片字节，实测 2026-10-06）——直接读包内 xl/media/* 最可靠。"""
    result = new_result()
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in archive.namelist():
                if not name.startswith(prefix):
                    continue
                ext = name.rsplit(".", 1)[-1] if "." in name else ""
                _admit(result, archive.read(name), None, ext)
    except zipfile.BadZipFile:
        pass  # 包损坏由解析器报中文原因
    return result


def collect_pdf_page_images(page, page_no: int) -> ExtractionResult:
    """pdf：pypdf PageObject.images（XObject 位图；InlineImage 由 pypdf 归入同列表）。"""
    result = new_result()
    try:
        for image_file in page.images:
            name = getattr(image_file, "name", "") or "image"
            ext = name.rsplit(".", 1)[-1] if "." in name else "png"
            _admit(result, image_file.data, page_no, ext)
    except Exception as exc:  # noqa: BLE001 —— 图片枚举失败不阻塞文本解析（降级语义）
        logger.warning("pdf 图片枚举失败 page=%s: %s", page_no, exc)
    return result
