# 31: 许可清单守账测试（法律意见证据链自动化保鲜）

**What to build:** 逐组件许可清单（`design/10-license-compliance-tech.md`，工单 10 实证产物）目前是纯手工维护——依赖升降级后即漂移且无报警。新增 pytest 守账：读 pyproject 运行时依赖 → 每包查安装 metadata 的 License/分类器 → 断言与登记清单一致（漂移即红，法律顾问每次只审 diff 而非重审全量）；另加 CI 侧断言：nomineru 降级档 compose 配置不包含 mineru 服务。**法律意见本体不可自动化**（AGPL/LGPL 义务边界解释权在外部顾问），本票只把证据链自动化保鲜。

**Blocked by:** None (can start immediately)。

**Status:** ready-for-agent

- [ ] tests/unit/test_license_audit.py：运行时依赖组（`pip install .` 口径，排除 dev-only 如 fpdf2——勿将 LGPL dev 依赖误报为分发义务）逐包 metadata license 断言 == 登记清单；psycopg=LGPL-3.0-only、mineru 容器侧 AGPL（清单行存在性）等关键行钉死；测试内注明"对账对象=design/10 清单，法律结论以顾问意见为准"
- [ ] nomineru 档断言：`docker compose -f prod -f nomineru config` 干跑（或等价静态断言）无 mineru 服务且 app MINERU_BASE_URL 为空——Docker 不可用环境 skip（CI/本机双形态）
- [ ] 全量回归绿；法律意见/签批工单 10/11 本身保持外部窗口（不变更其验收路径）
