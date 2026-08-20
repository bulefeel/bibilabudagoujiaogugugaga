@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "ROOT=D:\Vibe Seller2\ziniao-automation"
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONPATH=%ROOT%\src"
set "ZINIAO_PROJECT_ROOT=%ROOT%"
set "ZINIAO_DATA_DIR=%ROOT%\data"

if not exist "%PY%" (
  echo [错误] 尚未安装
  exit /b 1
)
"%PY%" -m ziniao_automation.runner --stop
if errorlevel 1 (
  echo [错误] PID 文件与本项目 Python 进程不匹配，未结束任何进程
  exit /b 1
)
echo [完成] 管理器已停止；紫鸟浏览器未被关闭
exit /b 0
