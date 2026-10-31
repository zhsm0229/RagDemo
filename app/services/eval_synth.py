r"""评测合成门面（03/04 票，工单 14 拆分后保留既有导入路径）。

实现拆分至三模块（各自 ≤300 行红线达标，语义零变化——既有 test_eval_synth/
test_doc_synth 全绿即回归钉）：
- eval_synth_seed：点踩种子变体通道（synthesize_from_seeds）
- eval_synth_doc：文档造题通道（synthesize_from_chunks + 零召回抽样）
- eval_synth_common：两通道共享的常量与 LLM 输出解析
调用方（eval_auto / admin_hardcase / 测试）导入路径不变。
"""

from app.services.eval_synth_common import extract_json
from app.services.eval_synth_doc import _docgen_candidates, synthesize_from_chunks
from app.services.eval_synth_seed import _grounding_chunks, synthesize_from_seeds

__all__ = [
    "_docgen_candidates",
    "_grounding_chunks",
    "extract_json",
    "synthesize_from_chunks",
    "synthesize_from_seeds",
]
