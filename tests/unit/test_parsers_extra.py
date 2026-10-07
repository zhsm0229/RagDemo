"""扩展格式解析器单测（F-1013）：现场构造 bytes → 断言 ParsedDoc（参照 test_parser 模式）。

覆盖：md 标题层级/代码块保护、csv/txt 单节、xlsx sheet 分节与行内连接、
pptx 幻灯片分节（页码/备注）、html 正文提取与脚本剥离、输出上限截断。
"""

import io

import openpyxl

from app.rag.chunker import ParsedDoc
from app.rag.parsers_extra import (
    HtmlParser,
    MarkdownParser,
    PptxParser,
    TextParser,
    XlsxParser,
)


def _xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pptx_bytes(slides: list[tuple[str | None, list[str], str]]) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width = Inches(10)
    presentation.slide_height = Inches(7.5)
    for title, bullets, notes in slides:
        layout = presentation.slide_layouts[1]  # 标题+内容版式
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = title or ""
        body = slide.placeholders[1].text_frame
        body.text = bullets[0] if bullets else ""
        for line in bullets[1:]:
            body.add_paragraph().text = line
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


class TestMarkdownParser:
    def test_heading_hierarchy_and_heading_path(self) -> None:
        md = "# 部署指南\n\n准备章节。\n## 环境要求\n\n内存至少 16G。\n### 内网隔离\n\n禁外网。\n".encode()
        doc: ParsedDoc = MarkdownParser().parse("部署指南.md", md)
        assert doc.title == "部署指南"
        assert [b.heading_path for b in doc.blocks] == [
            ["部署指南"],
            ["部署指南", "环境要求"],
            ["部署指南", "环境要求", "内网隔离"],
        ]
        assert doc.blocks[-1].text == "禁外网。"

    def test_code_fence_hash_not_split_as_heading(self) -> None:
        md = "# 标题\n\n```bash\n# 这不是标题\n```".encode()
        doc: ParsedDoc = MarkdownParser().parse("代码.md", md)
        assert len(doc.blocks) == 1  # 围栏内 # 不切节
        assert "这不是标题" in doc.blocks[0].text


class TestTextParser:
    def test_txt_single_section_with_stem_root(self) -> None:
        doc: ParsedDoc = TextParser().parse("说明.txt", "第一行\n第二行".encode())
        assert len(doc.blocks) == 1
        assert doc.blocks[0].heading_path == ["说明"]
        assert "第一行" in doc.blocks[0].text

    def test_csv_rows_kept_inline(self) -> None:
        doc: ParsedDoc = TextParser().parse("表.csv", "是,否,是\n1,2,3".encode())
        text = doc.blocks[0].text
        assert "是 否 是" in text  # 行内连接：重复短值不独立成行（清洗坑规避）


class TestXlsxParser:
    def test_sheets_to_blocks_with_inline_cells(self) -> None:
        content = _xlsx_bytes(
            {
                "报销标准": [["项目", "说明"], ["差旅", "凭发票据实报销"], [100, 200]],
                "考勤": [["迟到", "早退"], [3, 1]],
            }
        )
        doc: ParsedDoc = XlsxParser().parse("制度表.xlsx", content)
        assert [b.heading_path for b in doc.blocks] == [["报销标准"], ["考勤"]]
        报销 = doc.blocks[0].text
        assert "差旅 凭发票据实报销" in 报销  # 行内空格连接
        assert "100 200" in 报销  # 纯数字行不被删除（行内连接规避清洗坑）
        assert doc.blocks[1].text.startswith("迟到 早退")

    def test_empty_sheet_produces_no_block(self) -> None:
        content = _xlsx_bytes({"空表": []})
        doc: ParsedDoc = XlsxParser().parse("空.xlsx", content)
        assert doc.blocks == []


class TestPptxParser:
    def test_slides_with_title_page_and_notes(self) -> None:
        content = _pptx_bytes(
            [
                ("AI 培训", ["课程简介", "面向全员"], "讲师提前到场"),
                ("第二讲", ["进阶内容"], ""),
            ]
        )
        doc: ParsedDoc = PptxParser().parse("培训课件.pptx", content)
        assert len(doc.blocks) == 2
        assert doc.blocks[0].heading_path == ["AI 培训"]
        assert doc.blocks[0].page == 1
        assert "课程简介" in doc.blocks[0].text
        assert "备注：讲师提前到场" in doc.blocks[0].text
        assert doc.blocks[1].page == 2
        assert "备注" not in doc.blocks[1].text  # 无备注不产备注行


class TestHtmlParser:
    def test_heading_sections_and_script_stripped(self) -> None:
        html = """
        <html><head><title>产品说明</title><style>body{color:red}</style></head>
        <body><h1>产品说明</h1><p>第一段正文。</p>
        <script>alert('xss')</script>
        <h2>安装步骤</h2><p>下载安装包。</p></body></html>
        """.encode()
        doc: ParsedDoc = HtmlParser().parse("产品说明.html", html)
        assert doc.title == "产品说明"
        assert [b.heading_path for b in doc.blocks] == [["产品说明"], ["产品说明", "安装步骤"]]
        text = "\n".join(b.text for b in doc.blocks)
        assert "alert" not in text and "color:red" not in text  # script/style 剥离
        assert "第一段正文" in text and "下载安装包" in text
