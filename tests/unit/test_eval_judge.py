r"""eval_judge.judge_eval_item 单元测试（无 DB）：三判合一次调用与非法输出整题作废。

口径锚点（metrics-balance §2 裁决 3 / §4.3 / §6）：任一字段非法（解析失败/缺字段/
枚举外/context_relevant 长度不符）→ 返回 (None, judgeError)，绝不部分采纳、绝不判
wrong；UpstreamError（调用前失败/生成器形态首 next 失败/流中途断裂）同样返回失败
形态不向上抛且不做函数内重试（rewrite 同款异常边界，超时/重试归 llm 配置层）；
拒答类旁路在引擎侧，本函数不做——测试锁定 prompt 拒答声明存在 + 拒答回答按 LLM
判定透传不本地改判。
"""

from collections.abc import Iterator, Mapping, Sequence

import pytest

from app.core.llm import StubLLMProvider, UpstreamError
from app.rag.prompt import REFUSAL_TEXT
from app.services.eval_judge import JUDGE_MAX_TOKENS, JUDGE_PROMPT_VERSION, judge_eval_item

_QUESTION = "公司年假是怎么规定的"
_EXPECTED = "年假按工龄 5-10 天，需提前 3 个工作日申请"
_ANSWER = "年假按工龄 5-10 天，需提前申请 [1]"
_BLOCKS = ["年假制度：按工龄 5-10 天。", "病假制度。", "考勤制度。"]
_VALID_JSON = (
    '{"context_relevant": [true, false, true], "relevancy": 0.5, "correctness": "partial"}'
)


class _RecordingLLM:
    """替身：记录 (messages, max_tokens) 后按注入片段产出，供消息形状断言。"""

    def __init__(self, pieces: Sequence[str]) -> None:
        self.pieces = tuple(pieces)
        self.calls: list[tuple[list[dict[str, str]], int]] = []

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        self.calls.append(([dict(message) for message in messages], max_tokens))
        yield from self.pieces


class _GeneratorFailureLLM:
    """生成器函数形态替身：调用不抛、首次 next() 才抛（复刻真实现连接失败形态）。"""

    def chat_stream(self, messages: Sequence[Mapping[str, str]], max_tokens: int) -> Iterator[str]:
        raise UpstreamError("连接失败")
        yield ""  # 不可达：仅为把本函数标记为生成器函数（真实现同构）


def test_valid_json_reads_back_three_fields_and_calls_llm_once() -> None:
    """合法 JSON：三判逐字读回（逐块布尔/relevancy/correctness），且恰好一次 LLM 调用。"""
    llm = StubLLMProvider(chunks=(_VALID_JSON,))

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert error is None
    assert verdict is not None
    assert verdict.context_relevant == [True, False, True]
    assert verdict.relevancy == 0.5
    assert verdict.correctness == "partial"
    assert llm.stream_calls == 1


def test_integer_relevancy_one_is_accepted_as_legal_value() -> None:
    """JSON 整型 relevancy（1）按合法值接受：JSON 数字无整型/浮点之分，1 与 1.0 同义。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [true], "relevancy": 1, "correctness": "correct"}',),
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS[:1])

    assert error is None
    assert verdict is not None
    assert verdict.context_relevant == [True]
    assert verdict.relevancy == 1
    assert verdict.correctness == "correct"


def test_code_fenced_json_is_tolerated() -> None:
    """代码围栏包裹的合法 JSON 容错解析（prompt 已禁止围栏，模型偶发兜底，memory_extract 同款）。"""
    llm = StubLLMProvider(chunks=(f"```json\n{_VALID_JSON}\n```",))

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert error is None
    assert verdict is not None
    assert verdict.context_relevant == [True, False, True]
    assert verdict.correctness == "partial"


def test_invalid_json_fails_whole_item_without_any_verdict() -> None:
    """非法 JSON：整题失败形态 (None, judgeError)，无任何字段被采纳。"""
    llm = StubLLMProvider(chunks=("这不是JSON",))

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge 输出不是合法 JSON: '这不是JSON'"


def test_json_array_output_fails_as_not_an_object() -> None:
    """输出是 JSON 数组而非对象：整题失败（字段无从谈起）。"""
    llm = StubLLMProvider(chunks=("[]",))

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge 输出不是 JSON 对象"


def test_missing_field_fails_whole_item() -> None:
    """缺字段（缺 correctness）：整题失败，绝不部分采纳其余两判。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [true, true, true], "relevancy": 1}',),
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge 输出缺少字段: correctness"


def test_relevancy_out_of_enum_fails_whole_item() -> None:
    """relevancy 枚举外（0.8）：整题失败。"""
    llm = StubLLMProvider(
        chunks=(
            '{"context_relevant": [true, true, true], "relevancy": 0.8, "correctness": "correct"}',
        ),
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge 输出 relevancy 枚举外: 0.8"


def test_boolean_relevancy_true_is_rejected_as_out_of_enum() -> None:
    """relevancy 布尔 true 按枚举外拒绝（true==1 相等陷阱，bool 必须先拦）。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [true], "relevancy": true, "correctness": "correct"}',),
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS[:1])

    assert verdict is None
    assert error == "judge 输出 relevancy 枚举外: True"


def test_correctness_out_of_enum_fails_whole_item() -> None:
    """correctness 枚举外（"ok"）：整题失败。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [true, true, true], "relevancy": 0, "correctness": "ok"}',),
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge 输出 correctness 枚举外: 'ok'"


def test_context_relevant_length_mismatch_fails_whole_item() -> None:
    """context_relevant 长度 ≠ 资料块数（2 vs 3）：整题失败。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [true, true], "relevancy": 1, "correctness": "correct"}',),
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge 输出 context_relevant 长度 2 != 资料块数 3"


def test_non_boolean_context_relevant_items_fail_whole_item() -> None:
    """context_relevant 含非布尔项（[1, 0, 1]）：不是布尔数组，整题失败。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [1, 0, 1], "relevancy": 1, "correctness": "correct"}',),
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge 输出 context_relevant 不是布尔数组"


def test_upstream_failure_before_streaming_returns_error_without_retry() -> None:
    """调用前失败（fail_times=1）：返回失败形态不外抛，且不做函数内重试（恰 1 次调用）。"""
    llm = StubLLMProvider(chunks=(_VALID_JSON,), fail_times=1)

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge LLM 调用失败: stub chat_stream forced failure (call #1)"
    assert llm.stream_calls == 1


def test_generator_form_failure_on_first_next_returns_error() -> None:
    """生成器函数形态故障（首 next() 才抛）同样返回失败形态——锁定异常边界包住整个迭代。"""
    llm = _GeneratorFailureLLM()

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge LLM 调用失败: 连接失败"


def test_broken_stream_treated_as_upstream_failure_not_partial_parse() -> None:
    """流中途断裂（已产出半个 JSON）：按上游失败处理，绝不把部分产出当解析输入。"""
    llm = StubLLMProvider(
        chunks=(
            '{"context_relevant": [true, true], "relevancy": 0.5,',
            ' "correctness": "partial"}',
        ),
        break_after=1,
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS)

    assert verdict is None
    assert error == "judge LLM 调用失败: stub stream broken after 1 chunks"


def test_refusal_answer_passes_through_llm_verdict_without_local_override() -> None:
    """拒答话术回答按 prompt 声明交由 LLM 判 wrong，函数不做本地改判/旁路（旁路在引擎侧）。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [true], "relevancy": 0, "correctness": "wrong"}',),
    )

    verdict, error = judge_eval_item(llm, _QUESTION, _EXPECTED, REFUSAL_TEXT, _BLOCKS[:1])

    assert error is None
    assert verdict is not None
    assert verdict.correctness == "wrong"
    assert verdict.relevancy == 0
    assert llm.stream_calls == 1  # 拒答类旁路不在本函数：判分调用照常发生


def test_judge_messages_shape_and_untrusted_data_wrapping() -> None:
    """消息形状：system+user 两条；回答与资料块 <data> 包裹声明不可信；max_tokens 用模块常量。"""
    llm = _RecordingLLM((_VALID_JSON,))

    judge_eval_item(llm, _QUESTION, _EXPECTED, _ANSWER, _BLOCKS[:2])

    ((messages, max_tokens),) = llm.calls
    assert max_tokens == JUDGE_MAX_TOKENS
    assert [message["role"] for message in messages] == ["system", "user"]
    system = messages[0]["content"]
    for needle in (
        "评测判分器",
        "context_relevant",
        "relevancy",
        "correctness",
        "拒答话术",
        "correctness=wrong",
        "不可信数据",
        "<data>",
    ):
        assert needle in system
    user = messages[1]["content"]
    assert user.startswith("[评测问题]\n公司年假是怎么规定的")
    assert "[参考答案]\n年假按工龄 5-10 天，需提前 3 个工作日申请" in user
    assert "[待评回答]\n<data>\n年假按工龄 5-10 天，需提前申请 [1]\n</data>" in user
    assert "[资料片段]\n[1]\n<data>\n年假制度：按工龄 5-10 天。\n</data>" in user
    assert "[2]\n<data>\n病假制度。\n</data>" in user


def test_prompt_version_constant_is_v1() -> None:
    """JUDGE_PROMPT_VERSION 导出常量为 "v1"（config 快照 judgePromptVersion 接线归工单 07）。"""
    assert JUDGE_PROMPT_VERSION == "v1"


# ---------------------------------------------------------------------------
# judge_item_with_metrics：judge 预填 autoJudgment + 四指标装配（工单 07，spec §2/§4.3）
# ---------------------------------------------------------------------------

from app.db.models import EvalQuestion
from app.services.eval_judge import judge_item_with_metrics


def _scoring_question(
    expected: str = "年假按工龄 5-10 天", qtype: str = "factual", source_doc_id: int | None = 7
) -> EvalQuestion:
    """未入库的 ORM 对象直造（纯函数域无会话），字段与导入落库形态一致。"""
    return EvalQuestion(
        dept_id=1,
        question=_QUESTION,
        expected_answer=expected,
        source_ref=None,
        source_doc_id=source_doc_id,
        qtype=qtype,
    )


def test_非拒答题judge成功_预填autoJudgment与metrics五键读回() -> None:
    """三判合法：autoJudgment=judge correctness、judgeError=None；metrics 五键齐——
    recall 命中 1、citationsOk 角标合法 true、contextPrecision 排名加权、relevancy/
    correctness 按 1/0.5/0 折算。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [true, false], "relevancy": 0.5, "correctness": "partial"}',),
    )

    judgment, error, metrics = judge_item_with_metrics(
        llm,
        _scoring_question(),
        "年假按工龄 5-10 天 [1]",
        2,
        [(7, "年假制度。"), (8, "病假制度。")],
    )

    assert judgment == "partial"
    assert error is None
    assert metrics == {
        "recall": 1,
        "citationsOk": True,
        "contextPrecision": pytest.approx((1 / 1) / (1 / 1 + 1 / 2)),
        "relevancy": 0.5,
        "correctness": 0.5,
    }
    assert llm.stream_calls == 1


def test_拒答类旁路_规则自动判且metrics整体null不调llm() -> None:
    """拒答类（expected=[拒答]）：不调 judge，话术精确匹配判 correct；metrics=null
    （四指标全排除），judgeError=None。"""
    llm = StubLLMProvider(chunks=("不应被消费",))

    judgment, error, metrics = judge_item_with_metrics(
        llm,
        _scoring_question(expected="[拒答]", qtype="cross_reject", source_doc_id=None),
        REFUSAL_TEXT,
        0,
        [],
    )

    assert judgment == "correct"
    assert error is None and metrics is None
    assert llm.stream_calls == 0  # 拒答类旁路：judge LLM 零调用


def test_judge失败_整题pending且机检两键照算() -> None:
    """judge 输出非法（§2 裁决 3）：autoJudgment 留 pending + judgeError 落明，绝不判
    wrong；recall/citationsOk 机检不依赖 judge，照算；三判折算三键 null。"""
    llm = StubLLMProvider(chunks=("这不是JSON",))

    judgment, error, metrics = judge_item_with_metrics(
        llm, _scoring_question(), "年假按工龄 5-10 天", 2, [(7, "年假制度。")]
    )

    assert judgment == "pending"
    assert error is not None and error.startswith("judge 输出不是合法 JSON")
    assert metrics is not None
    assert metrics["recall"] == 1
    assert metrics["citationsOk"] is False  # 无角标=不可核对，机检必须红
    assert (metrics["contextPrecision"], metrics["relevancy"], metrics["correctness"]) == (
        None,
        None,
        None,
    )


def test_存量旧题无来源_recall为null_citationsOk照算() -> None:
    """source_doc_id=None：recall 记 null 不计入集均分；机检 citationsOk 与 judge 三判
    照常产出（指标互相独立）。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [true], "relevancy": 1, "correctness": "correct"}',),
    )

    judgment, error, metrics = judge_item_with_metrics(
        llm, _scoring_question(source_doc_id=None), "年假 5-10 天 [1]", 2, [(8, "考勤制度。")]
    )

    assert judgment == "correct"
    assert error is None
    assert metrics is not None
    assert metrics["recall"] is None
    assert metrics["citationsOk"] is True
    assert metrics["correctness"] == 1


def test_judge判wrong_折算0且预填wrong() -> None:
    """judge correctness=wrong：autoJudgment 预填 wrong、correctness 折算 0（pending 计 0
    口径的反例面：错误回答被 judge 拉入统计而非挂人工队列）。"""
    llm = StubLLMProvider(
        chunks=('{"context_relevant": [false], "relevancy": 0, "correctness": "wrong"}',),
    )

    judgment, error, metrics = judge_item_with_metrics(
        llm, _scoring_question(), "答非所问的内容", 1, [(7, "年假制度。")]
    )

    assert judgment == "wrong"
    assert error is None
    assert metrics is not None
    assert metrics["correctness"] == 0
    assert metrics["contextPrecision"] == 0.0
