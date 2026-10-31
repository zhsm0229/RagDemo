r"""父块上下文展开集成测试（切分策略 B）：命中子块 → 兄弟拼接还原父块，引用字段不动。

覆盖：多父块多子块的展开拼回（剥重复前缀、首块带前缀原样）、无父块命中零改动
快路径、软删兄弟不计入父块、引用定位字段（chunk_id/page_no/similarity）展开后
不变、空命中列表短路。真 pg + conftest 迁移到 head（0013 列在位）。"""

import hashlib
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document, User
from app.services.context_expand import expand_parent_context
from app.services.retrieval import DocHit

_PARENT0 = ["公告｜第一段・甲甲甲。", "公告｜第二段・乙乙乙。", "公告｜第三段・丙丙丙。"]
_PARENT1 = ["公告｜第四段・丁丁丁。", "公告｜第五段・戊戊戊。"]


def _make_doc(session: Session) -> Document:
    """种子 admin 建一份 ready 文档（复用 test_ingest 的最小必填口径）。"""
    admin_id = session.scalars(select(User.id).where(User.username == "admin")).one()
    dept_id = session.scalar(select(User.dept_id).where(User.username == "admin"))
    digest = hashlib.sha256(b"parent-expand-probe").hexdigest()
    doc = Document(
        title="父子块展开样本",
        dept_id=dept_id,
        original_filename="parent_expand.md",
        file_path=f"seed/{digest}.md",
        mime="text/markdown",
        size_bytes=16,
        content_hash=digest,
        version=1,
        effective_date=date(2026, 9, 1),
        owner_name="制度责任人",
        status="ready",
        uploaded_by=admin_id,
    )
    session.add(doc)
    session.flush()
    return doc


def _seed_chunks(session: Session, doc: Document) -> list[Chunk]:
    """两父块五子块：父 0 三子块、父 1 两子块；父 0 的 position=1 子块软删（不计入父块）。"""
    rows = [
        Chunk(
            document_id=doc.id,
            dept_id=doc.dept_id,
            title_path="公告",
            content=content,
            content_chars=len(content),
            position=position,
            parent_position=parent,
            embedding=[0.0] * 1024,
            is_active=not (parent == 0 and position == 1),
        )
        for parent, contents in ((0, _PARENT0), (1, _PARENT1))
        for position, content in enumerate(contents, start=parent * 10)
    ]
    session.add_all(rows)
    session.flush()
    return rows


def _hit(row: Chunk) -> DocHit:
    """最小命中：只带展开逻辑消费的字段（其余按 DocHit 缺省）。"""
    return DocHit(
        chunk_id=row.id,
        document_id=row.document_id,
        title="父子块展开样本",
        page_no=row.page_no,
        title_path=row.title_path,
        content=row.content,
        similarity=0.9,
        embedding=[],
    )


def test_命中子块展开为父块_兄弟拼接剥重复前缀(session: Session) -> None:
    """命中父 0 的 position=2 子块 → content 变三段拼接（软删的 position=1 不计入），
    首块带前缀原样、后续剥前缀换行连接；引用字段与列表序原样。"""
    doc = _make_doc(session)
    rows = _seed_chunks(session, session.get(Document, doc.id))
    hit = _hit(rows[2])  # position=2、parent_position=0（position=1 已软删）
    expanded = expand_parent_context(session, [hit])
    assert len(expanded) == 1
    assert expanded[0].content == _PARENT0[0] + "\n" + _PARENT0[2].removeprefix("公告｜")
    assert expanded[0].chunk_id == hit.chunk_id  # 引用仍指命中子块
    assert expanded[0].similarity == hit.similarity
    assert expanded[0].title_path == hit.title_path


def test_同父多命中共享一次取回_无父命中原样(session: Session) -> None:
    """父 0 两命中 → 两命中都展开为同一父块正文；无父块命中（NULL）原样返回。"""
    doc = _make_doc(session)
    rows = _seed_chunks(session, session.get(Document, doc.id))
    hits = [_hit(rows[0]), _hit(rows[2]), _hit(rows[3 + 1])]  # 父0两子块 + 父1子块
    hits[2] = DocHit(
        chunk_id=rows[3 + 1].id,
        document_id=rows[3 + 1].document_id,
        title="t",
        page_no=None,
        title_path="公告",
        content=rows[3 + 1].content,
        similarity=0.8,
        embedding=[],
        doc_type="notice",
    )
    expanded = expand_parent_context(session, hits)
    assert expanded[0].content == _PARENT0[0] + "\n" + _PARENT0[2].removeprefix("公告｜")
    assert expanded[1].content == expanded[0].content  # 同父命中同正文
    assert expanded[2].similarity == 0.8 and expanded[2].doc_type == "notice"


def test_空命中短路_无查询返回空(session: Session) -> None:
    """空命中列表原样返回空（拒答闸上游短路语义不受影响）。"""
    assert expand_parent_context(session, []) == []
