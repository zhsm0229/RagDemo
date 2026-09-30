# 运维手册（Runbook）

> 状态：W3 Task 13 产出骨架，W5 Task 6 定稿备份/恢复章节（第 3 节，§18）；演练实测数据待 T9 恢复演练后回填 3.4。其余章节随里程碑增量补全（§18.4：启动停止 / 备份恢复 / 常见故障 / 合规轨道切换 / 端口核查）。
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
- 凭据：compose 内置默认值仅供首次拉起；部署机建议经 `deploy/.env`（git 已忽略）覆盖 `RAGFLOW_MYSQL_PASSWORD` / `RAGFLOW_MINIO_USER` / `RAGFLOW_MINIO_PASSWORD` / `RAGFLOW_REDIS_PASSWORD`。

### 2.2 启动（错峰顺序）

基线资源上限约 11.75G / 4.5C，主栈满配上限 10C17G（§12.5 预算值，主栈 compose limits 待后续任务落地），主机仅 4 核 16G——**两栈禁止同时满载（§12.5）**，顺序如下：

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
| 系统 OOM | `sudo dmesg -T \| grep -i "out of memory"` | 出现 kill 记录即资源超卖，回去查错峰纪律 |
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
| valkey/valkey | 8.1.3 | 待部署机登记 | - | - | - |

注：① `infiniflow/ragflow:v0.19.1` 为 full 变体，内置 embedding 模型工件同受 §12.8 约束（模型 hash 复核在镜像内进行，登记表可加行）；② tag 全部钉死、禁 `latest`（§12.1）。

登记后闭环（§12.8「含模型的镜像以 digest 引用」）：在部署机 `deploy/.env`（git 已忽略）把 ragflow 运行引用切到 digest，并重建容器生效——

```bash
# deploy/.env
RAGFLOW_IMAGE=infiniflow/ragflow@sha256:<登记值>

# 重建生效（数据卷不受影响）
docker compose -f deploy/docker-compose.ragflow.yml up -d
```

compose 中 `${RAGFLOW_IMAGE:-infiniflow/ragflow:v0.19.1}` 的 tag 默认值保留作可读回退，部署机以 `.env` 的 digest 引用为准。

## 3. 备份 / 恢复（§18，W5 T6 定稿）

- 规范指针：standards §18——PostgreSQL 每日 `pg_dump` 全量保留 7 天（备份目录权限 0700）、上传目录每日备份、恢复目标 **RPO ≤ 24h / RTO ≤ 2h**（§18.3）、`.env` 凭据副本托管密码管理器。
- **安全须知（§18.1）：备份含全部问答与文档内容，访问权限同生产库**——备份只落独立目录（本方案 `./backups`，脚本已 `chmod 700`），禁止放入 Web 可访问路径或全员可见共享位置；`.env`（含密钥）不在备份内，副本托管密码管理器。
- 备份范围 = 主库 `rag`（容器 `ragdemo-pg`）+ 上传目录 `./data/uploads`（`STORAGE_DIR` 默认值）。`ragdemo-pg-test`（`rag_test`，5434 端口）是独立测试实例，**不在备份范围**；RAGFlow 基线环境不做备份（一次性对照数据，销毁重建，见 2.5）。
- 备份统一走 `deploy/backup.sh`（顶部参数化区可按环境覆盖容器名/库名/目录/保留期），每步带 `[backup]` 时间戳日志，任一步失败非零退出。

### 3.1 每日自动备份（调度示例）

Windows（装有 Git Bash 的开发/演示机；在 cmd 执行，Git Bash 与仓库路径按实际调整）：

```bat
schtasks /Create /TN "ragdemo-daily-backup" /SC DAILY /ST 02:30 /TR "\"C:\Program Files\Git\bin\bash.exe\" -c 'cd /d/Project/ZCodeProject/RagDemo && mkdir -p backups && ./deploy/backup.sh >> backups/backup-cron.log 2>&1'"
```

Linux（部署机；`crontab -e` 追加一行，仓库路径按实际调整）：

```bash
30 2 * * * cd /opt/ragdemo && mkdir -p backups && ./deploy/backup.sh >> backups/backup-cron.log 2>&1
```

- 两版均为每日 02:30 执行，日志追加到 `backups/backup-cron.log`（backup.sh 的保留期清理只删 `*.gz`，该日志需手工定期清理）。
- 前置：`ragdemo-pg` 在运行且 healthy（`docker inspect -f '{{.State.Health.Status}}' ragdemo-pg`）；失败先查上述日志，再手工按 3.2 复现。
- 登记核查：Windows `schtasks /Query /TN "ragdemo-daily-backup"`；Linux `crontab -l`。

### 3.2 手动备份

```bash
bash deploy/backup.sh
```

- 产物：`backups/rag-YYYYMMDD-HHMMSS.sql.gz`（主库全量 dump，gzip 在宿主侧管道完成）+ `backups/uploads-YYYYMMDD-HHMMSS.tar.gz`（上传目录归档；目录不存在时警告跳过）；结束自动清理超过 7 天的 `backups/*.gz`。
- 备份目录 `backups/` 已加入 `.gitignore`（备份含全部问答与文档内容，禁止入库，§18.1），勿移出该忽略项。
- 备份后速检：`ls -lh backups/`——当日 `.sql.gz` 必须非 0 字节；**没做过恢复读回验证的备份不算可用备份**——至少在每次演练时按 3.3(a) 验证一轮。

### 3.3 恢复步骤（两种口径，动手前先确认口径）

两种口径共用前置：`ragdemo-pg` 在运行；先定位目标备份文件（默认取最新一份，也可手工指定）：

```bash
BK="$(ls -t backups/rag-*.sql.gz | head -n1)"     # 主库备份，如 backups/rag-20260930-023000.sql.gz
BU="$(ls -t backups/uploads-*.tar.gz | head -n1)" # 上传目录备份
echo "BK=$BK BU=$BU"
```

#### (a) 恢复到新建验证库（演练 / 校验备份可用性，不影响生产数据）

```bash
# 1. 建验证库（幂等：先删同名残留）
docker exec ragdemo-pg psql -U rag -c "DROP DATABASE IF EXISTS rag_restore"
docker exec ragdemo-pg psql -U rag -c "CREATE DATABASE rag_restore"

# 2. 恢复（ON_ERROR_STOP=1：任一 SQL 失败即非零退出，不得静默半成功）
gunzip -c "$BK" | docker exec -i ragdemo-pg psql -v ON_ERROR_STOP=1 -U rag -d rag_restore

# 3. 读回核对：表清单应与生产库一致，核心表行数应与备份时点一致
docker exec ragdemo-pg psql -U rag -d rag_restore -c "\dt"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM qa_pairs"
docker exec ragdemo-pg psql -U rag -d rag_restore -c "SELECT count(*) FROM messages"

# 4. 与备份时点记录的行数比对（三表同第 3 步口径；演练当天如有新增写入，恢复库行数 ≤ 生产库属正常，差值 = 备份后增量）
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM qa_pairs"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM messages"

# 5. 演练结束清理验证库
docker exec ragdemo-pg psql -U rag -c "DROP DATABASE rag_restore"
```

uploads 侧核对：`tar -tzf "$BU" | head` 能列出 `uploads/...` 文件即归档完好；正式演练时抽一个文件解出比对内容。

#### (b) 覆盖生产库（灾难恢复；先停写、后覆盖、再冒烟）

> **【危险】覆盖生产库前必须确认已有一次成功备份——本节会先 DROP 生产库 `rag`。没有非 0 字节的 `rag-*.sql.gz` 时禁止执行本节，先走 3.3(a) 验证库口径排查备份可用性。**

```bash
# 0. 前置检查：目标备份存在且非 0 字节；停机窗口与相关方已确认
ls -lh backups/rag-*.sql.gz

# 1. 停写入方（app 容器并入主栈后生效；并入前确认无业务写入在跑）
docker compose stop app

# 2. 重建生产库（连 postgres 维护库执行；WITH (FORCE) 断开残留连接，PG15 支持）
docker exec ragdemo-pg psql -U rag -d postgres -c "DROP DATABASE IF EXISTS rag WITH (FORCE)"
docker exec ragdemo-pg psql -U rag -d postgres -c "CREATE DATABASE rag"

# 3. 恢复主库（ON_ERROR_STOP=1：任一 SQL 失败即非零退出——灾难恢复的"成功"信号必须可靠，不得静默半成功）
gunzip -c "$BK" | docker exec -i ragdemo-pg psql -v ON_ERROR_STOP=1 -U rag -d rag

# 4. 恢复上传目录（归档内含 uploads/ 一级目录，解到其父目录 ./data）
mkdir -p ./data
tar -xzf "$BU" -C ./data

# 5. 起服务 + 冒烟
docker compose up -d
docker compose ps   # 应全部 healthy
# 冒烟：登录工作台发起一次问答，确认回答带可核对引用；app 并入后增加 curl -f http://127.0.0.1/healthz（§12.2）

# 6. 读回核对（对 -d rag 执行，行数与备份时点一致）
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM documents"
docker exec ragdemo-pg psql -U rag -d rag -c "SELECT count(*) FROM qa_pairs"
```

- 红线（与第 1 节一致）：本节 `DROP`/`CREATE` 只针对库 `rag`，禁止波及 `rag_test`；全程禁止 `docker compose down -v`（会删 `pgdata` 业务数据卷）。

### 3.4 演练记录表（§18.3，实测回填）

| 日期 | 类型 | 备份大小 | 恢复耗时 | RTO | RPO | 执行人 | 备注 |
|---|---|---|---|---|---|---|---|
| （示例）2026-09-30 | 验证库恢复 | 12 MB | 8 分钟 | 12 分钟 | 20 小时 | 张三 | 示例行：RTO=恢复+冒烟端到端时长，RPO=距上次成功备份的数据窗口；正式记录自 W5 T9 演练起填 |

- 类型口径：`验证库恢复` = 3.3(a)，常规演练；`生产覆盖演练` = 3.3(b)，需审批与停机窗口。

### 3.5 RPO / RTO 目标声明（§18.3）

- **RPO ≤ 24 小时**：由每日 02:30 全量备份决定，最大数据丢失窗口 ≈ 24 小时（上次成功备份到故障点之间新增的数据）。
- **RTO ≤ 2 小时**：定位备份 → 3.3 恢复 → 起服务 → 冒烟核对的端到端时长上限。
- 实测值：**待 W5 T9 恢复演练回填**（实测写入 3.4 记录表；超出目标须先整改备份/恢复方案再定稿）。

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
