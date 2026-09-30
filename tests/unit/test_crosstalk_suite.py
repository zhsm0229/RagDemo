r"""串库双向评测题库生成器单测（Task 8）：T9 串库走查弹药的质量闸。

固化两层约束：
1. 生成物必须能被后端导入链原样消化——xlsx_io.read_rows 结构校验 →
   eval_service._row_errors 行级业务校验零错误 → _normalize_reject 拒答双向归一
   后仍为 (cross_reject, [拒答])。表头/部门中文枚举/题型枚举/拒答标记任何一处与
   后端漂移，此处即红（生成器不复制字面量，直接 import 后端常量，本测试兜底）。
2. 全员对照题期望答案与配套语料 make_sample_docx 的固定文本逐一比对——语料内容
   漂移会让对照题必假阴性（防假阴性设计反被假语料击穿）；生成须确定性（两次构建
   字节一致），保证「可重复运行覆盖输出」语义。
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from app.rag.parser import DocxParser
from app.services.eval_service import REJECT_MARK, TEMPLATE_HEADERS, _normalize_reject, _row_errors
from app.services.xlsx_io import read_rows

TOOLS = Path(__file__).resolve().parents[1] / "fixtures" / "tools"
REQUIRED_HEADERS = {"部门", "问题", "期望答案"}  # 与 eval_service.import_eval_questions 同口径


def _tool(name: str) -> ModuleType:
    """按文件路径加载 fixtures/tools 下的生成工具（无包结构，直接 spec 加载）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _suite_rows() -> list[tuple[int, dict[str, str]]]:
    """生成 → 后端结构校验读回（测试与演练共用同一读取路径）。"""
    suite = _tool("make_crosstalk_suite")
    data = suite.build_crosstalk_suite()
    return read_rows(data, TEMPLATE_HEADERS, 5000, required=REQUIRED_HEADERS)


def test_题库结构_24行且表头与后端模板一致():
    rows = _suite_rows()
    assert len(rows) == 24
    assert TEMPLATE_HEADERS == ["部门", "问题", "期望答案", "来源", "题型"]


def test_行级业务校验零错误_可整批导入不退回():
    assert _row_errors(_suite_rows()) == []


def test_人力串库组10条_归一后仍为拒答_题面标注合成题():
    rows = [row for _, row in _suite_rows() if row["部门"] == "人力"]
    assert len(rows) == 10
    for row in rows:
        assert row["题型"] == "cross_reject"
        assert row["期望答案"] == REJECT_MARK
        assert row["来源"] == ""  # 串库拒答题无期望出处（后端模板示例同风格）
        assert row["问题"].startswith("演练用合成题")
        assert _normalize_reject(row["题型"], row["期望答案"]) == ("cross_reject", REJECT_MARK)


def test_客服串库组10条_与人力组对称():
    rows = [row for _, row in _suite_rows() if row["部门"] == "客服"]
    assert len(rows) == 10
    for row in rows:
        assert row["题型"] == "cross_reject"
        assert row["期望答案"] == REJECT_MARK
        assert row["问题"].startswith("演练用合成题")
        assert _normalize_reject(row["题型"], row["期望答案"]) == ("cross_reject", REJECT_MARK)


def test_全员对照组4条_事实型非拒答且注明任一视角应命中():
    rows = [row for _, row in _suite_rows() if row["部门"] == "全员"]
    assert len(rows) == 4
    for row in rows:
        assert row["题型"] == "factual"
        assert row["期望答案"] != REJECT_MARK
        assert "任一视角应命中" in row["期望答案"]
        assert row["来源"] != ""  # 对照题必须指名语料出处（缺语料即假阴性）
        assert row["问题"].startswith("演练用合成题")


def test_对照组期望答案与对照语料固定文本逐一命中():
    suite = _tool("make_crosstalk_suite")
    sample = _tool("make_sample_docx")
    doc = DocxParser().parse("sample.docx", sample.build_sample_docx())
    corpus_text = "\n".join(block.text for block in doc.blocks)
    for control in suite.CONTROL_QUESTIONS:
        assert control["keyword"] in corpus_text, f"语料缺关键词：{control['keyword']}"


def test_构建确定性_两次生成字节一致():
    suite = _tool("make_crosstalk_suite")
    assert suite.build_crosstalk_suite() == suite.build_crosstalk_suite()


def test_命令行入口_输出到指定路径且内容与构建函数一致(tmp_path):
    suite = _tool("make_crosstalk_suite")
    out = tmp_path / "crosstalk.xlsx"
    sys.argv = ["make_crosstalk_suite.py", "-o", str(out)]
    suite.main()
    assert out.read_bytes() == suite.build_crosstalk_suite()
