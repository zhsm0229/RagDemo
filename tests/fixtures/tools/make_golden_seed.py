r"""程序化生成黄金种子样本（golden/manifest.json 中真跑的三条）：规整 docx、规整 pdf、乱码占位 docx。

规整类正文在 make_sample_docx/make_sample_pdf 的构造方式上加长——原样例全文仅 61/65 字符，
低于质量红线 max(100, 原始×10%) 的 100 字下限（standards §10.3），无法作为全链绿例。
乱码占位用 XML 合法的 C1 控制区（U+007F–U+009F，避开 U+0085 换行空白）与 U+FFFD 混排，
占比约 16% 远超 5% 阈值（F-1005），且行内容互不相同不被清洗规则整行删除。
用法：
    python tests/fixtures/tools/make_golden_seed.py   # 写出 tests/fixtures/golden/ 三个种子
单测（test_golden.py）import build_*() 与入库种子比对解析结果（防漂移）。
"""

import io
from pathlib import Path

from docx import Document
from pypdf import PdfReader


def build_clean_docx() -> bytes:
    """规整 docx 种子（hr_制度_v1.docx）：四章十条 + 1 表格，清洗后约 350 字，越过红线。"""
    document = Document()
    document.core_properties.title = "员工制度汇编"
    document.add_heading("第一章 总则", level=1)
    document.add_paragraph("第一条 本制度适用于总公司及各分支机构的全体员工，试用期员工参照执行。")
    document.add_paragraph("第二条 本制度由人力资源部负责解释与修订，自发布之日起施行。")
    document.add_heading("第二章 考勤管理", level=1)
    document.add_paragraph("第三条 标准工作时间为每周一至周五，上午九时至下午六时，午休一小时。")
    document.add_paragraph(
        "第四条 迟到或早退超过三十分钟按半天事假计，一个月内累计三次按旷工一日处理。"
    )
    document.add_heading("第三章 差旅与报销", level=1)
    document.add_paragraph(
        "第五条 员工出差须事先提交申请并经部门负责人批准，差旅费用凭正规发票据实报销。"
    )
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "说明"
    table.cell(1, 0).text = "城市间交通"
    table.cell(1, 1).text = "凭票据实报销"
    table.cell(2, 0).text = "住宿费用"
    table.cell(2, 1).text = "每晚上限四百元"
    document.add_paragraph(
        "第六条 报销单据须在差旅结束后十个工作日内提交财务部审核，逾期不予受理。"
    )
    document.add_heading("第四章 保密与责任", level=1)
    document.add_paragraph("第七条 员工在职期间知悉的商业秘密与客户信息，离职后仍应承担保密义务。")
    document.add_paragraph("第八条 违反本制度给公司造成损失的，公司有权依据情节追究相应责任。")
    document.add_paragraph("第九条 本制度未尽事宜按国家法律法规及公司其他规章制度执行。")
    document.add_paragraph("第十条 员工对制度修订有建议权，可通过人力资源部意见箱反馈。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _stream_obj(text: str) -> bytes:
    """内容流对象：单行 Helvetica 文本（拼装方式与 make_sample_pdf._stream_obj 相同）。"""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    return f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream"


def build_clean_pdf() -> bytes:
    """规整 pdf 种子（admin_handbook_v1.pdf）：2 页 ASCII 文本约 161 字符，越过红线 100 字下限。"""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
            b"/Resources << /Font << /F1 6 0 R >> >> >>"
        ),
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 7 0 R "
            b"/Resources << /Font << /F1 6 0 R >> >> >>"
        ),
        _stream_obj(
            "Employee Handbook Annual Report. Chapter 1 covers the scope and general principles."
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        _stream_obj(
            "Chapter 2 covers attendance rules. Contact the HR office for expense details."
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)


def build_garbled_docx() -> bytes:
    """乱码占位 docx 种子（hr_乱码_v1.docx）：C1 控制区 + U+FFFD 混排，乱码占比约 16% 超阈值。"""
    document = Document()
    document.add_heading("乱码回归样本", level=1)
    document.add_paragraph(
        "系统导出日志片段：\x7f\x80\ufffd\x91\x92后续说明文字保持完整可读，用于稀释乱码字符占比。"
    )
    document.add_paragraph(
        "历史数据迁移记录：\x93\x94\x95\x9a\x9f\x82本段其余部分为正常业务描述文本。"
    )
    document.add_paragraph(
        "遗留系统残留替换符：\ufffd\ufffd\x84\x86\x87\x88\x8b\x9e段末保持正常中文叙述收尾。"
    )
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _report() -> str:
    """自检：三个种子的全文长度与乱码占比（生成后人工核对入口）。"""
    from app.rag.clean import clean_text
    from app.rag.parser import DocxParser, PdfFallbackParser
    from app.rag.precheck import garbled_ratio

    docx_doc = DocxParser().parse("hr_制度_v1.docx", build_clean_docx())
    pdf_doc = PdfFallbackParser().parse("admin_handbook_v1.pdf", build_clean_pdf())
    garbled_doc = DocxParser().parse("hr_乱码_v1.docx", build_garbled_docx())
    joined = "\n".join(block.text for block in garbled_doc.blocks)
    cleaned = clean_text(joined)
    return (
        f"docx 清洗后 {len(clean_text(chr(10).join(b.text for b in docx_doc.blocks)))} 字符；"
        f"pdf 清洗后 {len(clean_text(chr(10).join(b.text for b in pdf_doc.blocks)))} 字符；"
        f"乱码样本清洗后 {len(cleaned)} 字符，乱码占比 {garbled_ratio(cleaned):.1%}"
    )


if __name__ == "__main__":
    golden = Path(__file__).resolve().parents[1] / "golden"
    golden.mkdir(exist_ok=True)
    (golden / "hr_制度_v1.docx").write_bytes(build_clean_docx())
    (golden / "admin_handbook_v1.pdf").write_bytes(build_clean_pdf())
    (golden / "hr_乱码_v1.docx").write_bytes(build_garbled_docx())
    print(
        f"written: {golden}\n{_report()}\npages: {PdfReader(io.BytesIO(build_clean_pdf())).pages}"
    )
