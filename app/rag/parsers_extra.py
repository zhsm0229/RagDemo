r"""扩展格式解析器（F-1013）：md / txt / csv / xlsx / pptx / html → 标准 ParsedDoc。

设计约定（与 DocxParser 对齐）：
- 产出 Block(level, heading_path, text, page?)，标题信息进 heading_path（chunker 拼
  title_path 前缀进向量）；无标题文档用文件名 stem 作根路径（避免空路径前缀）。
- **表格/幻灯片文本一律"行内空格连接"**——独立短行会被清洗规则误删（重复短行 ≥3 次、
  纯数字整行），行内连接从源头规避（2026-10-05 xlsx 接入评估）。
- 输出上限 200 万字符（与 DocxParser 同口径），超限截断并保留已解析内容。
- html 用内置 html.parser（convert_charrefs=True）：字符引用被转为纯文本、自定义实体
  声明不会被展开——无 XML 实体注入面；script/style 内容跳过。html 的 <!DOCTYPE html>
  是标准声明而非 XML DOCTYPE，不做拒绝（安全语义见类注释）。
"""

import re
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

from app.rag.chunker import Block, ParsedDoc

_MAX_OUTPUT_CHARS: int = 2_000_000
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SKIP_TAGS = frozenset({"script", "style"})
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}


def _stem(filename: str) -> str:
    return Path(filename).stem


def _cap(blocks: list[Block]) -> list[Block]:
    """输出上限保护：累计超限即截断该块并丢弃后续（与 DocxParser 输出上限同口径）。"""
    total = 0
    capped: list[Block] = []
    for block in blocks:
        if total >= _MAX_OUTPUT_CHARS:
            break
        remaining = _MAX_OUTPUT_CHARS - total
        capped.append(
            block if len(block.text) <= remaining else replace_text(block, block.text[:remaining])
        )
        total += len(capped[-1].text)
    return capped


def replace_text(block: Block, text: str) -> Block:
    return Block(level=block.level, heading_path=block.heading_path, text=text, page=block.page)


class MarkdownParser:
    """Markdown：#/##/### 标题层级切节；代码块围栏内的 # 视为内容不切节。"""

    engine = "markdown"

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        stem = _stem(filename)
        blocks: list[Block] = []
        path: list[str] = []
        level = 0
        para: list[str] = []
        in_code = False

        def flush() -> None:
            text = "\n".join(para).strip("\n")
            para.clear()
            if text.strip():
                blocks.append(Block(level=max(level, 1), heading_path=path or [stem], text=text))

        for line in content.decode("utf-8", "ignore").splitlines():
            if line.lstrip().startswith("```"):
                in_code = not in_code
                para.append(line)
                continue
            if in_code:
                para.append(line)
                continue
            heading = _HEADING_RE.match(line)
            if heading:
                flush()
                level = len(heading.group(1))
                path = [*path[: level - 1], heading.group(2)]
                continue
            para.append(line)
        flush()
        return ParsedDoc(title=stem, blocks=_cap(blocks))


class TextParser:
    """txt/csv：txt 整文单节；csv 逐行单元格空格连接（行内连接形态，规避清洗坑）。

    csv 与 xlsx 同理：独立短行（如"是,否,是"）会被重复短行清洗误删——行内连接从源头
    规避；连接符用空格替原分隔符，保持数值/文本相邻可读。"""

    engine = "text"

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        stem = _stem(filename)
        raw = content.decode("utf-8", "ignore")
        if filename.lower().endswith(".csv"):
            lines = [
                " ".join(part.strip() for part in line.split(",")) for line in raw.splitlines()
            ]
            text = "\n".join(lines)
        else:
            text = raw
        blocks = [Block(level=1, heading_path=[stem], text=text)] if text.strip() else []
        return ParsedDoc(title=stem, blocks=_cap(blocks))


class XlsxParser:
    """xlsx：每 sheet 一节（一级标题=sheet 名），行内单元格空格连接。

    with_images（F-1017）：openpyxl read_only 模式不加载图片——需非 read_only 二次
    加载读 ws._images；仅在开关开启时支付该成本。"""

    engine = "xlsx"

    def __init__(self, with_images: bool = False) -> None:
        self.with_images = with_images

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        import openpyxl  # 局部导入：openpyxl 导入较重，仅 xlsx 解析时支付

        stem = _stem(filename)
        workbook = openpyxl.load_workbook(BytesIO(content), read_only=True, data_only=True)
        images: list = []
        image_skipped = 0
        if self.with_images:
            from app.rag.image_extract import collect_ooxml_media_images

            extraction = collect_ooxml_media_images(content, "xl/media/")
            images, image_skipped = extraction.images, extraction.skipped
        blocks: list[Block] = []
        for sheet in workbook.worksheets:
            lines: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                cells = [
                    str(cell).strip() for cell in row if cell is not None and str(cell).strip()
                ]
                if cells:
                    lines.append(" ".join(cells))
            if lines:
                blocks.append(Block(level=1, heading_path=[sheet.title], text="\n".join(lines)))
        workbook.close()
        return ParsedDoc(
            title=stem, blocks=_cap(blocks), images=images, image_skipped=image_skipped
        )


class PptxParser:
    """pptx：每幻灯片一节（page=幻灯片序号），标题占位符进 heading_path，备注并入正文。"""

    engine = "pptx"

    def __init__(self, with_images: bool = False) -> None:
        self.with_images = with_images

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        import io as _io

        from pptx import Presentation

        stem = _stem(filename)
        presentation = Presentation(_io.BytesIO(content))
        blocks: list[Block] = []
        images: list = []
        image_skipped = 0
        if self.with_images:
            from app.rag.image_extract import collect_pptx_images

        for index, slide in enumerate(presentation.slides, 1):
            if self.with_images:
                extraction = collect_pptx_images(slide, index)
                images.extend(extraction.images)
                image_skipped += extraction.skipped
            title = ""
            texts: list[str] = []
            for shape in slide.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                text = shape.text_frame.text.strip()
                if not text:
                    continue
                if not title and shape == getattr(slide.shapes, "title", None):
                    title = text
                else:
                    texts.append(text)
            if getattr(slide, "has_notes_slide", False):
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    texts.append(f"备注：{notes}")
            heading = [title or f"幻灯片{index}"]
            body = "\n".join(texts) or title
            if body:
                blocks.append(Block(level=1, heading_path=heading, text=body, page=index))
        return ParsedDoc(
            title=stem, blocks=_cap(blocks), images=images, image_skipped=image_skipped
        )


class _HtmlTextExtractor(HTMLParser):
    """正文提取：script/style 跳过，h1-h4 开新节（标题层级进 heading_path）。

    HTMLParser(convert_charrefs=True) 把字符引用转为纯文本、自定义 ENTITY 声明原样
    当文本处理——不存在实体展开注入面（standards §6.3-1 的 html 侧实现依据）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.title: str = ""
        self._path: list[str] = []
        self._para: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._in_heading = False
        self._heading_level = 1
        self._heading_buf: list[str] = []

    def flush(self) -> None:
        text = "\n".join(self._para).strip("\n")
        self._para.clear()
        if text.strip():
            self.blocks.append(
                Block(level=max(len(self._path), 1), heading_path=self._path or ["正文"], text=text)
            )

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _HEADING_TAGS:
            self.flush()
            self._heading_level = _HEADING_TAGS[tag]
            self._path = self._path[: self._heading_level - 1]
            self._heading_buf.clear()
            self._in_heading = True

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _HEADING_TAGS and self._in_heading:
            heading = " ".join(part.strip() for part in self._heading_buf if part.strip())
            self._path = [
                *self._path[: self._heading_level - 1],
                heading or f"标题{len(self._path) + 1}",
            ]
            self._in_heading = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        if self._in_title:
            self.title = data.strip()
            return
        if self._in_heading:
            self._heading_buf.append(data.strip())
            return
        self._para.append(data.strip())


class HtmlParser:
    """html：正文提取 + 标题层级切节（无依赖：内置 html.parser）。"""

    engine = "html"

    def parse(self, filename: str, content: bytes) -> ParsedDoc:
        stem = _stem(filename)
        extractor = _HtmlTextExtractor()
        extractor.feed(content.decode("utf-8", "ignore"))
        extractor.close()
        extractor.flush()  # 尾节收口：最后一个标题节后无后续 heading 触发 flush
        blocks = extractor.blocks or [Block(level=1, heading_path=[stem], text=extractor.title)]
        return ParsedDoc(title=extractor.title or stem, blocks=_cap(blocks))
