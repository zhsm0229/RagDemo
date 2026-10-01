# 已知问题与测试发现汇总（known issues）

> 更新：2026-10-01。来源：W1-W6 各冲刺账本（`.superpowers/sdd/`）、部署验收报告（`deploy-acceptance-report.md`）、验收状态对照（`acceptance-status.md`）。
> 状态：**开放**（未解决）/ **已修复**（含修复指针）/ **登记裁决**（待决策）。

## 一、开放问题

### 开放-1：TEI 算力不足（内测放行最大阻断）
CPU TEI + 本机高负载下：首 token 57.4s（TEI 段 55.3s）、检索 38.2s、rerank 容器两次 OOM（Exited 137）、22/24 评测请求 infra 超时。远超非功能基线 #7（首 token <3s / 检索 <1.5s）。
**处置**：TEI 需 GPU 或独占 CPU 资源（资源需求已升级登记）；本机复测需在低负载窗口。

### 开放-2：串库行为面复测挂起（语料治理已落地，复测被开放-1 阻断）
真实 embedding 首轮复测：结构越界 **0/19**（部门隔离机制完好）；越视角拒答 7/9——3 例漏拒（rerank 0.6509-0.6953）全部撞在全员手册的「考勤与休假」HR 域段落上，漏拒带与对照命中带（0.63-0.71）重叠 → **0.55 阈值调不动，根因是语料治理**。
**已处置（W6 T2）**：HR 域内容撤出全员手册（v1.2 全员版 + HR 版分册，旧版已软删，重复语料已清）。
**待办**：TEI 恢复后重跑治理后复测（客服视角 hr 题 10/10 拒答为通过判据）+ hr 视角对称矩阵（W5 首轮被负载摧毁）。
**附带实证**：漏拒题回答含所引块外政策细节 1 例（弱上下文编造）——语料治理后复评归零（硬指标 #4）。

### 开放-3：eval 跑分逐题 detail 提交失效
**已修复（W6 T1，commit 12721f9）**：根因=共享 items list + `expire_on_commit=False` 下 flush 相等比较抑制 UPDATE；`flag_modified` 修复 + 逐题进度回归测试。**待部署复验**。复现脚本：`.zcode/accept/repro_detail_stall.py`。

### 开放-4：ingest 侧 StubEmbedder 泄漏（部署级缺陷）
**已修复（W6 T1，commit a3dd871）**：`documents.py:_build_deps` 走装配工厂（真实 TEI；test_mode 回 Stub）。W2 只接通问答侧、入库侧漏接，测试全在 TEST_MODE 故三个冲刺未暴露——**真实模式部署才发现的典型案例**。

### 开放-5：试点库文档标题 GBK 乱码
curl 在 Windows Git Bash 直传中文文件名导致 id=9/10 等 title/original_filename 乱码（检索不受影响，展示面缺陷）。
**处置**：运维口径——中文标题上传走 API 客户端脚本或界面；存量乱码文档重传规范化（复测窗口前顺手做）。

### 开放-6：试点库 E2E 残留
用户并行会话的 E2E 套件直接对试点库跑（users 表 44 行含 e2e-*/小张/停用员/重置员残留 40+；50 份文档含 e2e_* 前缀），且曾改掉 admin 密码。
**处置**：E2E 套件应使用独立库（隔离方案待你会话裁决）；残留清理需与你会话协调时机；本机已重置 admin 为 documented 密码（见下文测试账号）。

### 开放-7：documents 删除端点缺失（F-305 未实现）
语料下线目前只能 DBA 软删（W6 语料治理即用 DBA 步骤，已登记 runbook）。
**处置**：F-305 端点实现后收口。

### 开放-8：前端零散项
①KnowledgeGapCard 周切换失败时旧周数据残留（错误可见可重试，cosmetic）；②fmtTime 尚有 6 处内联未收敛（工具已备）；③前端 load 无 catch 的 unhandled rejection 统一收口；④全仓 lint 红来自并行会话未跟踪的 `frontend/e2e/`（trace 产物），归你会话收口（eslint ignore 或清理）。

### 开放-9：50MB 解析性能与 20 并发压测
待真实语料与部署机（本机跳过）。

## 二、登记裁决（待决策）

| # | 事项 | 选项 |
|---|------|------|
| 裁决-1 | standards §12.2"tei 禁止端口发布" vs compose 生产映射 8081 注记 | 生产收回端口 or 修订标准（runbook 已标"先评审不静默择一"） |
| 裁决-2 | degraded 字段：列表 DTO 未下发 vs 用户手册描述"降级解析标注" | 后端补字段（契约变更+deviations #24 翻转）或修订手册 |
| 裁决-3 | mineru compose 构建为 pipeline 后端 vs §12.5"必须 ONNX 后端" | 换 ONNX 镜像 or 修订标准（随 MinerU 实际部署一并裁决） |
| 裁决-4 | compose 注释引用不存在的 make-dev-models 目标 | 补 Makefile 目标 or 改注释（按 download_models.py 实际入口已登记 runbook） |
| 裁决-5 | frontend/e2e lint 归口（见开放-8④） | e2e 车道自清 or eslint 例外 |

## 三、已修复关闭（本机部署验收后）

- ~~eval detail-stall~~（开放-3，已修）
- ~~ingest StubEmbedder 泄漏~~（开放-4，已修）
- ~~login 不校验 is_active~~（W6 修复波 46e8472，禁用用户登录即 401）
- ~~并发重名 500~~（W6 修复波 52e9354，IntegrityError→409）
- ~~documents 列表 ILIKE 通配符未转义~~（W4 T1）
- ~~lockedUntil 不在锁定错误响应~~（W4 T3 rider）
- ~~重排响应形状（真实 TEI 裸数组）~~（用户并行会话适配，部署验收确认）
- ~~backup.sh 首跑与恢复流程~~（W5/T9+部署验收实测：RTO≈4min）
- ~~知识缺口卡错误态 / fmtTime 收敛 / ZRC 重试~~（W6 T3）

## 四、待用户/待资源清单

| 项 | 责任 | 阻断 |
|----|------|------|
| TEI GPU/独占资源 | 用户+IT | 阻断 #7、串库行为面复测、编造复评 |
| 评测题库 Top30-50 | 用户+对接人 | 阻断 #1 分部门准确率 |
| 合规清单签批（compliance-checklist 草案已由用户产出） | 用户 | 合规轨道正式确认 |
| 部署机（VM）落实 | 用户+IT | 正式内测载体 |
| RAGFlow 基线报告 | 部署后 | 硬指标 #5 对照半边 |
