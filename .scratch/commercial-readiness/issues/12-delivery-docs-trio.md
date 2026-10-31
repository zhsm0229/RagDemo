# 12: 交付物文案三件——限制清单 + arm64 说明 + AGENTS 数字刷新

**What to build:** 面向客户的交付文档补齐三处（PM/开发 A 评审 P1-7 与 P2 汇总）：
1. "部署形态边界与已知限制"一页：单实例约束（多 worker 重复调度风险，scheduler.py:145 自认）、重启即丢进程内态（登出黑名单/登录限流/精确缓存）、TEI 资源要求、CPU 档 rerank 120s 豁免——写入验收单附件，防商用期扯皮。
2. deploy-requirements.md 补 arm64 说明：当前全文无 arm64/Rosetta 字样，客户 arm64 服务器 `docker compose pull` 即失败且无文档指引（知识在 deploy/mac-setup/README.md:88,95 有但未搬入）；prod compose TEI 服务加平台注记锚点。
3. AGENTS.md"当前状态"数字刷新：主门禁复绿后（工单 02）更新为实测口径（1204+ passed、日期），W5 终审台账失效引用（AGENTS.md:84 指向不存在的 .superpowers/sdd/.../progress.md）修正或重建指针。

**Blocked by:** 02（数字刷新须在门禁复绿后回填）。

**Status:** ready-for-agent

- [ ] 限制清单页成文并挂接验收单模板
- [ ] deploy-requirements.md 硬件表系统行显式"仅支持 linux/amd64；arm64 需换 TEI 基镜像（参照 mac-setup §7）"，prod compose 加注释锚点
- [ ] AGENTS.md 数字刷新 + W5 台账引用修复（定位或重建终审节，归档 docs/archive/ 并修正三处指针）
- [ ] user-guide 与 help 双源同步（若涉及界面文案）
