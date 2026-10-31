# 22: 生效日期前置闸（检索排除未生效文档）

**What to build:** 检索语句 WHERE 前置 `Document.effective_date <= CURRENT_DATE`（DB 时钟 UTC 口径，与到期清扫一致），未生效文档不可召回；零签名改动（search_chunks/VectorStore 协议/四调用点全不动），评测引擎/检索测试页自动同享。补登于 2026-11-01 /code-review（spec 轴发现 22/23 无票开工，补登对账）。

**Blocked by:** None。

**Status:** done（2026-11-01，commit 113c6cd；设计先行为 `.scratch/commercial-readiness/issues/` 阶段二收口连带项）

- [x] 先红测试：tests/integration/test_retrieval.py 行为用例（effective_date=明天 → 不召回；今天 → 召回）+ 既有编译断言更新
- [x] 实现 `_chunk_search_statement` WHERE 追加生效闸（retrieval.py:263）
- [x] 文档同步：standards §11.4 改写、retrieval-policy §1 基线句、api.md 检索范围补句（bbba6aa）
