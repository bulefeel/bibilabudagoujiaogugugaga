@echo off
setlocal EnableExtensions
chcp 65001 >nul
title 修复飞书通知凭据

set "ROOT=D:\Vibe Seller2\ziniao-automation"
set "CLI=%ROOT%\.venv\Scripts\ziniao-automation.exe"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\src"
set "ZINIAO_PROJECT_ROOT=%ROOT%"
set "ZINIAO_DATA_DIR=%ROOT%\data"
cd /d "%ROOT%"

if not exist "%CLI%" (
  echo [错误] 项目尚未安装完整，请先运行 Install.bat
  pause
  exit /b 1
)

echo [1/2] 请重新输入飞书 App Secret（App ID/Chat ID 沿用已有设置）。
echo App Secret 会隐藏输入，只保存到 Windows 凭据管理器。
"%CLI%" configure feishu --reuse-metadata
if errorlevel 1 (
  echo [错误] 飞书凭据保存失败。
  pause
  exit /b 1
)

echo [2/2] 正在只重启本项目后台，紫鸟不会被关闭...
"%PY%" -m ziniao_automation.runner --stop >nul 2>&1
call "%ROOT%\Start.bat"
if errorlevel 1 (
  echo [错误] 凭据已保存，但后台重启失败，请手动运行 Start.bat。
  pause
  exit /b 1
)

echo [完成] 飞书凭据已恢复，下一次任务完成后会发送通知。
pause
exit /b 0
