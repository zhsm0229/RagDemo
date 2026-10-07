r"""评测题库 xlsx 模板（F-601，自 eval_service.py 拆出，行数红线 §1.4）：表头/示例行/模板字节流。

部门列收中文枚举名（客服/人力/全员）——对接人填 Excel 用中文名，服务层映射 code
（F-401 傻瓜式原则，映射在 eval_service._DEPT_NAME_TO_CODE）。示例行三型：事实型 /
多跳 / 串库拒答（期望答案 [拒答]，判定协议第 3 条）。eval_service 转出再导出
REJECT_MARK/TEMPLATE_HEADERS/build_template，既有导入路径不变。
"""

from app.services.xlsx_io import write_template

TEMPLATE_HEADERS: list[str] = ["部门", "问题", "期望答案", "来源", "题型"]
REJECT_MARK: str = "[拒答]"  # 串库拒答题期望答案的约定格式（判定协议第 3 条）
TEMPLATE_SAMPLES: list[list[str]] = [
    [
        "客服",
        "员工如何重置门户登录密码？",
        "登录页点击「忘记密码」，通过绑定手机号验证后重置。",
        "客服手册 3.2 节",
        "factual",
    ],
    [
        "人力",
        "年假可以跨年结转吗？未休完的年假如何处理？",
        "当年度假额度次年 1 月 1 日清零，不跨年结转。",
        "员工手册 5.1 节",
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
    """F-601 模板字节流：表头 + 3 行示例（事实型/多跳/串库拒答，部门列用中文名）。"""
    return write_template(headers=TEMPLATE_HEADERS, sample_rows=TEMPLATE_SAMPLES)
