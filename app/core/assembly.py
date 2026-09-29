r"""装配工厂：按 Settings 装配 Embedder/Reranker/LLMProvider 三件套与提示词包。

装配关切与 tei.py/openai_compat.py 同层：test_mode 返回确定性 Stub（app/core/llm.py，
同输入必同输出、可注故障），否则返回 TEI / OpenAI 兼容真实现实例（激活在 Task 3/4，
本模块只负责装配与参数传递）。非 test_mode 且 llm_base_url 为空时 build_llm
fail-fast（standards §6.4）。load_prompt_pack 读仓库根 prompts/ 三文件（缺文件
RuntimeError），version 以 chat_system.txt 头注释 `# version:` 行为唯一锚点并与
manifest.json 交叉校验——代码内不另设版本常量（standards §17.4 模板与生成参数同版本化）。
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.llm import (
    Embedder,
    LLMProvider,
    Reranker,
    StubEmbedder,
    StubLLMProvider,
    StubReranker,
)
from app.core.openai_compat import OpenAICompatLLMProvider
from app.core.tei import TeiEmbedder, TeiReranker

# 仓库根 = app 包上一级：以本文件位置为锚解析 prompts/，测试与运行路径一致
PROMPTS_DIR: Path = Path(__file__).resolve().parents[2] / "prompts"
_VERSION_HEADER = "# version:"


@dataclass
class PromptPack:
    """提示词包：问答/改写系统提示、版本（取自头注释）与生成温度（取自 manifest）。"""

    chat_system: str
    rewrite_system: str
    version: str
    temperature: float


def build_embedder(settings: Settings) -> Embedder:
    """test_mode → StubEmbedder；否则 TEI 真实现（端点/超时取自 Settings）。"""
    if settings.test_mode:
        return StubEmbedder()
    return TeiEmbedder(base_url=settings.embed_base_url, timeout_s=settings.embed_timeout_s)


def build_reranker(settings: Settings) -> Reranker:
    """test_mode → StubReranker；否则 TEI rerank 真实现（端点/超时取自 Settings）。"""
    if settings.test_mode:
        return StubReranker()
    return TeiReranker(base_url=settings.rerank_base_url, timeout_s=settings.rerank_timeout_s)


def build_llm(settings: Settings) -> LLMProvider:
    """test_mode → StubLLMProvider；否则 OpenAI 兼容真实现，base_url 缺失 fail-fast。"""
    if settings.test_mode:
        return StubLLMProvider()
    if not settings.llm_base_url:
        raise RuntimeError("llm_base_url 未配置")  # §6.4 fail-fast：装配期暴露配置缺口
    return OpenAICompatLLMProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_s=settings.llm_timeout_s,
    )


def _parse_version(chat_system: str) -> str:
    """从 chat_system.txt 首行 `# version: v1` 头注释提取版本号（首 token，忽略尾注）。"""
    lines = chat_system.splitlines()
    first_line = lines[0].strip() if lines else ""
    if not first_line.startswith(_VERSION_HEADER):
        raise RuntimeError(f"chat_system.txt 缺少 {_VERSION_HEADER} 版本头注释：{first_line!r}")
    tokens = first_line[len(_VERSION_HEADER) :].split()
    if not tokens:
        raise RuntimeError(f"chat_system.txt 版本头注释缺少版本号：{first_line!r}")
    return tokens[0]


def load_prompt_pack(prompts_dir: Path | str | None = None) -> PromptPack:
    """读 prompts/ 三文件装配 PromptPack；缺文件/版本不一致 → RuntimeError（fail-fast）。

    prompts_dir 缺省为仓库根 prompts/（PROMPTS_DIR），测试可注入 tmp_path 目录。
    """
    root = Path(prompts_dir) if prompts_dir is not None else PROMPTS_DIR
    chat_path = root / "chat_system.txt"
    rewrite_path = root / "rewrite_system.txt"
    manifest_path = root / "manifest.json"
    missing = [path.name for path in (chat_path, rewrite_path, manifest_path) if not path.is_file()]
    if missing:
        raise RuntimeError(f"提示词包文件缺失：{'、'.join(missing)}（目录：{root}）")
    chat_system = chat_path.read_text(encoding="utf-8")
    rewrite_system = rewrite_path.read_text(encoding="utf-8")
    version = _parse_version(chat_system)
    try:
        manifest: dict = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"manifest.json 不是合法 JSON：{exc}") from exc
    if "temperature" not in manifest:
        raise RuntimeError("manifest.json 缺少 temperature 字段")
    manifest_version = manifest.get("version")
    if manifest_version != version:
        raise RuntimeError(
            f"manifest.json version {manifest_version!r} 与 chat_system.txt 头注释 {version!r} 不一致"
        )
    return PromptPack(
        chat_system=chat_system,
        rewrite_system=rewrite_system,
        version=version,
        temperature=float(manifest["temperature"]),
    )
