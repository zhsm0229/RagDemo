r"""生成三份待投放黄金样本（manifest 中 pending=true 的 dual_col/table/scanned）。

内容均为虚构的《员工差旅费报销制度》（华信科技，内部虚构公司），不载真实业务信息。
- 表格 docx：python-docx 生成，含两张表格（住宿/交通标准），docx 引擎直接解析；
- 双栏 PDF：fpdf2 text_columns 双栏真实版面 + 文末两张通栏表格；
- 扫描件 PDF：Pillow 把正文绘成整页 PNG（无文本层），fpdf2 整页嵌图——仅 MinerU OCR 可解。

依赖（仅本工具用，运行时手动安装，不进 pyproject）：
    pip install python-docx fpdf2 pillow
用法：.venv/Scripts/python tests/fixtures/tools/make_pending_samples.py
生成后人工/脚本验证（黄金回归 + MinerU 实测），再置 manifest pending=false。
"""

import sys
from pathlib import Path

from docx import Document

FIXTURES = Path(__file__).resolve().parents[1]
GOLDEN = FIXTURES / "golden"
FONT = Path(r"C:\Windows\Fonts\simfang.ttf")  # 仿宋：单文件 TTF，fpdf2 兼容

TITLE = "员工差旅费报销制度"
ORG = "华信科技有限公司人力资源部"
BODY_SECTIONS: list[tuple[str, list[str]]] = [
    (
        "第一章 总则",
        [
            "第一条 为规范员工差旅管理，控制差旅成本，提高报销效率，依据公司财务管理规定，制定本制度。",
            "第二条 本制度适用于公司全体正式员工及经批准出差的劳务派遣、实习人员。",
            "第三条 差旅费开支范围包括城市间交通费、住宿费、市内交通费、伙食补助费与订票手续费。",
        ],
    ),
    (
        "第二章 交通与住宿标准",
        [
            "第四条 城市间交通工具按职级执行：总监及以上可乘飞机经济舱与高铁一等座；经理级可乘高铁一等座；普通员工乘高铁二等座或硬卧。未经批准乘用更高标准的，超出部分自理。",
            "第五条 住宿费按出差城市类别执行分档限额，超标部分自理，同性别同事合住时按双份限额执行。",
            "第六条 市内交通费凭票据实报销，单日上限八十元；鼓励乘坐地铁、公交等公共交通工具。",
        ],
    ),
    (
        "第三章 补贴与报销流程",
        [
            "第七条 伙食补助按自然日计发：一线城市每日一百元，其他城市每日八十元；由接待方供餐的餐次相应扣减。",
            "第八条 出差结束后十个工作日内，员工须在费用系统提交报销单，附行程单、发票原件与审批单，超期不予受理。",
            "第九条 报销单经部门负责人、财务复核两级审批后付款；单笔金额超过两万元的须加签总经理审批。",
            "第十条 虚报、冒领差旅费的，除追回款项外，按公司奖惩条例处理；情节严重的解除劳动合同。",
        ],
    ),
    (
        "第四章 附则",
        [
            "第十一条 本制度由人力资源部负责解释，自发布之日起施行，原有相关规定同时废止。",
            "第十二条 出国差旅参照本制度另行制定细则；本制度未尽事宜按国家有关规定执行。",
        ],
    ),
]
LODGING_TABLE = (
    ("城市类别", "总监及以上", "经理级", "普通员工"),
    ("一线城市（北上广深）", "600元/晚", "450元/晚", "350元/晚"),
    ("二线城市（省会计划单列）", "450元/晚", "350元/晚", "280元/晚"),
    ("三线及以下城市", "350元/晚", "280元/晚", "220元/晚"),
)
TRANSPORT_TABLE = (
    ("职级", "飞机", "高铁", "火车"),
    ("总监及以上", "经济舱", "一等座", "软卧"),
    ("经理级", "经济舱（特批）", "一等座", "软卧"),
    ("普通员工", "不适用", "二等座", "硬卧"),
)


def build_table_docx() -> None:
    """表格 docx：标题 + 章节段落 + 两张真实表格（docx 引擎解析表格文本）。"""
    doc = Document()
    doc.add_heading(TITLE, level=0)
    doc.add_paragraph(f"{ORG}（V1 · 2026 年 9 月修订）")
    for heading, paras in BODY_SECTIONS:
        doc.add_heading(heading, level=1)
        for p in paras:
            doc.add_paragraph(p)
    for heading, rows in (
        ("附表一 住宿费分档限额", LODGING_TABLE),
        ("附表二 城市间交通工具标准", TRANSPORT_TABLE),
    ):
        doc.add_heading(heading, level=1)
        table = doc.add_table(rows=len(rows), cols=len(rows[0]))
        table.style = "Table Grid"
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                table.cell(r, c).text = cell
    target = GOLDEN / "hr_制度_表格_v1.docx"
    doc.save(target)
    print(f"生成 {target} ({target.stat().st_size} bytes)")


def _new_pdf() -> "object":
    """A4 纵向 + 仿宋字体注册（fpdf2 2.7+ 原生 CJK 断行）。"""
    from fpdf import FPDF

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_page()
    pdf.add_font("FangSong", "", str(FONT))
    pdf.add_font("FangSong", "B", str(FONT))  # 表头加粗用：无真粗体文件，以常规体充当
    pdf.set_font("FangSong", size=11)
    return pdf


def _title_block(pdf) -> None:
    pdf.set_font_size(16)
    pdf.cell(0, 10, TITLE, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font_size(9.5)
    pdf.cell(0, 6, f"{ORG}（V1 · 2026 年 9 月修订）", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font_size(11)


def _tables(pdf) -> None:
    """文末两张通栏表格（标题 + Grid 边框）。"""
    from fpdf.fonts import FontFace

    for heading, rows in (
        ("附表一 住宿费分档限额", LODGING_TABLE),
        ("附表二 城市间交通工具标准", TRANSPORT_TABLE),
    ):
        pdf.set_font_size(12)
        pdf.cell(0, 8, heading, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font_size(10)
        header = FontFace(emphasis="BOLD", fill_color=(230, 230, 230))
        with pdf.table(borders_layout="ALL", line_height=6, headings_style=header) as t:
            for row in rows:
                r = t.row()
                for cell in row:
                    r.cell(cell)
        pdf.ln(3)
    pdf.set_font_size(11)


def build_dual_col_pdf() -> None:
    """双栏 PDF：正文双栏流式排版（真实双栏版面，检验版面分析），表格通栏置尾。"""
    pdf = _new_pdf()
    _title_block(pdf)
    with pdf.text_columns(ncols=2, gutter=8) as cols:
        for heading, paras in BODY_SECTIONS:
            with cols.paragraph(text_align="JUSTIFY") as para:
                para.write(heading)
            for p in paras:
                with cols.paragraph(text_align="JUSTIFY") as para:
                    para.write(p)
    _tables(pdf)
    pdf.set_font_size(9)
    pdf.cell(0, 6, f"{ORG}　内部制度　第 1 页 / 共 2 页", align="C")
    target = GOLDEN / "hr_制度_双栏_v1.pdf"
    pdf.output(target)
    print(f"生成 {target} ({target.stat().st_size} bytes)")


def build_scanned_pdf() -> None:
    """扫描件 PDF：正文 Pillow 绘制为整页 PNG（无文本层）→ fpdf2 整页嵌图。"""
    from PIL import Image, ImageDraw, ImageFont

    w, h = 1240, 1754  # A4 @150dpi
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(str(FONT), 40)
    f_small = ImageFont.truetype(str(FONT), 22)
    f_body = ImageFont.truetype(str(FONT), 26)
    margin, y = 96, 90
    # 页眉（重复行，供清洗规则回归）
    draw.text(
        ((w - draw.textlength(f"{ORG}　内部制度", font=f_small)) / 2, 36),
        f"{ORG}　内部制度",
        font=f_small,
        fill=(90, 90, 90),
    )
    t = TITLE
    draw.text(((w - draw.textlength(t, font=f_title)) / 2, y), t, font=f_title, fill="black")
    y += 66
    org_line = f"{ORG}（V1 · 2026 年 9 月修订）"
    draw.text(
        ((w - draw.textlength(org_line, font=f_small)) / 2, y),
        org_line,
        font=f_small,
        fill=(60, 60, 60),
    )
    y += 60
    for heading, paras in BODY_SECTIONS:
        draw.text((margin, y), heading, font=f_body, fill="black")
        y += 44
        for p in paras:
            # 按像素宽手工折行（CJK 无空格，简单逐行排）
            line = ""
            for ch in p:
                if draw.textlength(line + ch, font=f_body) > w - 2 * margin:
                    draw.text((margin, y), line, font=f_body, fill="black")
                    y += 40
                    line = ch
                else:
                    line += ch
            draw.text((margin, y), line, font=f_body, fill="black")
            y += 48
        if y > h - 220:  # 两页放不下则截断在附则前（样本只需一页即满足字符下限）
            break
    draw.text(
        ((w - draw.textlength("第 1 页 / 共 1 页", font=f_small)) / 2, h - 80),
        "第 1 页 / 共 1 页",
        font=f_small,
        fill=(90, 90, 90),
    )
    png = GOLDEN / "_tmp_scan_page.png"
    img.save(png)
    pdf = _new_pdf()
    pdf.image(str(png), x=0, y=0, w=210, h=297)
    target = GOLDEN / "hr_制度_扫描件_v1.pdf"
    pdf.output(target)
    png.unlink()
    print(f"生成 {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    if not FONT.exists():
        sys.exit(f"缺少中文字体：{FONT}")
    GOLDEN.mkdir(parents=True, exist_ok=True)
    build_table_docx()
    build_dual_col_pdf()
    build_scanned_pdf()
    print("完成：三份样本已生成，验证通过后请置 manifest pending=false")
