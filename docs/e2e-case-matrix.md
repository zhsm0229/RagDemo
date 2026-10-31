# P0 用例 → 自动化层映射矩阵（测试 B 交付物，W6 前滚动维护）

> 三层口径：**API**（pytest tests/api，后端契约）/ **E2E-L1**（Playwright，TEST_MODE stub，
> 每 MR）/ **E2E-L2**（Playwright @real，TEI+LLM 真链路，部署机每日/里程碑）/ **手工**。
> 本矩阵只管 P0（50 条）；P1 里程碑全量回归、P2 手工（test-cases.md §自动化归属）。

## 已落地的 E2E 用例（frontend/e2e/specs/，2026-10-31 对账：15 spec / 51 用例）

| spec | 用例数 | 覆盖（test-cases 编号或场景） | 层 |
|------|-------|------------------------|----|
| auth.spec | 3 | AUTH-01 登录/拒错/首登改密 | L1 |
| auth-lock.spec | 2 | AUTH-02/03 锁定与解锁 | L1 |
| anonymous.spec | 2 | 匿名守卫（未登录重定向） | L1 |
| chat.spec | 1 | CHAT-01/02 新会话+qa 直答+引用 | L1 |
| chat-input.spec | 3 | CHAT-03/04 输入边界/停止按钮 | L1 |
| chat-cache-manage.spec | 2 | 缓存命中与管理员清缓存 | L1 |
| chat-feedback.spec | 3 | 点踩反馈与 badcase 联动 | L1 |
| documents.spec | 2 | DOC-01/02 上传就绪 + 空态引导 | L1 |
| documents-gov.spec | 14 | 文档治理全矩阵（分型/待确认/到期/软删/版本/草稿审核） | L1 |
| qa-pairs.spec | 3 | FAQ 导入/启停/编辑 | L1 |
| users-admin.spec | 4 | 发放/禁用/重置/守卫 | L1 |
| admin-pages.spec | 6 | 管理页可达性与角色守卫矩阵 | L1 |
| agent-run.spec | 1 | 模板生成 AG-UI 事件流冒烟 | L1 |
| chat-real.spec | 2 | CHAT-09 逐字拒答 + 真实检索命中 | **L2 @real** |
| security.spec | 3 | SEC-01 管理面守卫 + SEC-03 串库拒答 | L1+**L2** |

**真实语义缺口（L2 @real 仅 3 条）**：CHAT-04 停止按钮真流中断、文档到期自动下线 UI、
记忆页、多轮历史浏览——归属工单 13 部署机复测窗口与后续 L2 扩容（交付体检命令绑定）。

## L1 层的确定性边界（为什么部分断言只在上层做）

- TEST_MODE 下 LLM 输出固定文案（StubLLMProvider.DEFAULT_CHAT_CHUNKS）——**答案语义
  正确性不可断言**，断言"通道命中 + 引用结构"（qa 直答相似度恒 1、cite-card 程序编号）。
- Stub rerank 分恒高于拒答阈值——**拒答语义在 L1 不可测**，红线由 API 测试
  （test_chat_stream 拒答用例）与评测集把守，UI 层归 L2。
- 默认选中最近会话（deviations #18）会让历史触发 stub 改写污染问题向量——qa 直答用例
  必须先开新会话（ChatPage.newConversation）。

## P0 其余用例的归属现状（W6 前补齐 UI 层缺口）

| 域 | P0 条目 | 现状 | 计划 |
|----|---------|------|------|
| AUTH | 03/05/06 | API 层已覆盖 | 锁定提示 UI 断言 → L1（W6 前） |
| CHAT | 04/10/13 | API 层已覆盖 | 停止按钮/反馈弹窗 → L1（W6 前） |
| DOC | 03/05/06/08/10-13/15/17/20 | API 层已覆盖；L1 有 01/02 | 版本上传/zip/到期 → L1 批次（W6 前） |
| QA | 01/02/04/05 | API 层已覆盖 | 导入/启停 UI 流 → L1 批次 |
| RET/EVAL | RET-01/02/06、EVAL-02/04/07/08 | API 层已覆盖 | 检索测试页/评测跑分 UI → L1 批次 |
| ADM/NFR/SEC | ADM-03/04/08、NFR-01/02/03/05、SEC-02/05 | API/部署档 | SEC 串库全量 → 评测集；NFR → 部署机档（§15.9） |

## 运行方式

```bash
# L1 栈（TEST_MODE 由 deploy/docker-compose.e2e.yml 硬编码，杜绝环境传递丢失；
# 第三层 e2e-isolated 把 app 指向独立隔离库 pg-e2e/rag_e2e——E2E_WIPE 硬复位只打隔离库，
# 试点/业务库零接触，2026-10-05 共库清空事故根治）：
docker compose -f docker-compose.yml -f deploy/docker-compose.e2e.yml \
  -f deploy/docker-compose.e2e-isolated.yml up -d --build pg-e2e app
docker compose exec -T app python -m app.db.seed            # 隔离库为空库：先部门后账号
docker compose exec -T -e E2E_PASSWORD=e2e-ci-password app python -m app.db.e2e_seed
cd frontend && E2E_CHANNEL=msedge npm run e2e     # 本机无 Playwright 浏览器下载通道时用系统 Edge
npm run e2e:down                                  # 收尾：恢复试点栈 app + 删除隔离库容器
# L2（部署机）：E2E_REAL=1 E2E_CHANNEL=chrome npm run e2e
# CI：e2e job 自动（TEST_MODE，官方 CDN 装 chromium）
```

- 防呆兜底：`app/db/e2e_seed.py` 在 E2E_WIPE=1 时校验库名必须为 `rag_e2e`（或 `*_e2e` 后缀），
  指向其余库直接非零退出；auth.setup.ts 自举段在种子前同样断言，指错库绝不照跑。
