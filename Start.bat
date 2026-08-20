@echo off
setlocal EnableExtensions
chcp 65001 >nul
title 紫鸟自动化 - 启动

set "ROOT=D:\Vibe Seller2\ziniao-automation"
set "PYW=%ROOT%\.venv\Scripts\pythonw.exe"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "UV_CACHE_DIR=%ROOT%\.uv-cache"
set "UV_PYTHON_INSTALL_DIR=%ROOT%\.uv-python"
set "TMP=%ROOT%\data\tmp"
set "TEMP=%ROOT%\data\tmp"
set "PYTHONPYCACHEPREFIX=%ROOT%\data\pycache"
set "PYTHONPATH=%ROOT%\src"
set "ZINIAO_PROJECT_ROOT=%ROOT%"
set "ZINIAO_DATA_DIR=%ROOT%\data"

cd /d "%ROOT%"
if not exist "%PY%" (
  echo [错误] 尚未安装，请先运行 Install.bat
  exit /b 1
)
if not exist "%ROOT%\data\run" mkdir "%ROOT%\data\run"
if not exist "%ROOT%\data\logs" mkdir "%ROOT%\data\logs"
if not exist "%TMP%" mkdir "%TMP%"

"%PY%" -c "import socket,sys;s=socket.socket();s.settimeout(.5);r=s.connect_ex(('127.0.0.1',8765));s.close();sys.exit(0 if r==0 else 1)"
if not errorlevel 1 (
  echo [正常] 管理后台已在 http://127.0.0.1:8765 运行
  exit /b 0
)

rem Do not pass temporary Codex shell ownership markers to the service.
set "CODEX_SHELL="
set "CODEX_THREAD_ID="
set "CODEX_INTERNAL_ORIGINATOR_OVERRIDE="
set "CODEX_SANDBOX_NETWORK_DISABLED="

rem Launch a detached process that remains alive after this batch exits.
start "Ziniao Automation" /D "%ROOT%" "%PYW%" -m ziniao_automation.runner

for /L %%I in (1,1,30) do (
  "%PY%" -c "import socket,sys;s=socket.socket();s.settimeout(.5);r=s.connect_ex(('127.0.0.1',8765));s.close();sys.exit(0 if r==0 else 1)"
  if not errorlevel 1 goto :ready
  ping 127.0.0.1 -n 2 >nul
)
echo [错误] 后台 30 秒内未就绪，请查看 data\logs\ziniao-automation.jsonl
exit /b 1

:ready
echo [完成] 管理后台：http://127.0.0.1:8765
exit /b 0
