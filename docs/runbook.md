# 运维手册（Runbook）

> 状态：骨架版（W3 Task 13 产出）。备份/恢复流程按 standards §18 于 **W5 演练定稿**，其余章节随里程碑增量补全（§18.4：启动停止 / 备份恢复 / 常见故障 / 合规轨道切换 / 端口核查）。
> 部署形态：单机 Docker Compose，4 核 16G VM（§12.5）。所有命令在仓库根目录执行（Windows Git Bash 或部署机 bash）。

## 1. 主栈启停

```bash
# 启动
docker compose up -d

# 状态与健康检查（healthcheck 就绪应为 healthy）
docker compose ps
docker inspect -f '{{.Name}} {{.State.Health.Status}}' ragdemo-pg

# 停止（保留数据卷）
docker compose down
```

- 主栈容器清单随里程碑并入：当前为 `pg` / `pg-test`（W3 阶段）；`app` / `mineru` / `tei-embed` / `tei-rerank` 后续并入，并入后 app 健康检查增加 `curl -f http://127.0.0.1/healthz`（发布端口仅 80，§12.2）。
- **禁止**对主栈使用 `down -v`（会删除 `pgdata` 业务数据）；数据清理只允许在明确授权的重建流程中执行。

## 2. RAGFlow 基线环境

### 2.1 定位与合规红线

- 用途：检索准确率评测的 RAGFlow 对照基线（project-brief 决策 11/15），**仅内测期存在，不进生产栈**（§12.2）。配置文件：`deploy/docker-compose.ragflow.yml`（独立 project `ragflow-baseline`，网络/卷与主栈完全隔离）。
- **§7.6 红线：禁止为 RAGFlow 配置任何云 API key 灌真实语料**（否则构成第二条内容出网通道）。只允许 full 镜像内置本地 embedding，或在基线界面配置内网模型端点；语料只用脱敏/合成数据。发现配置了云 key 立即下线整改。

### 2.2 启动（错峰顺序）

基线资源上限约 11.75G / 4.5C，主栈满配上限 10C17G，主机仅 4 核 16G——**两栈禁止同时满载（§12.5）**，顺序如下：

```bash
# 1. 先确认主栈无重负载：无解析任务（mineru 空闲）、无批量上传/评测在跑；
#    必要时先停主栈（至少停 mineru/tei 等重负载服务）
docker compose down

# 2. 起基线（首次启动拉镜像 + 初始化较慢，属预期）
docker compose -f deploy/docker-compose.ragflow.yml up -d

# 3. 等待五服务全部 healthy（es01 首次初始化最慢）
docker compose -f deploy/docker-compose.ragflow.yml ps

# 4. 浏览器访问 Web UI：http://<vm-ip>:9380
```

### 2.3 停止（错峰恢复主栈）

```bash
# 停基线（保留 baseline-* 卷，下次启动语料仍在）
docker compose -f deploy/docker-compose.ragflow.yml down

# 确认基线全部退出后再恢复主栈
docker compose ps   # 应为空
docker compose up -d
```

### 2.4 资源冲突观察点（合计上限 vs 4 核 16G）

| 观察点 | 命令 | 预警线 |
|---|---|---|
| 内存 | `free -h` | available < 2G 预警，< 1G 立即停一栈 |
| 容器资源 | `docker stats --no-stream` | 任一容器贴 memory limit（被 limit OOM 会频繁重启） |
| 系统 OOM | `dmesg -T \| grep -i "out of memory"` | 出现 kill 记录即资源超卖，回去查错峰纪律 |
| ES 磁盘水位 | 基线界面 / `docker logs baseline-es01` | 5gb/3gb/2gb 水位告警（已内置） |
| 宿主磁盘 | `df -h` | es/mysql/minio 卷增长，剩余 < 20% 预警 |

基线重负载 = 批量导入解析 + embedding 生成，为最坏负载；评测取数期间保持主栈停止，跑完按 2.3 交还资源。

### 2.5 数据清理

```bash
# 基线语料整体作废：删除全部 baseline-* 卷（不影响主栈）
docker compose -f deploy/docker-compose.ragflow.yml down -v

# 只重置单个数据域（先 down 再删对应卷）
docker volume rm baseline-es-data
```

- 卷以 `baseline-` 前缀与主栈（`ragdemo_pgdata` 等）隔离；**清理命令只允许作用于 baseline 前缀卷**。
- 基线是一次性对照数据，不做备份（见第 3 节），销毁后重新导入脱敏语料即可。

### 2.6 镜像 digest 核对登记表（§12.8 风格）

部署机首跑时逐条核对登记（命令见下），**双人复核**后本表方可视为有效；换 tag 属变更，须重新登记：

```bash
docker pull <镜像:tag>
docker inspect --format '{{index .RepoDigests 0}}' <镜像:tag>
```

| 镜像 | tag | digest (sha256) | 登记日期 | 复核人 A | 复核人 B |
|---|---|---|---|---|---|
| infiniflow/ragflow | v0.19.1 | 待部署机登记 | - | - | - |
| docker.elastic.co/elasticsearch/elasticsearch | 8.11.3 | 待部署机登记 | - | - | - |
| mysql | 8.0.39 | 待部署机登记 | - | - | - |
| quay.io/minio/minio | RELEASE.2023-12-20T01-00-02Z | 待部署机登记 | - | - | - |
| valkey/valkey | 8 | 待部署机登记 | - | - | - |

注：① `infiniflow/ragflow:v0.19.1` 为 full 变体，内置 embedding 模型工件同受 §12.8 约束（模型 hash 复核在镜像内进行，登记表可加行）；② tag 全部钉死、禁 `latest`（§12.1）。

## 3. 备份 / 恢复

- 规范指针：standards §18——PostgreSQL 每日 `pg_dump` 全量保留 7 天（备份目录权限 0700）、上传目录每日同步、恢复目标 **RPO ≤ 24h / RTO ≤ 2h**、`.env` 凭据副本托管密码管理器。
- **状态：W5 演练定稿**——具体备份脚本、恢复步骤、演练实测数据（RPO/RTO 实际值）在 W5 恢复演练后回填本节（§18.3：演练步骤写入 runbook，runbook 先于演练产出，故先立本占位）。
- RAGFlow 基线环境不做备份（一次性对照数据，销毁重建，见 2.5）。

## 4. 端口暴露核查清单（§12.2，每次发布 / 基线启停后执行）

规则基线：**主栈仅 app 发布 80；RAGFlow 基线仅发布 9380；其余一切端口仅 compose 内网互联，禁止 `ports` 发布。**

```bash
# 1. 容器发布端口核查：输出中只允许出现 0.0.0.0:80(app) 与 0.0.0.0:9380(baseline-ragflow)
docker ps --format '{{.Names}}\t{{.Ports}}'

# 2. 宿主监听核查：比对无未登记监听
ss -tlnp   # 或 netstat -tlnp
```

- [ ] `docker ps` 发布端口仅为 80（主栈 app）与 9380（基线），无其他 `0.0.0.0:` 映射
- [ ] 主栈 pg 的 5433/5434 属**开发期临时发布**（本机开发用），生产收口前必须收回为 compose 内网（§12.2）
- [ ] es01/mysql/minio/valkey（9200/3306/9000/9001/6379）无任何宿主发布
- [ ] 防火墙/安全组仅放行 80、9380（22 运维端口按 IT 策略另行管理）
- [ ] 核查结果异常时：先 `docker compose down` 对应栈收敛端口，再排查是谁改了 compose
