@echo off
REM ============================================================================
REM 可视化界面自动化（headed 模式）：浏览器窗口可见，能全程看到自动登录与页面操作。
REM 流程：拉起隔离栈（独立 pg-e2e 库，不碰试点库）-> 种子 -> 有头浏览器跑全量用例
REM       -> 自动恢复试点栈。报告：frontend\e2e\.artifacts\report\index.html
REM 前置：Docker Desktop 在运行。可选参数透传给 playwright（如只跑登录：
REM       e2e-visible.cmd auth.spec.ts）
REM ============================================================================
setlocal
cd /d %~dp0..

echo [1/4] 拉起隔离测试栈（独立 pg-e2e + TEST_MODE 应用）...
docker compose -f docker-compose.yml -f deploy/docker-compose.e2e.yml -f deploy/docker-compose.e2e-isolated.yml up -d pg-e2e app || goto :err
echo     等待应用健康（约 30 秒）...
timeout /t 30 /nobreak >nul

echo [2/4] 初始化测试库（部门种子 + E2E 账号/夹具）...
docker compose exec -T app python -m app.db.seed || goto :err
REM 口令不落本脚本字面量：缺省值由工具链自持（e2e_seed.py 与 auth.setup.ts 同源缺省，
REM 仅作用于一次性隔离库）；自定义时先 set E2E_PASSWORD=xxx 再运行本脚本。
set SEEDEXTRA=-e E2E_WIPE=1
if defined E2E_PASSWORD set SEEDEXTRA=-e E2E_WIPE=1 -e "E2E_PASSWORD=%E2E_PASSWORD%"
docker compose exec -T %SEEDEXTRA% app python -m app.db.e2e_seed || goto :err

echo [3/4] 有头浏览器运行界面自动化（将弹出 Edge 窗口，请勿动鼠标键盘）...
cd frontend
set E2E_CHANNEL=msedge
set E2E_WIPE=1
npx playwright test --config=e2e/playwright.config.ts --headed %*
cd ..

echo [4/4] 恢复试点栈（真实模式应用）并停隔离库...
docker compose -f docker-compose.yml up -d app
docker stop ragdemo-pg-e2e >nul 2>&1

echo.
echo 完成。HTML 报告：frontend\e2e\.artifacts\report\index.html（直接双击打开）
echo 也可命令行打开：cd frontend ^&^& npx playwright show-report --config e2e/playwright.config.ts
pause
goto :eof

:err
echo.
echo [错误] 栈未就绪：请确认 Docker Desktop 正在运行后重试。
pause
