r"""评测题库 xlsx 模板（F-601，自 eval_service.py 拆出，行数红线 §1.4）：表头/示例行/模板字节流。

部门列收中文枚举名（客服/人力/全员）——对接人填 Excel 用中文名，服务层映射 code
（F-401 傻瓜式原则，映射在 eval_service._DEPT_NAME_TO_CODE）。示例行三型：事实型 /
多跳 / 串库拒答（期望答案 [拒答]，判定协议第 3 条）。来源列 0011 起非拒答题必填
（spec metrics-balance §4.1），示例行给可匹配的合法引用形态（完整标题/原始文件名）。
eval_service 转出再导出 REJECT_MARK/TEMPLATE_HEADERS/build_template，既有导入路径不变。
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment

TEMPLATE_HEADERS: list[str] = ["部门", "问题", "期望答案", "来源", "题型"]
REJECT_MARK: str = "[拒答]"  # 串库拒答题期望答案的约定格式（判定协议第 3 条）
# 来源列表头批注（必填标注）。不改表头名：导入端表头按 TEMPLATE_HEADERS 逐字匹配
# （xlsx_io._header_errors），改名会让存量文件报"缺少列"；批注是纯展示元数据，
# read_rows 不感知。write_template 无批注能力，build_template 自行组装 workbook。
_SOURCE_NOTE = (
    "非拒答题必填：填文档完整标题或原始文件名（可带扩展名），须匹配本部门唯一文档；串库拒答题留空"
)
TEMPLATE_SAMPLES: list[list[str]] = [
    [
        "客服",
        "员工如何重置门户登录密码？",
        "登录页点击「忘记密码」，通过绑定手机号验证后重置。",
        "客服_门户使用手册_v1",
        "factual",
    ],
    [
        "人力",
        "年假可以跨年结转吗？未休完的年假如何处理？",
        "当年度假额度次年 1 月 1 日清零，不跨年结转。",
        "员工手册.docx",
        "multi_hop",
    ],
    [
        "客服",
        "人力部门的薪酬体系是怎样的？",
        REJECT_MARK,
        "",  # 串库拒答题期望行为是拒答，无期望出处
        "cross_reject",
    ],
]


def build_template() -> bytes:
    """F-601 模板字节流：表头（来源列带必填批注）+ 3 行示例（事实型/多跳/串库拒答）。

    组装逻辑与 xlsx_io.write_template 同构是刻意对齐（首行表头 + 示例行），
    差异仅在表头批注；示例行可直接整批导入（模板与校验规则同源，有钉死用例）。
    """
    wb = Workbook()
    ws = wb.worksheets[0]
    ws.append(TEMPLATE_HEADERS)
    for row in TEMPLATE_SAMPLES:
        ws.append(row)
    ws.cell(row=1, column=TEMPLATE_HEADERS.index("来源") + 1).comment = Comment(
        _SOURCE_NOTE, "系统"
    )
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
