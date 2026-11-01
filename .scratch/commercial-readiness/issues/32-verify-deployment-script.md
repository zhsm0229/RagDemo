# 32: 部署机一键验收脚本（工单 13 窗口时间压缩）

**What to build:** 把工单 13 验收检查单（acceptance-status §三 + runbook §11）从 markdown 表格变可执行物：`deploy/verify-deployment.sh` 统一入口，聚合四个子检查（可 `--only` 单跑）——① 串库复测前置核对（客服版 v1.1 下线状态）+ 触发真实链路复测；② golden mineru 样本回归（env 开关已在）；③ 备份调度实测（backup.sh 一次真实执行 + 新鲜度探针）；④ **arm64 推理冒烟**（起 `ragdemo-tei-cpu-arm64` 容器→等 health→/embed 真实一问断言向量维度→teardown；本机 Docker VM 预算 OOM 的约束在部署机不存在）。每步结果 JSON 归档（nfr-results 同目录口径），任何 FAIL 非零退出——窗口内"跑脚本+看报告"替代"按清单逐条手敲"。

**Blocked by:** None (can start immediately)（脚本本机可写+干跑校验；真机执行仍归工单 13 部署机窗口——不变更其验收路径，只压缩窗口时间）。

**Status:** done（2026-11-01 实施：四子检查脚本化，本机干跑+golden 真跑全路径验证；**首次实跑即机检出一项治理漏项**）

- [x] verify-deployment.sh：四子检查 + `--only` / `--dry-run` / 结果 JSON（deploy/verify-results/<ts>.json）/ `[verify]` 时间戳日志 / 任一 FAIL 非零退出；备份新鲜度 age 计算 python 化（BSD date -j 与 GNU date -d 语法分叉的跨平台坑）
- [x] arm64 冒烟子步骤：镜像缺失给构建指引（tei-arm64.Dockerfile 指针）、health 120s 轮询、/embed 真实一问断言 bge-m3 1024 维数值向量、teardown 全脚本化；本机干跑验证（真机执行归工单 13 窗口——本机 OOM 约束不复检）
- [x] 本机验证：`--dry-run` 全路径打印 ✓、`--only golden` 真跑 13 passed ✓、JSON 归档 ✓、汇总表+非零退出 ✓
- [x] **首次实跑机检发现（脚本价值实证）**：dev/试点库 doc id=1（客服版 v1.1）仍 active——acceptance-status §三③"是否同步下线未在账本登记"待核对项实锤为治理漏项，登记 known-issues 开放-11 ⑤（复测窗口前 F-305 软删），脚本已把该项列为复测前置 FAIL 闸
- [x] runbook §11 检查单增"步骤 0：一键验收脚本"入口（1-4 多副本拓扑观察项仍人工核）
