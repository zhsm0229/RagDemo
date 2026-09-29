r"""评测判分与 config 快照（纯函数域，自 eval_engine 迁出）：键名与取值口径勿改。

为什么单独成文件：eval_engine.py 单文件 300 行零余量，而判分（_auto_judge）与收口
快照（_config_snapshot）是无 IO 纯函数，与后台任务体（会话管理、逐题 commit）关注点
不同——迁出后任务体专注执行，纯函数域可独立演进与单测；eval_engine import 使用，
detail/config 的消费方（Task 7 人工评判、docs/api.md）不受影响。
"""

from app.core.assembly import PromptPack
from app.core.config import Settings
from app.db.models import EvalQuestion
from app.rag.prompt import REFUSAL_TEXT
from app.services.eval_service import REJECT_MARK

_STUB_MODEL = "stub"  # settings.llm_model 为空（test_mode Stub 无模型名）时的落库回退名
_CORRECT, _WRONG, _PENDING = "correct", "wrong", "pending"


def _auto_judge(question: EvalQuestion, answer: str) -> str:
    """判定协议第 3 条：拒答类按话术精确匹配自动判；其余 pending（人工复核）。"""
    if question.expected_answer == REJECT_MARK or question.qtype == "cross_reject":
        return _CORRECT if answer == REFUSAL_TEXT else _WRONG
    return _PENDING


def _config_snapshot(settings: Settings, prompts: PromptPack) -> dict:
    """收口 config 快照 9 字段：阈值/模型/提示词版本/温度，防跨跑对比被污染。"""
    return {
        "model": settings.llm_model or _STUB_MODEL,
        "promptVersion": settings.prompt_version,
        "qaThreshold": settings.qa_threshold,
        "refuseThreshold": settings.refuse_threshold,
        "efSearch": settings.hnsw_ef_search,
        "topK": settings.retriever_top_k,
        "topN": settings.rerank_top_n,
        "temperature": prompts.temperature,
        "manifestVersion": prompts.version,
    }
