# 15: NFR 性能取证脚本化（deploy/nfr-check.sh）

**What to build:** 商用交付的性能证据链可复现：一条命令在目标硬件产出 NFR-01/02/03 实测数字并随版本归档——验收时拿得出、降级时比得了。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] deploy/nfr-check.sh：并发 SSE 计时（首 token P50/P95，NFR-01 口径 <3s）、检索延迟抽样（NFR-02 <1.5s）、20 并发×2min 稳态（NFR-03）
- [ ] 依赖目标机可达 app 与测试账号（E2E_PASSWORD 同源，不落字面量）；输出 JSON+人读摘要
- [ ] 结果归档口径：写入 deploy/releases.md 对应版本行（或 nfr-results/<tag>.json）
- [ ] dev 机实测一轮并归档（CPU TEI 基线数字，作为商用对照基线）
- [ ] runbook 增补"交付前跑 nfr-check"步骤
