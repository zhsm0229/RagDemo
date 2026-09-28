r"""黄金样本回归骨架（standards §10.7/§15.7）：参数化读 golden/manifest.json，对每个样本
走 parse→质量红线→清洗→乱码检测→分块 全链断言 expect。

样本缺失或 pending=true 时 pytest.skip（显式原因），并由 tests/integration/conftest.py
的 session 级 pytest_terminal_summary 在汇总区打印缺失清单（§15.1 禁静默跳过；
hook 挂 conftest 是因为测试模块里的 pytest_terminal_summary 不会触发，已实测）。
真实样例到位后由对接人投放到清单标注路径并置 pending=false（见 golden/README.md）。

红线与乱码判定口径刻意在 run_chain 内独立重算（与 ingest_pipeline._check_quality 一致
而非 import 其私有函数）：解析/清洗实现漂移时黄金回归必须变红，这正是回归的目的。
"""

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from app.core.config import Settings
from app.core.errors import ERR_PARSE_FAILED, ApiError
from app.rag.chunker import Block, ParsedDoc, split
from app.rag.clean import RAW_CHARS, clean_text
from app.rag.parser import DocxParser, MinerUParser, Parser, PdfFallbackParser, get_parser
from app.rag.precheck import GARBLE_THRESHOLD, garbled_ratio

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"  # tests/fixtures
MANIFEST_PATH = FIXTURES / "golden" / "manifest.json"
TOOLS = FIXTURES / "tools"
# 钉死进程内解析路径（python-docx/pypdf），不随部署环境 MINERU_BASE_URL 漂移；mineru
# 类 expect 的样本（当前均 pending）待 W2 接入 MinerU 后改此一处改为读环境配置。
GOLDEN_SETTINGS = Settings(jwt_secret="golden-regression", mineru_base_url="")
GARBLE_REASON: str = "编码异常"  # F-1005 失败原因前缀，与 ingest_pipeline 文案对齐
KINDS: frozenset[str] = frozenset({"clean", "dual_col", "table", "scanned", "garbled"})
ENGINES: frozenset[str] = frozenset({"python-docx", "mineru", "pypdf"})


@dataclass(frozen=True)
class ChainOutcome:
    """全链产物：引擎名与各阶段量化结果，供逐项断言 expect。"""

    engine: str
    raw_chars: int
    cleaned_chars: int
    garbled: float
    block_count: int
    chunk_count: int


def _tool(name: str) -> ModuleType:
    """按文件路径加载 fixtures/tools 下的生成工具（无包结构，直接 spec 加载）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_entry(entry: Any) -> None:
    """manifest 条目 schema 校验（收集期即失败，防对接人填错静默溜过）。"""
    assert isinstance(entry, dict), f"条目须为对象：{entry!r}"
    assert {"file", "kind", "expect"} <= set(entry), f"条目缺必填键：{entry!r}"
    filename, kind, expect = entry["file"], entry["kind"], entry["expect"]
    assert isinstance(filename, str) and filename.startswith("golden/")
    assert filename.endswith((".docx", ".pdf")), f"黄金样本仅收 docx/pdf：{filename}"
    assert kind in KINDS, f"kind 非法：{kind!r}"
    assert isinstance(expect, dict), f"expect 须为对象：{filename}"
    assert {"engine", "min_chars", "min_blocks", "garbled_ok", "fail_reason"} <= set(expect), (
        f"expect 缺必填键：{filename}"
    )
    assert expect["engine"] in ENGINES, f"engine 非法：{filename}"
    assert isinstance(expect["min_chars"], int) and expect["min_chars"] >= 0, filename
    assert isinstance(expect["min_blocks"], int) and expect["min_blocks"] >= 0, filename
    assert isinstance(expect["garbled_ok"], bool), f"garbled_ok 须为布尔：{filename}"
    fail_reason = expect["fail_reason"]
    # fail_reason 仅建模乱码失败：garbled_ok ⟺ fail_reason="编码异常"，二者必须成对
    assert fail_reason is None or fail_reason == GARBLE_REASON, f"fail_reason 非法：{filename}"
    assert (fail_reason == GARBLE_REASON) == (expect["garbled_ok"] is True), (
        f"garbled_ok 与 fail_reason 不成对：{filename}"
    )
    if fail_reason is None:  # 全链通过的样本：清洗字符与分块数必须给出正下限
        assert expect["min_chars"] >= 1 and expect["min_blocks"] >= 1, filename


def load_manifest() -> list[dict[str, Any]]:
    """读 golden/manifest.json 并逐条 schema 校验，返回清单（顺序即执行顺序）。"""
    entries: Any = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(entries, list) and entries, "manifest 须为非空数组"
    for entry in entries:
        _validate_entry(entry)
    return cast("list[dict[str, Any]]", entries)


def missing_samples() -> list[str]:
    """待投放/待确认样本清单（文件名+投放路径），跳过原因与会话汇总共用此单一事实来源。"""
    lines: list[str] = []
    for entry in load_manifest():
        filename = str(entry["file"])
        target = (FIXTURES / filename).as_posix()  # 展示统一 posix 斜杠，跨平台可断言
        if entry.get("pending") is True:
            lines.append(
                f"待用户提供（pending）：{filename}"
                f"（投放 {target}，样例验证通过后请置 pending=false）"
            )
        elif not Path(target).exists():
            lines.append(f"待用户提供样例：{filename}（投放 {target}）")
    return lines


def _engine_name(parser: Parser) -> str:
    """解析器实现 → manifest 引擎词汇（python-docx/mineru/pypdf），与 get_parser 调度树对齐。"""
    if isinstance(parser, DocxParser):
        return "python-docx"
    if isinstance(parser, MinerUParser):
        return "mineru"
    if isinstance(parser, PdfFallbackParser):
        return "pypdf"
    raise AssertionError(f"未知解析器实现，请同步 KINDS/ENGINES：{type(parser).__name__}")


def run_chain(filename: str, content: bytes) -> ChainOutcome:
    """黄金样本全链：parse→红线→清洗→乱码检测→分块，各阶段量化产物供断言。

    红线（§10.3：清洗后 < max(100, 清洗前×10%) 判失败）与乱码阈值（F-1005：>5%）的
    判定顺序、分母口径均与 ingest_pipeline._check_quality 一致，且独立重算不 import。
    """
    parser = get_parser(filename, GOLDEN_SETTINGS)
    parsed = parser.parse(filename, content)
    raw = "\n".join(block.text for block in parsed.blocks)  # 清洗前全文＝红线分母（§10.3）
    cleaned_blocks = [Block(b.level, b.heading_path, clean_text(b.text)) for b in parsed.blocks]
    cleaned = "\n".join(block.text for block in cleaned_blocks)
    if len(cleaned) < max(100, RAW_CHARS(raw) * 0.1):
        raise ApiError(ERR_PARSE_FAILED, "疑似扫描件或空文档，有效内容不足", 422)
    ratio = garbled_ratio(cleaned)
    if ratio > GARBLE_THRESHOLD:
        raise ApiError(ERR_PARSE_FAILED, f"{GARBLE_REASON}：乱码字符占比 {ratio:.1%} 超过阈值", 422)
    chunks = split(ParsedDoc(title=parsed.title, blocks=cleaned_blocks))
    return ChainOutcome(
        engine=_engine_name(parser),
        raw_chars=RAW_CHARS(raw),
        cleaned_chars=len(cleaned),
        garbled=ratio,
        block_count=len(parsed.blocks),
        chunk_count=len(chunks),
    )


def test_manifest_schema() -> None:
    """清单 schema：种子三件套（clean docx/pdf + garbled docx 占位）真跑，其余三类 pending。"""
    entries = load_manifest()
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_kind.setdefault(str(entry["kind"]), []).append(entry)
    assert set(by_kind) == {"clean", "dual_col", "table", "scanned", "garbled"}, "五类必须都有条目"
    assert len(by_kind["clean"]) == 2  # 规整类 docx+pdf 种子各一（§10.7 双格式起点）
    assert len(by_kind["garbled"]) == 1
    for kind in ("dual_col", "table", "scanned"):  # 待真实样例的三类必须标 pending
        assert [e for e in by_kind[kind] if e.get("pending") is True], f"{kind} 缺 pending 条目"
    seeds = {(str(e["file"]), str(e["expect"]["engine"])) for e in entries if "pending" not in e}
    assert seeds == {
        ("golden/hr_制度_v1.docx", "python-docx"),
        ("golden/admin_handbook_v1.pdf", "pypdf"),
        ("golden/hr_乱码_v1.docx", "python-docx"),
    }


def test_missing_samples_lists_unplaced_only() -> None:
    """缺失清单：只含 pending 与文件缺失条目，每条带文件名与投放路径；种子不列入。"""
    lines = missing_samples()
    assert lines, "当前 dual_col/table/scanned 三类待投放，清单不得为空（禁静默跳过）"
    seeds = ("golden/hr_制度_v1.docx", "golden/admin_handbook_v1.pdf", "golden/hr_乱码_v1.docx")
    for line in lines:
        assert "待用户提供" in line and "tests/fixtures" in line
        assert not any(seed in line for seed in seeds), f"种子不应列入缺失清单：{line}"
    assert len(lines) == 3  # dual_col/table/scanned 各一条（pending）
    assert all("pending" in line for line in lines)


def test_missing_samples_lists_absent_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件缺失（非 pending）分支：只缺的文件入清单，存在的文件不列入（skip 行为的收集断言）。"""
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    (golden_dir / "present.docx").write_bytes(b"placeholder")
    (golden_dir / "manifest.json").write_text(
        json.dumps(
            [
                {
                    "file": "golden/present.docx",
                    "kind": "clean",
                    "expect": {
                        "engine": "python-docx",
                        "min_chars": 1,
                        "min_blocks": 1,
                        "garbled_ok": False,
                        "fail_reason": None,
                    },
                },
                {
                    "file": "golden/absent.pdf",
                    "kind": "dual_col",
                    "pending": False,
                    "expect": {
                        "engine": "pypdf",
                        "min_chars": 1,
                        "min_blocks": 1,
                        "garbled_ok": False,
                        "fail_reason": None,
                    },
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(f"{__name__}.FIXTURES", tmp_path)
    monkeypatch.setattr(f"{__name__}.MANIFEST_PATH", golden_dir / "manifest.json")
    absent = (golden_dir / "absent.pdf").as_posix()
    assert missing_samples() == [f"待用户提供样例：golden/absent.pdf（投放 {absent}）"]


def test_committed_seeds_match_builder_output() -> None:
    """入库种子与生成工具再产物的解析结果一致（防样本漂移；字节级比对因 zip 时间戳不可用）。"""
    builder = _tool("make_golden_seed")
    cases = [
        ("golden/hr_制度_v1.docx", "hr_制度_v1.docx", builder.build_clean_docx()),
        ("golden/admin_handbook_v1.pdf", "admin_handbook_v1.pdf", builder.build_clean_pdf()),
        ("golden/hr_乱码_v1.docx", "hr_乱码_v1.docx", builder.build_garbled_docx()),
    ]
    for rel, name, fresh in cases:
        parser = get_parser(name, GOLDEN_SETTINGS)
        committed = parser.parse(name, (FIXTURES / rel).read_bytes())
        assert committed == parser.parse(name, fresh), f"种子漂移：{rel}"


@pytest.mark.parametrize(
    "entry",
    load_manifest(),
    ids=[Path(str(entry["file"])).stem for entry in load_manifest()],
)
def test_golden_regression(entry: dict[str, Any]) -> None:
    """逐样本全链回归：缺失/pending 显式跳过；失败类断言失败原因；通过类逐项断言 expect。"""
    filename = str(entry["file"])
    target = (FIXTURES / filename).as_posix()
    expect = cast("dict[str, Any]", entry["expect"])
    if entry.get("pending") is True:
        pytest.skip(f"待用户提供（pending）：{filename}（投放 {target}，验证后置 pending=false）")
    if not Path(target).exists():
        pytest.skip(f"待用户提供样例：{filename}（投放 {target}）")
    content = Path(target).read_bytes()
    if expect["fail_reason"] is not None:  # 乱码类：全链在乱码检测处失败且原因可核对
        with pytest.raises(ApiError) as exc_info:
            run_chain(filename, content)
        assert exc_info.value.code == ERR_PARSE_FAILED
        assert exc_info.value.http_status == 422
        assert exc_info.value.message.startswith(GARBLE_REASON)
        return
    outcome = run_chain(filename, content)
    assert outcome.engine == expect["engine"]
    assert outcome.cleaned_chars >= max(100, outcome.raw_chars * 0.1)  # §10.3 红线显式断言
    assert outcome.cleaned_chars >= int(expect["min_chars"])
    assert outcome.block_count >= 1
    assert outcome.chunk_count >= int(expect["min_blocks"])
    assert outcome.garbled <= GARBLE_THRESHOLD
