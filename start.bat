@echo off
chcp 65001 >nul
title 抖音续火花助手
cd /d "%~dp0"

echo ============================================
echo   抖音续火花助手 - Windows 一键启动
echo ============================================

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+ 并勾选 "Add to PATH"
    pause
    exit /b 1
)

echo [0/4] 检查端口 8000 是否被旧服务占用...
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /r ":8000.*LISTENING"') do (
    if not "%%a"=="0" (
        echo       发现占用 8000 的进程 PID %%a，正在结束旧服务...
        taskkill /F /PID %%a >nul 2>nul
        set KILLED=1
    )
)
if "%KILLED%"=="1" (
    timeout /t 2 /nobreak >nul
    echo       旧服务已清理
) else (
    echo       端口 8000 空闲
)

echo [1/4] 检查依赖包...
python -c "import fastapi, uvicorn, playwright, apscheduler, multipart, pydantic" >nul 2>nul
if errorlevel 1 (
    echo       正在安装依赖，请稍候...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
)

echo [2/4] 检查 Chromium 浏览器（已安装则秒过）...
set CHROME_FOUND=0
for /d %%d in ("%LOCALAPPDATA%\ms-playwright\chromium-*") do (
    if exist "%%d\chrome-win\chrome.exe" set CHROME_FOUND=1
    if exist "%%d\chrome-win64\chrome.exe" set CHROME_FOUND=1
    if exist "%%d\chrome-linux\chrome" set CHROME_FOUND=1
)
if "%CHROME_FOUND%"=="0" (
    echo       未检测到 Chromium，尝试自动安装（首次约 150MB）...
    python -m playwright install chromium
    if errorlevel 1 (
        echo       [警告] Chromium 安装失败，服务仍会启动，但发送/登录功能需装好浏览器后才能用
    )
)

echo [3/4] 启动服务...
echo       网页地址: http://127.0.0.1:8000
echo       访问令牌: 见本目录 .env 文件中的 AUTH_TOKEN
echo       按 Ctrl+C 停止服务
echo.
rem 稍等 3 秒等服务就绪后再用系统默认浏览器打开网页
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000"
python app.py
echo.
echo [错误] 服务异常退出了！请把上面窗口里的提示信息发给我排查。
echo 日志文件位置: data\logs\app.log
pause
