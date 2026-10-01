r"""语料治理拆分手册生成器单测（W6 Task 2）：串库漏拒语料侧治理的质量闸。

固化三层约束：
1. 治理完整性——全员版 v1.2 不得残留任何 HR 域条目（年假/病假正文、跨年结转、
   工资比例等 hr 制度语义），否则 service 视角串库复测必漏拒（W5 实测漏拒带
   0.6509-0.6953 全部撞「考勤与休假」块）；HR 版 v1.0 必须完整承接三节政策要点
   （年假 5/10/15 天、跨年结转至次年三月三十一日、病假 30 天复查/80% 工资），
   否则 hr 视角视角内命中率回归不了。
2. 可解析性——两份产物必须能被 DocxParser 原样解析出段落（上传管道前置校验，
   生成物进不了解析闸 = 演练当场上不了库）。
3. 确定性——两次构建字节一致（可重复运行覆盖输出语义）。
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from app.rag.parser import DocxParser

TOOLS = Path(__file__).resolve().parents[1] / "fixtures" / "tools"

# HR 域特征词：任一出现在全员版即治理失败（章节名与正文要点双重覆盖）
HR_DOMAIN_MARKS = (
    "年假",
    "病假",
    "考勤与休假",
    "跨年结转",
    "三月三十一日",
    "百分之八十",
    "复查",
)
# HR 版必须承接的政策要点（年假/病假/工作时间各节核心句片段）
HR_POLICY_MARKS = (
    "满十年者每年十天",
    "跨年结转至次年三月三十一日",
    "病假累计超过三十天",
    "百分之八十发放",
    "标准工作时间为每周一至周五",
)


def _tool(name: str) -> ModuleType:
    """按文件路径加载 fixtures/tools 下的生成工具（无包结构，直接 spec 加载）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _corpus_text(payload: bytes) -> str:
    """解析 docx 字节流取全文（标题路径 + 节内正文，测试与上传管道共用同一 parser）。"""
    doc = DocxParser().parse("员工手册.docx", payload)
    parts: list[str] = []
    for block in doc.blocks:
        parts.extend(block.heading_path)
        if block.text:
            parts.append(block.text)
    return "\n".join(parts)


def test_全员版v12_不含任何HR域条目():
    joined = _corpus_text(_tool("make_pilot_handbooks").build_all_handbook_v12())
    for mark in HR_DOMAIN_MARKS:
        assert mark not in joined, f"全员版残留 HR 域内容：{mark}"
    # 治理保留项与版本行逐一在场
    for kept in ("标准工作时间为每周一至周五", "差旅费用凭发票据实报销", "退货政策", "版本：v1.2"):
        assert kept in joined


def test_HR版v10_完整承接考勤与休假全节():
    joined = _corpus_text(_tool("make_pilot_handbooks").build_hr_handbook_v10())
    assert "考勤与休假" in joined
    for mark in HR_POLICY_MARKS:
        assert mark in joined, f"HR 版缺政策要点：{mark}"
    assert "版本：v1.0" in joined


def test_两份产物可被解析管道读取且确定性构建():
    module = _tool("make_pilot_handbooks")
    for build in (module.build_all_handbook_v12, module.build_hr_handbook_v10):
        assert _corpus_text(build()), "产物解析结果为空"
        assert build() == build(), "两次构建字节不一致（违背可重复运行语义）"
