r"""父块上下文展开（切分策略 B，metrics-balance §3.2 父子块档）：命中子块 → 拼回父块正文。

notice/table_report 档入库的是 ≤child_len 子块（embedding 粒度=小段，召回精度），
本模块在精排折叠**之后**把命中子块的 content 替换为其所属父块完整正文（同文档同
parent_position 的兄弟块按 position 拼接，剥去重复的标题路径前缀）——LLM 上下文
完整，而引用仍指命中子块（chunk_id/page_no/similarity 不动，证据定位精度不降）。

parent_position 由 chunk_id 现查（不进 DocHit/search_chunks：retrieval.py 已在
行数红线，且展开是编排层可选步骤，管理端检索测试 RET-03/04 保持原始子块视角）。
部门隔离无需重查：命中已过部门闸，兄弟块同文档同部门；软删兄弟不计入父块
（is_active 过滤，父块可能缺段——可接受，检索可见性与父块还原同口径）。
"""

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Chunk
from app.services.retrieval import DocHit


def expand_parent_context(db: Session, hits: list[DocHit]) -> list[DocHit]:
    """折叠后的命中子块替换为父块正文；其余字段（含 similarity 排序）原样。

    无父子块档（全部命中 parent_position NULL）走快路径原样返回，零额外查询；
    同父多命中共用一次兄弟取回（进程内去重）。"""
    if not hits:
        return hits
    parent_texts: dict[tuple[int, int], str] = {}
    expanded: list[DocHit] = []
    for hit in hits:
        row = db.execute(select(Chunk.parent_position).where(Chunk.id == hit.chunk_id)).first()
        if row is None or row[0] is None:
            expanded.append(hit)
            continue
        key = (hit.document_id, row[0])
        if key not in parent_texts:
            parent_texts[key] = _parent_text(db, *key)
        text = parent_texts[key]
        expanded.append(replace(hit, content=text) if text else hit)
    return expanded


def _parent_text(db: Session, document_id: int, parent_position: int) -> str:
    """兄弟块按 position 拼回父块：首块带前缀原样，其余剥本块 title_path 前缀换行连接。"""
    rows = db.execute(
        select(Chunk.title_path, Chunk.content)
        .where(
            Chunk.document_id == document_id,
            Chunk.parent_position == parent_position,
            Chunk.is_active.is_(True),
        )
        .order_by(Chunk.position)
    ).all()
    if not rows:  # 理论不可达（命中子块自身在列）；防御空串交调用方回退原 hit
        return ""
    first_content = rows[0][1]
    rest = "".join(
        "\n" + content.removeprefix(f"{title_path}｜") for title_path, content in rows[1:]
    )
    return first_content + rest
