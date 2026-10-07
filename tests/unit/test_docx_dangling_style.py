r"""docx 悬空样式引用回归（2026-10-06 Xstudio 实测缺陷）：

正文段落引用 styles.xml 未定义的样式 ID 时，python-docx 的 paragraph.style 返回
None——_heading_level 曾直接取 .name 抛 AttributeError，被 _DOCX_PARSE_ERRORS
兜底误报"包体结构非法或已加密"，整篇文档解析失败。修复后按无标题普通段落处理。
（放独立文件避免与在途的 test_parser.py 冲突；被测函数同源 app/rag/parser.py）
"""

import io
import zipfile

from docx import Document as DocxDocument

from app.rag.parser import DocxParser


def _dangling_style_docx() -> bytes:
    """构造带悬空样式引用的 docx：正文段落 pStyle 指向未定义的 GhostStyle。"""
    document = DocxDocument()
    document.add_heading("第一章 报销标准", level=1)
    document.add_paragraph("差旅费用报销须凭正规票据，住宿费按城市分档执行，交通费据实报销。" * 6)
    buffer = io.BytesIO()
    document.save(buffer)
    src = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "word/document.xml":
                text = data.decode("utf-8")
                old = "<w:p><w:r>"
                assert old in text  # python-docx 产出的普通段落形态稳定
                text = text.replace(
                    old,
                    '<w:p><w:pPr><w:pStyle w:val="GhostStyle"/></w:pPr><w:r>',
                    1,  # 仅首个正文段落挂悬空样式
                )
                data = text.encode("utf-8")
            dst.writestr(info, data)
    return out.getvalue()


def test_docx_dangling_style_reference_parses() -> None:
    """悬空样式引用不判文档非法：整篇正常出块，其余标题层级不受影响。"""
    content = _dangling_style_docx()
    parsed = DocxParser().parse("悬空样式.docx", content)
    joined = "\n".join(block.text for block in parsed.blocks)
    assert any(block.heading_path == ["第一章 报销标准"] for block in parsed.blocks)
    assert "差旅费用报销须凭正规票据" in joined
