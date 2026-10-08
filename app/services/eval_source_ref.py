r"""评测题来源列（source_ref）校验与同部门文档匹配（metrics-balance 0011，自 eval_service 拆出）。

spec 冻结口径（specs/metrics-balance.md §4.1）：非拒答题（expected≠`[拒答]` 且
qtype≠cross_reject）来源必填；逐行归一化（去首尾空格/去扩展名）后匹配同部门 documents
的 title 或 original_filename（去扩展名），精确或唯一后缀段匹配，必须恰好命中 1 篇 →
source_doc_id 落列；拒答类豁免校验、落 NULL。匹配只在 is_active 文档中进行——单一生效
版本唯一索引（uq_doc_active）保证同部门同名不歧义，软删行与历史版本不作为引用落点。
后缀段以分隔符为界（name 以「分隔符+ref」结尾才算段命中），防「手册」这类子串误命中
「客服手册」。整批"全或无"语义归 eval_service（本模块只产行级错误，格式与
_row_errors 同构：{"row": Excel 物理行号, "field": "来源", "reason": 中文原因}）。

匹配纯函数（给定候选做字符串判定）与候选装载（SQLAlchemy，无裸 SQL、无 f-string 拼接）
同置本文件；行数红线 §1.4 拆分，同 eval_template 先例。
"""

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document
from app.rag.precheck import ALLOWED_EXTS
from app.services.eval_template import REJECT_MARK

_FIELD = "来源"  # 错误报告的字段名 = Excel 列名（与 _row_errors 报告口径一致）
# 后缀段边界分隔符：标题规范命名（部门_主题_版本_日期）与常见文件名的段界
_DELIMS = ("_", "-", " ")


def strip_known_ext(value: str) -> str:
    """去扩展名：仅当最后一个「.」后缀是白名单扩展名（大小写不敏感）才剥。

    os.path.splitext 对「客服手册 3.2 节」会在「3.2」处误剥（得「客服手册 3」），
    故只认上传白名单（ALLOWED_EXTS）内的扩展名，其余后缀一律原样返回。
    """
    stem, ext = os.path.splitext(value)
    return stem if ext.lower() in ALLOWED_EXTS else value


def load_active_docs_by_dept(db: Session, dept_ids: set[int]) -> dict[int, list[Document]]:
    """装载本批涉及部门的 is_active 文档实体并按部门分组（导入期一次性取齐，逐行免查库）。"""
    if not dept_ids:
        return {}
    docs = db.scalars(
        select(Document).where(Document.dept_id.in_(dept_ids), Document.is_active.is_(True))
    ).all()
    by_dept: dict[int, list[Document]] = {}
    for doc in docs:
        by_dept.setdefault(doc.dept_id, []).append(doc)
    return by_dept


def validate_source_refs(
    rows: list[tuple[int, dict[str, str]]],
    norm_pairs: list[tuple[str, str]],
    dept_ids: dict[str, int],
    docs_by_dept: dict[int, list[Document]],
) -> tuple[list[dict], list[int | None]]:
    """逐行来源校验 + source_doc_id 解析（spec §4.1 冻结口径）。

    返回 (errors, doc_ids)，两者均与 rows 同序对齐：doc_ids 供入库循环直接取用，
    拒答类/未命中/部门未解析行落 None。norm_pairs 是 eval_service 已做的拒答双向
    归一结果（单一来源，本模块不重复归一），据 expected==[拒答] 判豁免——归一后
    cross_reject 行的 expected 必为 [拒答]，单一条件即覆盖两类拒答入口。部门名非法
    的行跳过匹配（_row_errors 已单独报错，此处无部门可匹配，不重复报）。
    """
    errors: list[dict] = []
    doc_ids: list[int | None] = []
    for (row_no, row), (_, expected) in zip(rows, norm_pairs, strict=True):
        if expected == REJECT_MARK:
            doc_ids.append(None)  # 拒答类豁免：来源不校验，source_doc_id 落 NULL
            continue
        raw_ref = row[_FIELD]
        ref = strip_known_ext(raw_ref.strip())
        if not ref:
            errors.append({"row": row_no, "field": _FIELD, "reason": "非拒答题来源必填"})
            doc_ids.append(None)
            continue
        hits = _match_dept_docs(ref, dept_ids.get(row["部门"]), docs_by_dept)
        doc_ids.append(hits[0] if len(hits) == 1 else None)
        if not hits:
            errors.append(
                {
                    "row": row_no,
                    "field": _FIELD,
                    "reason": f"来源「{raw_ref.strip()}」在本部门未匹配到文档",
                }
            )
        elif len(hits) > 1:
            errors.append(
                {
                    "row": row_no,
                    "field": _FIELD,
                    "reason": f"来源「{raw_ref.strip()}」匹配到多篇文档，请填写完整标题",
                }
            )
    return errors, doc_ids


def _match_dept_docs(
    ref: str, dept_id: int | None, docs_by_dept: dict[int, list[Document]]
) -> list[int]:
    """返回同部门命中文档 id 列表：title 或 original_filename 去扩展名后，ref 精确相等
    或为其分隔符边界的后缀段即命中；一篇文档任一名称命中只计一次。"""
    if dept_id is None:
        return []
    hits: list[int] = []
    for doc in docs_by_dept.get(dept_id, []):
        names = {
            strip_known_ext(doc.title.strip()),
            strip_known_ext(doc.original_filename.strip()),
        }
        if any(ref == name or _is_suffix_segment(ref, name) for name in names):
            hits.append(doc.id)
    return hits


def _is_suffix_segment(ref: str, name: str) -> bool:
    """ref 是否为 name 的后缀段：name 以 ref 结尾且紧邻字符是分隔符（真后缀，非全文相等）。"""
    if not ref or len(ref) >= len(name):
        return False
    return name.endswith(ref) and name[len(name) - len(ref) - 1] in _DELIMS
