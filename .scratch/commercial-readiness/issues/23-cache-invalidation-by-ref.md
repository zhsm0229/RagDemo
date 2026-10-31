# 23: 按 key 缓存失效（clear_by_ref 语义分级）

**What to build:** ExactCache 增 `clear_by_ref(refType, refId)`：条目 citations 已带 docId/refId，锁内扫描精准删除，无 citations 条目保守删除（fail-safe）；10 处调用点按"是否改变可检索事实"语义分级改接（收缩/重置类精准、扩展可见性类整清、纯新增不失效）。补登于 2026-11-01 /code-review（spec 轴发现 22/23 无票开工，补登对账）。

**Blocked by:** None。

**Status:** done（2026-11-01，commit 8e5da60；语义分级裁决见 app/api/document_admin.py confirm 注释）

- [x] 先红：cache 单测（两条不同 docId 条目精准失效、无关保留、无 citations 保守删除）+ API 回归（既有"新答案立即生效"用例改造）
- [x] 实现 clear_by_ref（app/services/chat_cache.py）与 10 处调用点改接（document_admin×4、document_rebuild×2、admin_qa_pairs×3、QA 导入不失效）
- [x] 文档同步：api.md §1.2 失效总则 + 五处端点行改写 + 五处 docstring（bbba6aa）
