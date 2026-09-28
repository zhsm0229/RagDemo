r"""integration 目录级 pytest 扩展：黄金样本缺失清单的会话汇总（§15.1 禁静默跳过）。

hook 只能挂 conftest/插件——测试模块里的 pytest_terminal_summary 不会触发（已实测），
故独立成本文件。清单本身取 test_golden.missing_samples()（单一事实来源），按文件路径
spec 加载，不依赖 sys.modules 的模块名状态。
"""

import importlib.util
from pathlib import Path
from typing import Any

_GOLDEN_TEST = Path(__file__).with_name("test_golden.py")


def pytest_terminal_summary(terminalreporter: Any) -> None:  # pytest 未导出 TerminalReporter 类型
    """会话汇总区打印待投放黄金样本（文件名+投放路径）；清单不可读时显式报告不吞掉。"""
    try:
        spec = importlib.util.spec_from_file_location("_golden_summary", _GOLDEN_TEST)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        missing: list[str] = module.missing_samples()
    except (OSError, ValueError, AssertionError, ImportError) as exc:
        # 汇总钩子不因清单损坏中断测试报告，但缺失信息必须显式可见（禁静默）
        terminalreporter.section("黄金样本缺失清单读取失败（请检查 golden/manifest.json）", red=True)
        terminalreporter.write_line(f"{type(exc).__name__}: {exc}")
        return
    if not missing:
        return
    terminalreporter.section(
        f"黄金样本缺失清单（{len(missing)} 条待投放，禁止静默跳过，投放说明见 golden/README.md）",
        red=True,
    )
    for line in missing:
        terminalreporter.write_line(line)
