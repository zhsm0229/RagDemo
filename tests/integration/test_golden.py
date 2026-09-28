r"""黄金样本回归骨架（standards §10.7/§15.7）：参数化读 golden/manifest.json，对每个样本
走 parse→质量红线→清洗→乱码检测→分块 全链断言 expect。

样本缺失或 pending=true 时 pytest.skip（显式原因），并由 tests/integration/conftest.py
的 session 级 pytest_terminal_summary 在汇总区打印缺失清单（§15.1 禁静默跳过；
hook 挂 conftest 是因为测试模块里的 pytest_terminal_summary 不会触发，已实测）。
真实样例到位后由对接人投放到清单标注路径并置 pending=false（见 golden/README.md）。

清单断言一律写成对投放状态不敏感的不变式（种子在册且生效 / pending 按 kind 整体
声明 / 缺失清单覆盖全部 pending 条目）：对接人投放样例、增删条目不需要改测试；
与清单状态相关的硬编码值只集中在 _GOLDEN_SEEDS 一处（替换程序化种子时同步）。

红线与乱码判定口径刻意在 run_chain 内独立重算（与 ingest_pipeline._check_quality 一致
而非 import 其私有函数）：解析/清洗实现漂移时黄金回归必须变红，这正是回归的目的。
"""

import importlib.util
import json
from copy import deepcopy
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
ENGINES: frozenset[str] = frozenset({"docx", "mineru", "pypdf"})  # 与 documents.parse_engine 同词汇
# 程序化种子三件套（make_golden_seed.py 生成，可直接重生成）：替换种子时同步此处与
# golden/README.md。不变式只要求它们"在册且生效"，不锁清单总量（对投放状态不敏感）。
_GOLDEN_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("golden/hr_制度_v1.docx", "clean", "docx"),
    ("golden/admin_handbook_v1.pdf", "clean", "pypdf"),
    ("golden/hr_乱码_v1.docx", "garbled", "docx"),
)


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
    """读 golden/manifest.json 并逐条 schema 校验，返回清单（顺序即执行顺序）。

    额外不变式：file 全局唯一——手工编辑清单时同一文件重复登记会让回归静默漏跑。
    """
    entries: Any = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(entries, list) and entries, "manifest 须为非空数组"
    for entry in entries:
        _validate_entry(entry)
    files = [str(entry["file"]) for entry in entries]
    assert len(files) == len(set(files)), f"manifest 存在重复 file 条目：{files}"
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
    """解析器实现 → manifest 引擎词汇（docx/mineru/pypdf），与 get_parser 调度树对齐。

    与 documents.parse_engine 同一词汇（ingest_pipeline 把 parser.engine 直接落库）：
    黄金回归断言的引擎值即未来入库值，两套词汇并存只会在投放真实样例时产生映射成本。
    """
    if isinstance(parser, DocxParser):
        return "docx"
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


def check_seed_entries(entries: list[dict[str, Any]]) -> None:
    """不变式①：程序化种子逐条在册且"生效"（子集断言，替代旧精确等值）。

    manifest 无 is_active 字段，"生效"即非 pending 且文件在盘。只要求三条种子始终
    在册、kind/engine 正确，不锁清单总量与种子集合精确等值——投放真实样例、扩充
    清单后本断言不变红。
    """
    by_file: dict[str, dict[str, Any]] = {}
    for entry in entries:
        by_file[str(entry["file"])] = entry
    assert len(by_file) == len(entries), "manifest 存在重复 file 条目"
    for rel, kind, engine in _GOLDEN_SEEDS:
        entry = by_file.get(rel)
        assert entry is not None, f"程序化种子缺条目：{rel}（被移除请同步 _GOLDEN_SEEDS）"
        assert str(entry["kind"]) == kind, f"种子 kind 漂移：{rel}"
        assert str(entry["expect"]["engine"]) == engine, f"种子 engine 漂移：{rel}"
        assert entry.get("pending") is not True, f"种子不得 pending：{rel}"
        assert (FIXTURES / rel).exists(), f"种子文件缺失：{rel}"


def check_pending_uniformity(entries: list[dict[str, Any]]) -> None:
    """不变式②：pending 按 kind 整体声明，且种子永不 pending。

    某 kind 一旦有 pending 行则该 kind 全部行都须 pending（一半在册一半待投放的
    混合状态只可能是清单填错）；程序化种子是回归的基线，不得落入待投放集合。
    """
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_kind.setdefault(str(entry["kind"]), []).append(entry)
    seed_files = {rel for rel, _, _ in _GOLDEN_SEEDS}
    for kind, rows in by_kind.items():
        pendings = [row for row in rows if row.get("pending") is True]
        if not pendings:
            continue
        files = [str(row["file"]) for row in rows]
        assert len(pendings) == len(rows), f"{kind} pending 状态混杂：{files}"
        overlap = seed_files & {str(row["file"]) for row in pendings}
        assert not overlap, f"种子不得 pending：{sorted(overlap)}"


def test_manifest_schema() -> None:
    """清单不变式：五类齐全；种子逐条在册且生效；pending 按 kind 整体声明。"""
    entries = load_manifest()
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_kind.setdefault(str(entry["kind"]), []).append(entry)
    assert set(by_kind) == {"clean", "dual_col", "table", "scanned", "garbled"}, "五类必须都有条目"
    check_seed_entries(entries)
    check_pending_uniformity(entries)


def test_missing_samples_lists_unplaced_only() -> None:
    """缺失清单不变式：每条格式完备、不含种子；全部 pending 条目逐条在列。

    不做行数等值断言（投放几条就少几条，行数随清单状态自然变化）；存在 pending
    条目时清单非空（禁静默跳过），全部投放后清单为空同样合法。
    """
    entries = load_manifest()
    lines = missing_samples()
    seeds = {rel for rel, _, _ in _GOLDEN_SEEDS}
    pendings = [str(entry["file"]) for entry in entries if entry.get("pending") is True]
    if pendings:
        assert lines, "存在 pending 条目时清单不得为空（禁静默跳过）"
    for line in lines:
        assert "待用户提供" in line and "tests/fixtures" in line
        assert not any(seed in line for seed in seeds), f"种子不应列入缺失清单：{line}"
    for filename in pendings:  # 固定样本名逐条覆盖：pending 条目必须出现在缺失清单
        assert any(filename in line for line in lines), f"pending 条目未列入缺失清单：{filename}"


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
                        "engine": "docx",
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


def _tmp_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]
) -> Path:
    """临时 fixtures 目录：写入给定清单 + 种子占位文件，并重定向模块路径常量。"""
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    (golden_dir / "manifest.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )
    for rel, _, _ in _GOLDEN_SEEDS:
        (golden_dir / Path(rel).name).write_bytes(b"placeholder")  # 不变式只查在盘，不解析
    monkeypatch.setattr(f"{__name__}.FIXTURES", tmp_path)
    monkeypatch.setattr(f"{__name__}.MANIFEST_PATH", golden_dir / "manifest.json")
    return tmp_path


def test_invariants_survive_sample_arrival(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """投放模拟：真实样例到位（去 pending + 文件在盘）后不变式仍全绿，缺失清单同步缩水。

    旧断言（种子集合精确等值、dual_col/table/scanned 必须有 pending 行、行数==3）
    在此变体下全部翻红——这正是本次改写要拆除的"地雷"。
    """
    arrived = deepcopy(load_manifest())
    for entry in arrived:
        if entry["kind"] == "dual_col":
            del entry["pending"]  # 模拟对接人投放双栏真实样例
    fixtures = _tmp_fixtures(tmp_path, monkeypatch, arrived)
    (fixtures / "golden" / "hr_制度_双栏_v1.pdf").write_bytes(b"placeholder")

    entries = load_manifest()
    check_seed_entries(entries)
    check_pending_uniformity(entries)
    lines = missing_samples()
    assert all("hr_制度_双栏_v1.pdf" not in line for line in lines)  # 已投放不再列入
    assert any("hr_制度_表格_v1.docx" in line for line in lines)  # 其余 pending 仍在列


def test_invariants_survive_extra_samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """扩充模拟：新增第四条 clean 样本（非 pending、文件在盘）后不变式仍全绿。

    旧断言（clean 类恰好 2 条）在此变体下翻红；新不变式只关心种子与 pending 结构。
    """
    extended = deepcopy(load_manifest())
    extended.append(
        {
            "file": "golden/hr_制度_新增_v1.docx",
            "kind": "clean",
            "expect": {
                "engine": "docx",
                "min_chars": 1,
                "min_blocks": 1,
                "garbled_ok": False,
                "fail_reason": None,
            },
        }
    )
    fixtures = _tmp_fixtures(tmp_path, monkeypatch, extended)
    (fixtures / "golden" / "hr_制度_新增_v1.docx").write_bytes(b"placeholder")

    entries = load_manifest()
    check_seed_entries(entries)
    check_pending_uniformity(entries)


def test_invariants_reject_pending_tamper() -> None:
    """负面对照（不变式有牙）：种子被误标 pending / 同 kind pending 混杂时必须翻红。"""
    seeded_pending = deepcopy(load_manifest())
    # 取 garbled 种子（单行 kind）：uniformity 检查的混杂分支不先触发，精确命中种子交集分支
    next(e for e in seeded_pending if e["file"] == _GOLDEN_SEEDS[2][0])["pending"] = True
    with pytest.raises(AssertionError, match="种子不得 pending"):
        check_seed_entries(seeded_pending)
    with pytest.raises(AssertionError, match="种子不得 pending"):
        check_pending_uniformity(seeded_pending)

    mixed = deepcopy(load_manifest())  # table 类当前单行 pending：补一行非 pending 即混杂
    mixed.append(
        {
            "file": "golden/hr_制度_表格_v2.docx",
            "kind": "table",
            "expect": {
                "engine": "docx",
                "min_chars": 1,
                "min_blocks": 1,
                "garbled_ok": False,
                "fail_reason": None,
            },
        }
    )
    with pytest.raises(AssertionError, match="pending 状态混杂"):
        check_pending_uniformity(mixed)


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
