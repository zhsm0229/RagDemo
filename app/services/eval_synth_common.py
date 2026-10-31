r"""评测合成共用件（工单 14 拆分）：常量与 LLM 输出解析——两通道共享的单一事实源。"""

import json
import re
from typing import Any

_MAX_QUESTION_CHARS: int = 2000  # 与题库导入行级校验同口径（eval_service）
_MAX_ANSWER_CHARS: int = 10000  # 同上
_MAX_LLM_TOKENS: int = 1024  # 变体+答案 的输出预算（JSON 结构小文本）
_DOCGEN_MIN_CLEAN_CHARS: int = 100  # 短块闸：清洗后 <100 字不出题（质量红线 §10.3 同款）
_DOCGEN_ZERO_RECALL_DAYS: int = 30  # 零召回口径：last_hit_at 为空或早于 30 天（概览同款）

_PROMPT_SYSTEM: str = (
    "你是企业知识库的评测题生成助手。只输出一个 JSON 对象，"
    '形如 {"variants": ["问题1", "问题2"], "expected_answer": "答案"}，'
    "不输出任何其他文字、解释或代码围栏。"
)
_PROMPT_USER_TEMPLATE: str = (
    "请基于给定知识块，为原问题生成 {count} 个评测变体问题，并给出期望答案。\n"
    "要求：\n"
    "1. 变体保持与原问题相同的问意与考察点，变换句式、称谓或提问粒度；\n"
    "2. expected_answer 必须逐字出自知识块原文（可拼接多处原文，不得自行编造或补充）——"
    "知识块不足以支撑答案时，expected_answer 返回空字符串；\n"
    "3. 变体问题不得出现知识块之外的专有名词。\n\n"
    "原问题：{question}\n\n知识块：\n{context}"
)
_DOCGEN_PROMPT_SYSTEM: str = (
    "你是企业知识库的评测题生成助手。只输出一个 JSON 对象，"
    '形如 {"question": "问题", "answer": "答案"}，'
    "不输出任何其他文字、解释或代码围栏。"
)
_DOCGEN_PROMPT_USER_TEMPLATE: str = (
    "请基于给定知识块出一道评测问答题（考察该块的核心事实）。\n"
    "要求：\n"
    "1. question 是员工会真实问出的问题（口语化、不带文档标题词）；\n"
    "2. answer 必须逐字出自知识块原文（可拼接多处原文，不得自行编造或补充）——"
    "知识块不足以支撑答案时，question 与 answer 均返回空字符串；\n\n"
    "知识块标题路径：{title_path}\n知识块正文：\n{context}"
)


def extract_json(text: str) -> dict[str, Any] | None:
    """解析 LLM 输出 JSON：容忍 ```json 围栏（eval_judge 同款口径），非 dict 即失败。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
