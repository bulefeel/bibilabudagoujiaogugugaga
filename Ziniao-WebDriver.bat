@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Ziniao Automation - WebDriver Launcher

set "PORT=16851"
set "ZINIAO_EXE="

echo ==================================================
echo   Ziniao WebDriver Temporary Launcher
echo ==================================================
echo.

rem Keep this file ASCII-only. Build the known Chinese path in PowerShell.
for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -Command "$name=[string][char]0x7D2B+[char]0x9E1F+[char]0x6D4F+[char]0x89C8+[char]0x5668; $p=Join-Path (Join-Path 'D:\' $name) 'ziniao\ziniao.exe'; if(Test-Path -LiteralPath $p){[Console]::Out.Write($p)}"`) do set "ZINIAO_EXE=%%I"

rem Try common installation paths.
if not defined ZINIAO_EXE if exist "C:\Program Files\ziniao\ziniao.exe" set "ZINIAO_EXE=C:\Program Files\ziniao\ziniao.exe"
if not defined ZINIAO_EXE if exist "C:\Program Files (x86)\ziniao\ziniao.exe" set "ZINIAO_EXE=C:\Program Files (x86)\ziniao\ziniao.exe"
if not defined ZINIAO_EXE if exist "D:\Program Files\ziniao\ziniao.exe" set "ZINIAO_EXE=D:\Program Files\ziniao\ziniao.exe"
if not defined ZINIAO_EXE if exist "D:\ziniao\ziniao.exe" set "ZINIAO_EXE=D:\ziniao\ziniao.exe"
if not defined ZINIAO_EXE if exist "%LOCALAPPDATA%\ziniao\ziniao.exe" set "ZINIAO_EXE=%LOCALAPPDATA%\ziniao\ziniao.exe"

rem Fall back to the uninstall registry.
if not defined ZINIAO_EXE for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -Command "$roots='HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'; $x=Get-ItemProperty $roots -ErrorAction SilentlyContinue ^| Where-Object { $_.DisplayIcon -match 'ziniao\.exe' } ^| Select-Object -First 1 -ExpandProperty DisplayIcon; if($x){[Console]::Out.Write(($x -replace ',\d+$',''))}"`) do set "ZINIAO_EXE=%%I"

if not defined ZINIAO_EXE goto :not_found
if not exist "%ZINIAO_EXE%" goto :not_found

echo Found Ziniao:
echo %ZINIAO_EXE%
echo.

if /I "%ZINIAO_CHECK_ONLY%"=="1" goto :check_ok

rem If the local API already answers, do not launch a duplicate process.
call :port_check
if !errorlevel! equ 0 goto :already_ready

rem Normal and WebDriver modes cannot run together.
set "HAS_ZINIAO=0"
tasklist /FI "IMAGENAME eq ziniao.exe" 2>nul | find /I "ziniao.exe" >nul && set "HAS_ZINIAO=1"
tasklist /FI "IMAGENAME eq ziniaobrowser.exe" 2>nul | find /I "ziniaobrowser.exe" >nul && set "HAS_ZINIAO=1"

if "!HAS_ZINIAO!"=="1" (
    echo Ziniao is running without the local WebDriver API.
    echo Switching modes will close all open Ziniao store windows.
    echo.
    choice /C YN /N /M "Switch to WebDriver mode? [Y/N]: "
    if errorlevel 2 goto :cancelled
    echo.
    echo Closing existing Ziniao processes...
    taskkill /F /T /IM ziniaobrowser.exe >nul 2>&1
    taskkill /F /T /IM ziniao.exe >nul 2>&1
    ping 127.0.0.1 -n 4 >nul
)

echo Starting Ziniao WebDriver mode on port %PORT%...
for %%D in ("%ZINIAO_EXE%") do set "ZINIAO_DIR=%%~dpD"
start "" /D "%ZINIAO_DIR%" "%ZINIAO_EXE%" --run_type=web_driver --ipc_type=http --port=%PORT%

echo Waiting for the local API, up to 60 seconds...
for /L %%I in (1,1,60) do (
    call :port_check
    if !errorlevel! equ 0 goto :ready
    ping 127.0.0.1 -n 2 >nul
)

color 0E
echo.
echo [TIMEOUT] Ziniao started, but port %PORT% is not accepting connections.
echo Check the Ziniao WebDriver permission, then run this file again.
goto :end_error

:port_check
powershell.exe -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try{$c.Connect('127.0.0.1',%PORT%);exit 0}catch{exit 1}finally{$c.Dispose()}"
exit /b %errorlevel%

:check_ok
color 0A
echo [CHECK OK] The configured Ziniao executable exists.
goto :end_ok

:ready
color 0A
echo.
echo ==================================================
echo [SUCCESS] Ziniao WebDriver mode is ready.
echo API: http://127.0.0.1:%PORT%
echo ==================================================
echo Return to http://127.0.0.1:8765/stores and click Sync Stores.
goto :end_ok

:already_ready
color 0A
echo [OK] Port %PORT% is already accepting connections.
goto :end_ok

:not_found
color 0C
echo [ERROR] ziniao.exe was not found.
echo Expected path: D:\[Chinese Ziniao Browser folder]\ziniao\ziniao.exe
goto :end_error

:cancelled
echo Cancelled. Existing Ziniao processes were not changed.
goto :end_ok

:end_error
echo.
if /I not "%ZINIAO_NO_PAUSE%"=="1" pause
exit /b 1

:end_ok
echo.
if /I not "%ZINIAO_NO_PAUSE%"=="1" pause
exit /b 0
