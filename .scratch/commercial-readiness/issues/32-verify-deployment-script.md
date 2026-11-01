# 32: 部署机一键验收脚本（工单 13 窗口时间压缩）

**What to build:** 把工单 13 验收检查单（acceptance-status §三 + runbook §11）从 markdown 表格变可执行物：`deploy/verify-deployment.sh` 统一入口，聚合四个子检查（可 `--only` 单跑）——① 串库复测前置核对（客服版 v1.1 下线状态）+ 触发真实链路复测；② golden mineru 样本回归（env 开关已在）；③ 备份调度实测（backup.sh 一次真实执行 + 新鲜度探针）；④ **arm64 推理冒烟**（起 `ragdemo-tei-cpu-arm64` 容器→等 health→/embed 真实一问断言向量维度→teardown；本机 Docker VM 预算 OOM 的约束在部署机不存在）。每步结果 JSON 归档（nfr-results 同目录口径），任何 FAIL 非零退出——窗口内"跑脚本+看报告"替代"按清单逐条手敲"。

**Blocked by:** None (can start immediately)（脚本本机可写+干跑校验；真机执行仍归工单 13 部署机窗口——不变更其验收路径，只压缩窗口时间）。

**Status:** ready-for-agent

- [ ] verify-deployment.sh：四子检查 + `--only <name>` / `--dry-run` / 结果 JSON（deploy/verify-results/<ts>.json）+ 每步 `[verify]` 时间戳日志（backup.sh 先例）；非 Linux/amd64 相关步骤按部署机架构自动启停
- [ ] arm64 冒烟子步骤：health 轮询超时/向量维度断言/teardown 全脚本化；镜像缺失给拉取/构建指引（tei-arm64.Dockerfile 指针）
- [ ] 本机干跑校验（--dry-run 全路径打印）+ runbook §11 检查单改指脚本入口（清单保留作判据说明）
- [ ] 真机执行结果回填仍走工单 13 验收路径（票内注明）
