r"""VectorStore 协议（第六可替换接口，规范/规格审查补齐）：向量检索的替换缝。

standards §1.2「可替换点一律接口化，共六个」与 scaling-plan §1.7 B 阶段「启用
VectorStore 第二实现迁 Milvus」的承载物：问答链路（prepare_turn）与检索测试经本
协议消费向量召回与 QA 匹配。pgvector 实现落在 services/retrieval.PgVectorStore；
实现选择在装配层 app/main.py._build_chat_deps（services 层协议不进 core/assembly
——五层单向依赖，core 不得 import services），B 阶段新增 Milvus 实现时仅装配分支
变化，业务链路零改动。

协议方法签名与 pg 实现逐字对应：dept_ids 恒为 [用户部门, 全员]（F-502 由实现强制，
调用方不得绕过）；db: Session 按调用传入（实现无状态，事务边界归调用方，services
层惯例）。分数口径（standards §4.4）见 retrieval 模块 docstring，两套分数绝不互比。
"""

from typing import Protocol

from sqlalchemy.orm import Session

from app.services.retrieval import DocHit, QaHit


class VectorStore(Protocol):
    """向量检索实现契约：QA 匹配 + 文档块召回（pgvector 为首个实现）。"""

    def match_qa(
        self, db: Session, query_vec: list[float], dept_ids: list[int], threshold: float
    ) -> QaHit | None:
        """最高分问答对且余弦相似度 ≥ threshold 才命中；否则 None（qa 阈值口径）。"""
        ...

    def search_chunks(
        self, db: Session, query_vec: list[float], dept_ids: list[int], top_k: int, ef_search: int
    ) -> list[DocHit]:
        """向量召回 top_k（部门隔离 + 强制 JOIN documents）；similarity 暂存召回余弦。"""
        ...
