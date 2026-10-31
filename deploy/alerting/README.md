# 最小告警链（工单 09，2026-10-31 评审 P1-4）

商用私有化交付的最小可运维告警闭环：不引 Prometheus/Grafana 全家桶（§25.4 触发线
约束），用"一条拨测 + 四个探针 + webhook"满足无人值守感知。部署机按本 README 配置
即进交付验收清单。

## 探针清单（全部已有输出源，无需改应用）

| # | 探针 | 数据源 | 判定口径 |
|---|---|---|---|
| 1 | 服务存活 | `GET /healthz`（每 1-5min） | 非 200 连续 2 次即告警 |
| 2 | 容器重启 | `docker inspect --format '{{.RestartCount}} {{.State.Health.Status}}'`（pg/tei-embed/tei-rerank/mineru/gotenberg/app） | RestartCount 增长或 health≠healthy 即告警 |
| 3 | 磁盘水位 | `df -P` 备份分区与 Docker 数据分区 | 使用率 >85% WARN / >92% 告警 |
| 4 | 备份新鲜度 | `backups/last-backup.json`（backup.sh 步骤 4/3 维护，bootstrap 体检项 #10 同口径） | lastSuccessAt 距今 >25h 告警 |

## 配置样例（crontab + webhook）

```bash
# crontab：每 5 分钟拨测 + 四探针汇总，异常即 POST webhook（企业微信/钉钉/飞书均适用）
*/5 * * * * /opt/ragdemo/deploy/alerting/check-and-alert.sh >> /var/log/ragdemo-alert.log 2>&1
```

`check-and-alert.sh`（随部署包交付，参考实现见本目录同文件）输出单行 JSON：
`{"ts":"...","fails":[...],"warns":[...]}`——fails 非空时 POST 到 `$ALERT_WEBHOOK_URL`。

## 升级路径（触发线后）

探针数量增长或需要历史曲线时，再评估 Prometheus node_exporter + Alertmanager
（§25.4 未到不动）；当前形态的目标是"夜间 pg OOM / 备份漏跑 / 磁盘涨满"三类
高频事故能在 5 分钟内被感知。
