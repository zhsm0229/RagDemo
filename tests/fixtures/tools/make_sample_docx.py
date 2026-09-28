r"""程序化生成样例 docx（含 Heading1/Heading2/正文/表格），供 parser 单测与人工检查。

用法：
    python tests/fixtures/tools/make_sample_docx.py   # 写出 tests/fixtures/samples/sample.docx
单测直接 import build_sample_docx() 取字节（单一事实来源，样本不落盘也可测）。
"""

import io
from pathlib import Path

from docx import Document


def build_sample_docx() -> bytes:
    """构造两章样例：H1 总则（1 段）→ H2 适用范围（1 段 + 2x2 表格）→ H1 考勤（1 段）。"""
    document = Document()
    document.core_properties.title = "员工手册"
    document.add_heading("第一章 总则", level=1)
    document.add_paragraph("第一条 本手册适用于全体员工。")
    document.add_heading("1.1 适用范围", level=2)
    document.add_paragraph("本手册覆盖总部及各分支机构。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "说明"
    table.cell(1, 0).text = "差旅"
    table.cell(1, 1).text = "凭发票据实报销"
    document.add_heading("第二章 考勤", level=1)
    document.add_paragraph("第二条 工作时间为每周一至周五。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "samples" / "sample.docx"
    out.write_bytes(build_sample_docx())
    print(f"written: {out}")
