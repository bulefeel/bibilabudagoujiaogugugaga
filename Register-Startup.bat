@echo off
setlocal EnableExtensions
chcp 65001 >nul
rem Derive the install directory from this script, so the folder can be
rem moved or installed anywhere without editing any file.
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PYW=%ROOT%\.venv\Scripts\pythonw.exe"
set "TASK=Ziniao Automation V1"

if not exist "%PYW%" (
  echo [错误] 尚未安装，请先运行 Install.bat
  exit /b 1
)

rem 登录任务直接执行 pythonw.exe，不经过 cmd /c、隐藏窗口或编码命令。
schtasks.exe /Create /F /SC ONLOGON /TN "%TASK%" /TR "\"%PYW%\" -m ziniao_automation.runner"
if errorlevel 1 (
  echo [错误] 登录启动任务注册失败
  exit /b 1
)
echo [完成] 已注册当前 Windows 用户登录后启动任务：%TASK%
echo 可运行：schtasks /Delete /F /TN "%TASK%" 取消注册
exit /b 0
