r"""文档分型纯函数域（metrics-balance §3.1/§3.2）：结构信号自动检测 + 档位常量。

分层红线：本模块禁止 import services/db（纯函数域，golden 镜像按 spec §6 独立重算，
不 import 本模块私有件）。判定规则"先命中先判"（§3.1 优先级表）：条款 ≥5 处 >
markdown 表格 > 短文且浅层（通知）> 有子标题层级（手册）> 通用兜底。
档位常量 PROFILE_PARAMS 随代码评审冻结（§3.2）：查询期 top_n 的消费方在检索域
（工单 05），条款边界切分的消费方在工单 04——常量先行落位，防两处各自硬编码。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

# 检测与条款切分共用的编号词表（工单 04 起公开导出：chunker.split_clauses 引用，单一事实源）
CLAUSE_PATTERN = re.compile(r"第[一二三四五六七八九十百千零〇0-9０-９]+条")
_CLAUSE_MIN_HITS = 5  # ≥5 处条款编号才认条款档：防普通行文里偶发"第3条"式引用误判
_SHORT_DOC_CHARS = 1500  # 全文严格小于该字数且无子标题层级 → 通知公告
_SHALLOW_DEPTH = 2  # 标题路径深度 ≤2 视为浅层；≥2 存在子标题层级 → 操作手册信号
_TABLE_SEPARATOR = "|---"  # markdown 表格分隔行信号（出现即判表格档）
_TABLE_PIPE_MIN = 2  # 单行竖线 ≥2 个视为表格行
_TABLE_PIPE_RUN = 3  # 表格行连续 ≥3 行视为表格信号
_PATH_SEP = " > "  # 标题路径连接符（与 chunker.OutChunk.title_path 同源，勿改）


class DocProfile(StrEnum):
    """文档五档（DB 存 code，DTO 字段 docType 透出同一 code；中文文案映射在前端）。"""

    POLICY_CLAUSE = "policy_clause"  # 制度条款
    TABLE_REPORT = "table_report"  # 表格报表
    NOTICE = "notice"  # 通知公告
    OPERATION_MANUAL = "operation_manual"  # 操作手册
    GENERAL = "general"  # 通用（兜底）


@dataclass(frozen=True)
class AggregateParams:
    """标题聚合档参数：min_len/max_len/overlap_len/child_len 直传 chunker.split（§3.2 档位表）。

    overlap_len（切分策略 A）：被句切的多块相邻共享前块尾部的字数上限（整句优先、
    字符尾兜底），0=不重叠；单块节不注入。child_len（切分策略 B）：非 None 时每块
    再句切为 ≤child_len 子块——子块进向量库保召回精度，命中后按 parent_position
    拼回父块保上下文完整（services/context_expand）。"""

    min_len: int
    max_len: int
    top_n: int  # 查询期精排保留条数（工单 05 消费）
    overlap_len: int = 0  # 句级重叠上限（A）；0=旧行为
    child_len: int | None = None  # 子块切分上限（B）；None=本块即检索单元


@dataclass(frozen=True)
class ClauseParams:
    """条款边界档参数（仅 policy_clause）：切分实现归工单 04，常量先行冻结。

    单条超 max_clause_len 句切；不足 merge_below 的碎条并入下条（并入后允许超
    max_clause_len，同 chunker"min 优先于 max"先例）；top_n 查询期精排保留条数。"""

    max_clause_len: int
    merge_below: int
    top_n: int


PROFILE_PARAMS: dict[DocProfile, AggregateParams | ClauseParams] = {
    DocProfile.POLICY_CLAUSE: ClauseParams(max_clause_len=600, merge_below=120, top_n=5),
    DocProfile.OPERATION_MANUAL: AggregateParams(
        min_len=400, max_len=1000, top_n=6, overlap_len=100
    ),
    DocProfile.TABLE_REPORT: AggregateParams(
        min_len=300, max_len=1500, top_n=5, overlap_len=100, child_len=400
    ),
    DocProfile.NOTICE: AggregateParams(
        min_len=800, max_len=2000, top_n=3, overlap_len=100, child_len=400
    ),
    DocProfile.GENERAL: AggregateParams(min_len=300, max_len=800, top_n=5, overlap_len=100),
}

# 查询期 top_n 生效模式标识（§5，评测 config 快照辨识用）：主问答链路恒按 top1
# 命中型取档；对比基线（固定 int）只存在于 §17.3 双轮跑分的旧代码轮，不进本代码路径
TOPN_MODE_BY_DOC_TYPE = "by_doc_type"

_DEFAULT_TOP_N = PROFILE_PARAMS[DocProfile.GENERAL].top_n  # general 档 5：缺省兜底（§3.2）


class ProfiledHit(Protocol):
    """按型取档的最小结构接口：带 doc_type 的命中对象即满足。

    retrieval.DocHit 结构化满足本协议——rag 域不得反向 import services（模块头红线），
    故以 Protocol 表达依赖而非导入具体类型。
    """

    doc_type: str


def resolve_top_n(hits: Sequence[ProfiledHit]) -> int:
    """查询期精排保留条数按型取档（§3.2 参数化）：top1 命中 hit 的 doc_type 决定档位。

    调用时点由消费方保证：rerank 分排序完成后以 ranked（分降序）序列传入，此时
    top1 即最高分命中型。空序列、或 doc_type 不在五档（异常/旧数据）→ general
    缺省 5 条（spec：top_n=5 缺省）。
    """
    if not hits:
        return _DEFAULT_TOP_N
    try:
        profile = DocProfile(hits[0].doc_type)
    except ValueError:
        return _DEFAULT_TOP_N
    return PROFILE_PARAMS[profile].top_n


def detect_from_text(title: str, title_paths: list[str], full_text: str) -> DocProfile:
    """五档判定（§3.1 优先级表，先命中先判）。

    三参口径由调用方适配器保证：title 为文档标题；title_paths 为标题路径串列表
    （" > " 连接）；full_text 为正文全文（不含 title_path 前缀）。title 入参当前
    不参与规则（签名随 spec 冻结，为 LLM 兜底分型预留），调用方必须照实传入。
    """
    if _count_clauses(full_text) >= _CLAUSE_MIN_HITS:
        return DocProfile.POLICY_CLAUSE
    if _has_table_signal(full_text):
        return DocProfile.TABLE_REPORT
    depth = _max_depth(title_paths)
    if len(full_text) < _SHORT_DOC_CHARS and depth <= _SHALLOW_DEPTH:
        return DocProfile.NOTICE
    if depth >= _SHALLOW_DEPTH:
        return DocProfile.OPERATION_MANUAL
    return DocProfile.GENERAL


def _count_clauses(full_text: str) -> int:
    return len(CLAUSE_PATTERN.findall(full_text))


def _has_table_signal(full_text: str) -> bool:
    """markdown 表格信号：分隔行出现即判；或表格行（竖线 ≥2）连续 ≥3 行即判。"""
    run = 0
    for line in full_text.splitlines():
        if _TABLE_SEPARATOR in line:
            return True
        run = run + 1 if line.count("|") >= _TABLE_PIPE_MIN else 0
        if run >= _TABLE_PIPE_RUN:
            return True
    return False


def _max_depth(title_paths: list[str]) -> int:
    """标题路径最大深度：路径段数（空串/空列表=1，即无子标题层级）。"""
    return max((path.count(_PATH_SEP) + 1 for path in title_paths), default=1)
