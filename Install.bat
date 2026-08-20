@echo off
setlocal EnableExtensions
chcp 65001 >nul
title 紫鸟自动化 - 安装

rem Derive the install directory from this script, so the folder can be
rem moved or installed anywhere without editing any file.
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\.venv"
set "UV=%ROOT%\uv.exe"
set "CACHE=%ROOT%\.uv-cache"
set "TMPROOT=%ROOT%\data\tmp"
set "UV_CACHE_DIR=%CACHE%"
set "UV_PYTHON_INSTALL_DIR=%ROOT%\.uv-python"
set "UV_TOOL_DIR=%ROOT%\.uv-tools"
set "UV_PYTHON_PREFERENCE=only-managed"
set "PIP_CACHE_DIR=%CACHE%\pip"
set "PIP_BUILD_TRACKER=%CACHE%\build-tracker"
set "TMP=%TMPROOT%"
set "TEMP=%TMPROOT%"
set "PYTHONPYCACHEPREFIX=%ROOT%\data\pycache"
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\data\playwright-unused"
set "PYTHONUSERBASE=%ROOT%\data\python-userbase"

cd /d "%ROOT%"
if not exist "%TMPROOT%" mkdir "%TMPROOT%"
if not exist "%CACHE%" mkdir "%CACHE%"

rem uv.exe ships next to this script.  Only fall back to a PATH copy so a
rem developer checkout without the binary can still install.
if exist "%UV%" goto :uv_ready
where uv >nul 2>&1
if errorlevel 1 goto :no_uv
for /f "delims=" %%U in ('where uv') do (
  copy /Y "%%U" "%UV%" >nul
  goto :uv_ready
)

:uv_ready
if not exist "%UV%" goto :no_uv

echo [1/4] 正在 D 盘准备 Python 3.12...
"%UV%" python install 3.12
if errorlevel 1 goto :failed

if not exist "%VENV%\Scripts\python.exe" (
  echo [2/4] 正在 D 盘创建虚拟环境...
  "%UV%" venv --python 3.12 "%VENV%"
  if errorlevel 1 goto :failed
) else (
  "%VENV%\Scripts\python.exe" -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>&1
  if errorlevel 1 (
    echo [错误] 现有 .venv 不是 Python 3.12，请先重命名该目录再运行安装。
    exit /b 1
  )
)

echo [3/4] 正在安装项目依赖（不会下载 Playwright 浏览器）...
"%UV%" pip install --python "%VENV%\Scripts\python.exe" -e "%ROOT%"
if errorlevel 1 goto :failed

echo [4/4] 正在初始化 D 盘目录和数据库...
set "PYTHONPATH=%ROOT%\src"
"%VENV%\Scripts\python.exe" -c "from ziniao_automation.config import Settings; from ziniao_automation.db import create_sqlite_engine,init_database; s=Settings.from_env(); s.ensure_directories(); e=create_sqlite_engine(s); init_database(e); e.dispose()"
if errorlevel 1 goto :failed

echo.
echo [完成] 下一步依次配置：admin、ziniao、feishu
echo   "%VENV%\Scripts\ziniao-automation.exe" configure admin
echo   "%VENV%\Scripts\ziniao-automation.exe" configure ziniao
echo   "%VENV%\Scripts\ziniao-automation.exe" configure feishu
exit /b 0

:no_uv
echo [错误] 安装目录里缺少 uv.exe。
echo         它随安装包一起提供，请重新解压/重装，不要单独挪走这个文件。
exit /b 1

:failed
echo [错误] 安装未完成，请查看上方第一条错误。
exit /b 1
