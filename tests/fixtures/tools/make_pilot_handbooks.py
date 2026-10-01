r"""程序化生成试点语料治理拆分手册 docx（W6 串库漏拒根因处置，裁决见 W5 部署验收报告 §5.2）。

背景：v1.1 全员/客服手册同内容双部门发布且含「考勤与休假」（HR 域）章，真实 rerank
下 hr 制度题撞该章漏拒（0.6509/0.6578/0.6953，漏拒带与对照命中带重叠、阈值不可修），
治理走语料侧拆分：
    - 全员版 v1.2（dept=all）：撤出年假/病假（HR 域），保留工作时间（考勤打卡通用
      口径，裁决留全员）、报销制度、售后服务；
    - HR 版 v1.0（dept=hr）：承接「考勤与休假」全节（工作时间/年假/病假）。
拆分后 hr 主题内容退出 service 视角可见范围（界=[视角部门, 全员]，F-502），
串库复测判据：service 视角 hr 题 10/10 拒答、对照 4 题命中（runbook §5 登记表）。

内容单一事实来源：两份手册的正文段落均从 v1.1（data/pilot-handbook-all.docx）
逐字迁移，仅三处治理性差异——①全员版删除 1.2 年假/1.3 病假两节；②HR 版病假段
清理 v1.1 排版混入的杂散英文词「annual」；③各自追加版本行。禁改其余措辞（对照题
命中与 W5 分数带可比性均依赖语料稳定）。

用法（需项目 venv）：
    python tests/fixtures/tools/make_pilot_handbooks.py -o data/pilot
单测 import build_all_handbook_v12() / build_hr_handbook_v10() 取字节（同
make_sample_docx 约定，生成物不入库：data/pilot 已 gitignore，上传后由 minio/本地
uploads 落盘）。
"""

import argparse
import io
from pathlib import Path

from docx import Document

ALL_VERSION_LINE = "版本：v1.2（全员发布版，2026-10-01 起施行）。"
HR_VERSION_LINE = "版本：v1.0（HR 部门发布版，2026-10-01 起施行）。"

# v1.1 原文（逐字迁移，来源 data/pilot-handbook-all.docx；HR 域三节由 HR 版承接）
WORK_TIME_TEXT = (
    "标准工作时间为每周一至周五，上午 9:00 至下午 18:00，午休 1 小时。"
    "因公外出需提前在 OA 系统报备，未报备且无法说明去向的按缺勤处理。"
)
ANNUAL_LEAVE_TEXT = (
    "入职满一年的员工享有带薪年假：工龄不满十年者每年五天，满十年者每年十天，"
    "满二十年者每年十五天。年假原则上当年使用，经审批最多可跨年结转至次年三月三十一日，"
    "逾期未休按日工资的两倍补偿。"
)
SICK_LEAVE_TEXT = (
    "病假需提供二级以上医院开具的证明。病假累计超过三十天的，公司有权要求复查。"
    "病假期间工资按当地最低工资标准的百分之八十发放。"
)
TRAVEL_EXPENSE_TEXT = (
    "差旅费用凭发票据实报销，交通费按实报销，住宿费上限为每晚四百元，超出部分自理。"
    "报销单需在出差返回后十个工作日内提交，逾期不予受理。"
)
DAILY_EXPENSE_TEXT = (
    "日常办公费用报销单笔上限五百元，超过需部门负责人与财务双审批。"
    "所有报销通过线上报销系统提交，纸质单据每周三集中收取。"
)
RETURN_POLICY_TEXT = (
    "自签收之日起七天内，商品未使用且包装完好的可无理由退货；"
    "质量问题的退货不受七天限制，运费由公司承担。退款将在退货验收后五个工作日内原路退回。"
)
WARRANTY_TEXT = (
    "自营电子产品提供一年免费保修，保修期内非人为损坏免费维修或更换；"
    "人为损坏或超过保修期的维修仅收取成本费。"
)


def _save(document: Document) -> bytes:
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_all_handbook_v12() -> bytes:
    """全员版 v1.2：工作时间（通用口径）+ 报销制度 + 售后服务，无任何 HR 域条目。"""
    document = Document()
    document.core_properties.title = "员工手册（试点版）"
    document.add_heading("员工手册（试点版）", level=1)
    document.add_heading("第一章 工作时间", level=1)
    document.add_heading("1.1 工作时间", level=2)
    document.add_paragraph(WORK_TIME_TEXT)
    document.add_heading("第二章 报销制度", level=1)
    document.add_heading("2.1 差旅报销", level=2)
    document.add_paragraph(TRAVEL_EXPENSE_TEXT)
    document.add_heading("2.2 日常报销", level=2)
    document.add_paragraph(DAILY_EXPENSE_TEXT)
    document.add_heading("第三章 售后服务", level=1)
    document.add_heading("3.1 退货政策", level=2)
    document.add_paragraph(RETURN_POLICY_TEXT)
    document.add_heading("3.2 保修条款", level=2)
    document.add_paragraph(WARRANTY_TEXT)
    document.add_paragraph(ALL_VERSION_LINE)
    return _save(document)


def build_hr_handbook_v10() -> bytes:
    """HR 版 v1.0：承接 v1.1「考勤与休假」全节（工作时间/年假/病假）。"""
    document = Document()
    document.core_properties.title = "HR手册（试点版）"
    document.add_heading("HR手册（试点版）", level=1)
    document.add_heading("第一章 考勤与休假", level=1)
    document.add_heading("1.1 工作时间", level=2)
    document.add_paragraph(WORK_TIME_TEXT)
    document.add_heading("1.2 年假", level=2)
    document.add_paragraph(ANNUAL_LEAVE_TEXT)
    document.add_heading("1.3 病假", level=2)
    document.add_paragraph(SICK_LEAVE_TEXT)
    document.add_paragraph(HR_VERSION_LINE)
    return _save(document)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成试点语料治理拆分手册（全员版 v1.2 / HR 版 v1.0，W6 串库治理配套）"
    )
    parser.add_argument("-o", "--output", default="data/pilot", help="输出目录（默认 data/pilot）")
    args = parser.parse_args()
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("员工手册_全员版.docx", build_all_handbook_v12()),
        ("HR手册_试点.docx", build_hr_handbook_v10()),
    ):
        out = out_dir / name
        out.write_bytes(payload)
        print(f"written: {out}")


if __name__ == "__main__":
    main()
