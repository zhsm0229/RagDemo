# 31: 许可清单守账测试（法律意见证据链自动化保鲜）

**What to build:** 逐组件许可清单（`design/10-license-compliance-tech.md`，工单 10 实证产物）目前是纯手工维护——依赖升降级后即漂移且无报警。新增 pytest 守账：读 pyproject 运行时依赖 → 每包查安装 metadata 的 License/分类器 → 断言与登记清单一致（漂移即红，法律顾问每次只审 diff 而非重审全量）；另加 CI 侧断言：nomineru 降级档 compose 配置不包含 mineru 服务。**法律意见本体不可自动化**（AGPL/LGPL 义务边界解释权在外部顾问），本票只把证据链自动化保鲜。

**Blocked by:** None (can start immediately)。

**Status:** done（2026-11-01 实施：3 用例全绿 + 漂移检测有效性验证）

- [x] tests/unit/test_license_audit.py：① 运行时依赖逐包 metadata 实测 == 登记清单（20 包双向对账——集合不一致也红；psycopg=LGPL-3.0-only 关键行钉死，⚠️ 注释随行）；② fpdf2（LGPL dev 依赖）不入运行时分发面钉死；③ nomineru 档 compose 静态 YAML 断言（mineru 挂 full profile 缺省不起 + app MINERU_BASE_URL 强制置空——免 Docker 依赖，CI/本机双形态稳定）
- [x] 漂移检测有效性验证：模拟 psycopg 期望漂移 → 精确检出 ("MIT","LGPL-3.0-only") 并指名更新动作
- [x] 边界声明入 docstring：只守技术事实清单不漂、不构成法律意见，工单 10/11 外部验收路径不变
- [x] 全量回归绿（守账 3 passed / 0.04s）；make lint 通过
