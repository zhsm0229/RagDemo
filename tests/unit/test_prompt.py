r"""prompt 组装纯函数测试：注入防线（<data> 包裹、question 只在 user role）与格式逐字断言。

断言来源 task-6-brief/task-6b-brief + standards §6.7/§17.5 + CHAT-03：REFUSAL_TEXT
与标准拒答话术各自硬编码逐字相等（防漂移）；资料段 "[n] 标题｜位置" 逐字格式
（全角竖线｜；来源类型由 ContextSource.ref_type 显式判别，page 不参与类型推断——
doc 位置 = title_path（page 非 None 时附"｜第N页"），qa 位置 = 问答对）；chat
messages 恰 2 条、system 原文透传、question 只出现在 user role；rewrite messages
三轮历史逐字断言、助手内容超 200 字截断加省略号、全部历史原样渲染（取最近 3 轮由
调用方截断）。
"""

import pytest

from app.rag.prompt import (
    REFUSAL_TEXT,
    ContextSource,
    build_chat_messages,
    build_context_block,
    build_rewrite_messages,
)


def _source(**overrides: object) -> ContextSource:
    """doc 来源基线构造：测试按需覆写单字段，避免逐字断言被样板噪声淹没。"""
    base: dict[str, object] = {
        "n": 1,
        "ref_type": "doc",
        "title": "员工手册",
        "title_path": "人事制度 > 员工手册",
        "page": 3,
        "content": "试用期三个月。",
    }
    base.update(overrides)
    return ContextSource(**base)  # type: ignore[arg-type] — 键集即为 ContextSource 字段


def test_refusal_text_is_chat03_exact_phrase() -> None:
    """REFUSAL_TEXT 与 CHAT-03 标准拒答话术逐字相等（常量与字面量各自硬编码，防漂移）。"""
    assert REFUSAL_TEXT == "知识库中未找到相关内容"


def test_context_block_doc_segment_with_page() -> None:
    """doc 命中（page 非 None）：location = title_path｜第N页，逐字格式含两个全角竖线。"""
    block = build_context_block([_source()])
    assert block == "[1] 员工手册｜人事制度 > 员工手册｜第3页\n试用期三个月。"


def test_context_block_doc_without_page_omits_page_segment() -> None:
    """doc 命中 page 为 None：location = title_path（省略页码段），绝不误标"问答对"。"""
    block = build_context_block([_source(page=None, content="本手册适用于全体员工。")])
    assert block == "[1] 员工手册｜人事制度 > 员工手册\n本手册适用于全体员工。"
    assert "问答对" not in block


def test_context_block_qa_segment_renders_qa_label() -> None:
    """qa 命中（ref_type="qa"）：location 固定为"问答对"，title_path 与 page 不参与渲染。"""
    block = build_context_block(
        [
            _source(
                n=2,
                ref_type="qa",
                title="退货时限是多久",
                title_path="",
                page=None,
                content="签收后 7 天内。",
            )
        ]
    )
    assert block == "[2] 退货时限是多久｜问答对\n签收后 7 天内。"


def test_context_block_type_is_explicit_not_inferred_from_page() -> None:
    """显式判别：类型只由 ref_type 决定——qa 即使携带 page 也渲染"问答对"，不翻转为 doc。"""
    block = build_context_block(
        [
            _source(
                n=3,
                ref_type="qa",
                title="退货时限",
                title_path="",
                page=5,
                content="签收后 7 天内。",
            )
        ]
    )
    assert block == "[3] 退货时限｜问答对\n签收后 7 天内。"


def test_context_block_unknown_ref_type_rejected() -> None:
    """ref_type 仅收 doc/qa：未知值显式拒绝（防静默误标——本任务修复的缺陷类别）。"""
    with pytest.raises(ValueError, match="doc/qa"):
        build_context_block([_source(ref_type="web")])


def test_context_block_multi_segments_joined_in_input_order() -> None:
    """多段以单个换行相接，按输入顺序渲染不重排（编号与资料段一一对应 §6.7）。"""
    block = build_context_block(
        [
            _source(
                n=2, title="退货政策", title_path="客服 > 退货政策", page=12, content="七日无理由。"
            ),
            _source(
                n=1,
                ref_type="qa",
                title="员工手册",
                title_path="人事 > 手册",
                page=None,
                content="问答对内容。",
            ),
        ]
    )
    assert block == (
        "[2] 退货政策｜客服 > 退货政策｜第12页\n七日无理由。\n[1] 员工手册｜问答对\n问答对内容。"
    )


def test_context_block_empty_items_returns_empty_string() -> None:
    """空命中返回空串：是否走拒答链路（CHAT-03）由调用方决定。"""
    assert build_context_block([]) == ""


def test_chat_messages_shape_and_data_wrapping() -> None:
    """恰 2 条消息；system 原文透传；user 内容 = <data> 包裹资料段 + 末尾问题（§6.7）。"""
    system = "你是企业内部智能工作台的问答助手。"
    block = "[1] 员工手册｜人事 > 手册｜第3页\n试用期三个月。"
    messages = build_chat_messages(system, block, "试用期多久？")
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"] == system
    assert messages[1]["content"] == (
        "<data>\n[1] 员工手册｜人事 > 手册｜第3页\n试用期三个月。\n</data>\n\n试用期多久？"
    )


def test_chat_messages_question_only_in_user_role() -> None:
    """§17.5：question 只出现在 user role，绝不进 system prompt。"""
    question = "退货时限是多久"
    messages = build_chat_messages("系统提示词原文", "[1] 标题｜问答对\n内容", question)
    assert question in messages[1]["content"]
    assert all(question not in m["content"] for m in messages if m["role"] == "system")


def test_rewrite_messages_three_rounds_exact_format() -> None:
    """三轮历史逐字断言：[对话] 段按时间升序渲染，[当前问题] 段在 user 内容最后。"""
    history = [
        ("user", "退货时限？"),
        ("assistant", "签收后 7 天内可退。"),
        ("user", "运费谁出？"),
    ]
    messages = build_rewrite_messages("改写系统提示", history, "那质量问题呢")
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"] == "改写系统提示"
    assert messages[1]["content"] == (
        "[对话]\n用户：退货时限？\n助手：签收后 7 天内可退。\n用户：运费谁出？\n"
        "[当前问题]\n那质量问题呢"
    )


def test_rewrite_messages_assistant_content_truncated_at_200_chars() -> None:
    """助手内容超 200 字截断加省略号（用户内容不截断）。"""
    long_answer = "好" * 250
    messages = build_rewrite_messages("s", [("assistant", long_answer)], "q")
    rendered = messages[1]["content"]
    assert rendered == f"[对话]\n助手：{'好' * 200}…\n[当前问题]\nq"
    assert "好" * 201 not in rendered


def test_rewrite_messages_exactly_200_chars_not_truncated() -> None:
    """恰 200 字不截断（阈值语义为"超 200 字才截"）。"""
    answer = "好" * 200
    messages = build_rewrite_messages("s", [("assistant", answer)], "q")
    assert messages[1]["content"] == f"[对话]\n助手：{'好' * 200}\n[当前问题]\nq"


def test_rewrite_messages_renders_all_history_caller_truncates() -> None:
    """brief 约定"取最近 3 轮由调用方截断"：本函数渲染传入的全部消息，不自行截条数。"""
    roles = ["user", "assistant", "user", "assistant"]
    history = [(role, f"消息{i}") for i, role in enumerate(roles)]
    messages = build_rewrite_messages("s", history, "q")
    assert messages[1]["content"] == (
        "[对话]\n用户：消息0\n助手：消息1\n用户：消息2\n助手：消息3\n[当前问题]\nq"
    )


def test_rewrite_messages_long_user_content_not_truncated() -> None:
    """截断只针对助手内容：用户 250 字消息原样保留（改写需要完整追问原文）。"""
    long_question = "问" * 250
    messages = build_rewrite_messages("s", [("user", long_question)], "q")
    assert f"用户：{'问' * 250}" in messages[1]["content"]
