@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM 显示(可见)窗口模式开关：默认隐藏(无窗口)，加 --show 进入调试显示模式
set "SHOWN=0"
for %%a in (%*) do if /i "%%a"=="--show" set "SHOWN=1"

REM 端口单来源：WEB_PORT 同时驱动 PORT / URL
set "VENV_PY=.\.venv\Scripts\python.exe"
set "VENV_PYW=.\.venv\Scripts\pythonw.exe"
if not defined WEB_PORT set "WEB_PORT=50000"
set "PORT=%WEB_PORT%"
set "URL=http://127.0.0.1:%WEB_PORT%"
set "LOG=app.log"

if not exist "%VENV_PY%" (
    echo [ERROR] 虚拟环境未找到：%VENV_PY%
    echo 请先安装依赖：python -m venv .venv 然后 pip install -r requirements.txt
    pause
    exit /b 1
)

REM 清理端口上可能的旧实例（避免陈旧/崩溃的进程占着端口）
set "OLD_PID="
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr /r ":%PORT%[ ]"') do set "OLD_PID=%%a"
if defined OLD_PID (
    echo [INFO] 端口 %PORT% 被进程 %OLD_PID% 占用，正在结束旧实例...
    taskkill /PID !OLD_PID! /F 2>nul
    timeout /t 2 /nobreak 2>nul
)

echo 正在启动 RSS2Email 服务（端口 %PORT%）...
echo 日志写入：%LOG%

REM 启动服务：隐藏模式优先用 pythonw，否则降级为最小化窗口
if "%SHOWN%"=="0" (
    if exist "%VENV_PYW%" (
        start "RSS2Email" cmd /c ""%VENV_PYW%" app.py ^> "%LOG%" 2^>^&1"
    ) else (
        start /MIN "%VENV_PY%" app.py
    )
) else (
    start "RSS2Email" cmd /c ""%VENV_PY%" app.py ^> "%LOG%" 2^>^&1"
)

set "READY=0"
for /L %%i in (1,1,20) do (
    for /f "tokens=*" %%s in ('netstat -ano 2^>nul ^| findstr /r ":%PORT%[ ]"') do set "READY=1"
    if "!READY!"=="1" goto :OPEN
    timeout /t 1 /nobreak 2>nul
)

if "!READY!"=="0" (
    echo [ERROR] 服务在 20 秒内未启动，最后日志如下：
    echo ===== %LOG% =====
    type "%LOG%"
    echo ===================
    if "%SHOWN%"=="1" (
        echo 请查看上方日志，按任意键退出...
        pause
        exit /b 1
    )
    start "RSS2Email Log" cmd /k type "%LOG%"
    exit /b 1
)

:OPEN
echo 服务已就绪，正在打开 %URL% ...
start "" "%URL%"

if "%SHOWN%"=="1" (
    echo.
    echo RSS2Email 已启动（窗口模式 / DEBUG）。配置页面： %URL%
    echo 停止服务：网页点「停止服务」，或关闭标题为 RSS2Email 的 python 窗口
    pause
    exit /b 0
)

echo RSS2Email 已后台启动（无窗口）。配置页面： %URL%
exit /b 0
