r"""agent 任务档案（工单 #06）：taskType → 系统提示 + 工具集，白名单与编排共用一份。

为什么单列：api 层白名单（未放开 taskType 一律 422）与 services 层编排（系统提示/
绑定工具集）必须同源——两处各写一份必然漂移（"任意类型都跑同一个最小 agent"的
口径漂移正是白名单要防的）。每接入一个任务 = 本表加一行评审一次：提示词过红线
（拒答红线、无审核不落地），工具集最小化（不因共享骨架顺手放开工具）。

工具名是注册表键（装配层 main._build_agent_deps 注册）：profile 引用了未注册的
工具名属装配缺陷，编排层 registry.get 会 KeyError fail-fast（响亮暴露，不静默缩集）。
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TaskProfile:
    """一个任务类型的编排档案：系统提示（注入领域约束与红线）+ 绑定工具集（最小化）。"""

    system_prompt: str
    tool_names: tuple[str, ...]


_KB_QA_PROMPT = "你是知识库助手"  # 工单 #07 最小 agent 任务，保持原提示不动

# 工单 #06：模板生成首任务（HR 制度/客服话术）。few-shot 参照取自知识库既有制度/话术
# （检索工具），产物一律经 save_draft 落草稿待审核（specs §五 无审核不落地）；检索持续
# 空手而归时以"知识库中未找到相关内容"收尾（拒答红线在 agent 场景的等价物）。
_TEMPLATE_DRAFT_PROMPT = (
    "你是企业知识库的模板生成助手，负责起草 HR 制度、客服话术等结构化模板。规则：\n"
    "1. 先用 knowledge_base_search 检索知识库中既有的相关制度/话术作为参照（few-shot），"
    "模板结构尽量与库内同类文档一致；\n"
    "2. 产出为 Markdown 结构化模板，只能基于检索到的内容与通用最佳实践，不得编造公司"
    "特定的事实（人名、数字、日期等）；\n"
    "3. 完成起草后必须调用 save_draft 保存草稿（参数 title/content），草稿进入人工审核，"
    "审核通过前不会进入知识库；\n"
    '4. 若检索不到任何可参照内容且无法可靠起草，直接告知"知识库中未找到相关内容"，'
    "不得编造成稿，也不得调用 save_draft；\n"
    "5. 最终回复用中文简要说明草稿标题与后续流程（等待管理员审核）。"
)

# 白名单即评审点：新增任务在此登记（api 校验与编排同源消费）
TASK_PROFILES: dict[str, TaskProfile] = {
    "kb_qa": TaskProfile(
        system_prompt=_KB_QA_PROMPT,
        tool_names=("knowledge_base_search",),
    ),
    "template_draft": TaskProfile(
        system_prompt=_TEMPLATE_DRAFT_PROMPT,
        tool_names=("knowledge_base_search", "save_draft"),
    ),
}


def get_profile(task_type: str) -> TaskProfile:
    """按 taskType 取档案：未知类型 KeyError fail-fast（api 白名单已挡，此处只兜装配缺陷）。"""
    return TASK_PROFILES[task_type]


# B5：api 层请求校验的白名单 Literal 从本表动态派生（单一真相源）——新增任务 = 本表加
# 一行，api 校验与 OpenAPI enum 自动跟随，杜绝"两处各写一份白名单"的漂移。动态构造
# Literal[tuple(keys)] 在 import 期求值一次即冻结，Pydantic/OpenAPI 按静态枚举处理；
# 选动态派生而非"显式同步 + 契约测试"的理由：契约测试只能事后报警，派生从机制上
# 不可漂移（测试仍保留作为回归防撤销，见 tests/api/test_agent_runs.py）。
TaskType = Literal[tuple(TASK_PROFILES)]
