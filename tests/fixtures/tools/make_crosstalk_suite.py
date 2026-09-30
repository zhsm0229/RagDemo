r"""程序化生成串库双向评测题库 xlsx（T9 串库双向走查的弹药，流程见 runbook 串库演练节）。

构成（24 行）：人力制度类串库拒答题 10 条（部门=人力）+ 客服售后类串库拒答题 10 条
（部门=客服）+ 全员对照事实题 4 条（部门=全员）。串库题期望答案一律 `[拒答]`——
客服视角跑分时人力组必须全部拒答（检索边界恒 [视角部门, 全员]，F-502），人力视角
对称；对照题问的是配套全员语料（make_sample_docx 固定文本）的内容，任一视角都应
命中——防"全部拒答也全绿"的假阴性，语料上传是演练前置步骤（runbook）。

表头/部门中文枚举/题型英文枚举/拒答标记不复制字面量，直接 import 后端
eval_service 常量，xlsx 字节流也走后端模板同款 write_template——后端模板漂移时
test_crosstalk_suite 单测即红。题目均为合成题（问题以「演练用合成题」起头标注，
与真实题库区分，演练后可按此前缀识别清理）。

用法（需项目 venv，app 以 -e 安装）：
    python tests/fixtures/tools/make_crosstalk_suite.py -o crosstalk.xlsx
可重复运行：-o 指向的既有文件直接覆盖；产物不入库（生成物放临时目录）。
单测（test_crosstalk_suite.py）import build_crosstalk_suite() 用 xlsx_io 读回断言。
"""

import argparse
from pathlib import Path

from app.services.eval_service import REJECT_MARK, TEMPLATE_HEADERS
from app.services.xlsx_io import write_template

DRILL_MARK = "演练用合成题"  # 合成题统一前缀：演练题与真实题库的识别标记

HR_CROSSTALK_QUESTIONS: list[str] = [
    f"{DRILL_MARK}：年假未休完可以跨年结转吗？",
    f"{DRILL_MARK}：试用期员工是否享有年假？",
    f"{DRILL_MARK}：病假需要提交哪些证明材料？",
    f"{DRILL_MARK}：婚假有多少天，是否包含法定节假日？",
    f"{DRILL_MARK}：迟到或早退会如何处理？",
    f"{DRILL_MARK}：社保缴费基数是如何确定的？",
    f"{DRILL_MARK}：绩效考核结果如何影响调薪？",
    f"{DRILL_MARK}：离职手续需要提前多少天申请？",
    f"{DRILL_MARK}：加班调休可以折算成加班费吗？",
    f"{DRILL_MARK}：员工体检费用由公司承担吗？",
]

SERVICE_CROSSTALK_QUESTIONS: list[str] = [
    f"{DRILL_MARK}：退换货的时限是多久？",
    f"{DRILL_MARK}：如何查询订单物流信息？",
    f"{DRILL_MARK}：发票开错后如何换开？",
    f"{DRILL_MARK}：会员积分如何兑换或清零？",
    f"{DRILL_MARK}：商品出现质量问题如何申请售后？",
    f"{DRILL_MARK}：定制类商品支持无理由退货吗？",
    f"{DRILL_MARK}：退货运费由哪方承担？",
    f"{DRILL_MARK}：优惠券可以叠加使用吗？",
    f"{DRILL_MARK}：收到商品破损第一步该做什么？",
    f"{DRILL_MARK}：如何申请延长保修服务？",
]

# 对照题四元组（问题/期望答案/来源/语料关键词）：期望答案取自 make_sample_docx 固定
# 文本并附「任一视角应命中」演练说明；语料关键词供单测与语料固定文本逐一比对
# （语料漂移 = 对照题假阴性），也供演练人快速核对答案出处。
CONTROL_QUESTIONS: list[dict[str, str]] = [
    {
        "question": f"{DRILL_MARK}（对照语料）：员工手册适用于哪些人员？",
        "expected": "本手册适用于全体员工。（任一视角应命中）",
        "source": "对照语料 sample.docx 第一章 总则",
        "keyword": "本手册适用于全体员工",
    },
    {
        "question": f"{DRILL_MARK}（对照语料）：手册适用范围覆盖哪些机构？",
        "expected": "本手册覆盖总部及各分支机构。（任一视角应命中）",
        "source": "对照语料 sample.docx 1.1 适用范围",
        "keyword": "总部及各分支机构",
    },
    {
        "question": f"{DRILL_MARK}（对照语料）：差旅费用如何报销？",
        "expected": "差旅凭发票据实报销。（任一视角应命中）",
        "source": "对照语料 sample.docx 总则附表",
        "keyword": "凭发票据实报销",
    },
    {
        "question": f"{DRILL_MARK}（对照语料）：标准工作时间是每周哪几天？",
        "expected": "工作时间为每周一至周五。（任一视角应命中）",
        "source": "对照语料 sample.docx 第二章 考勤",
        "keyword": "每周一至周五",
    },
]

TOTAL_ROWS = len(HR_CROSSTALK_QUESTIONS) + len(SERVICE_CROSSTALK_QUESTIONS) + len(CONTROL_QUESTIONS)


def build_crosstalk_suite() -> bytes:
    """题库 xlsx 字节流：后端模板表头 + 24 行（行序：人力 10 → 客服 10 → 全员对照 4）。"""
    rows: list[list[str]] = []
    rows += [["人力", q, REJECT_MARK, "", "cross_reject"] for q in HR_CROSSTALK_QUESTIONS]
    rows += [["客服", q, REJECT_MARK, "", "cross_reject"] for q in SERVICE_CROSSTALK_QUESTIONS]
    rows += [
        ["全员", c["question"], c["expected"], c["source"], "factual"] for c in CONTROL_QUESTIONS
    ]
    return write_template(headers=TEMPLATE_HEADERS, sample_rows=rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成串库双向评测题库 xlsx（24 行，runbook 串库演练节配套）"
    )
    parser.add_argument("-o", "--output", required=True, help="输出 xlsx 路径（覆盖既有文件）")
    args = parser.parse_args()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_crosstalk_suite())
    print(f"written: {out} ({TOTAL_ROWS} rows)")


if __name__ == "__main__":
    main()
