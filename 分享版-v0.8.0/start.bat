@echo off
chcp 65001 >nul
title 火花助手
cd /d "%~dp0"

echo ============================================
echo   火花助手 - Windows 一键启动
echo ============================================

echo [0/5] 准备 Python（优先用已有的，没有才下载）...
set "PYEXE="

rem 1）程序自带 Python
if exist "%~dp0python\python.exe" set "PYEXE=%~dp0python\python.exe"
if defined PYEXE goto :pycheck

rem 2）python-path.txt 自定义路径
if exist "%~dp0python-path.txt" (
    for /f "usebackq delims=" %%i in ("%~dp0python-path.txt") do set "PYEXE=%%i"
)
if defined PYEXE goto :pycheck

rem 3）py 启动器
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if defined PYEXE goto :pycheck

rem 4）常见安装目录
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python39\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
if not defined PYEXE if exist "C:\Python313\python.exe" set "PYEXE=C:\Python313\python.exe"
if not defined PYEXE if exist "C:\Python312\python.exe" set "PYEXE=C:\Python312\python.exe"
if not defined PYEXE if exist "C:\Python311\python.exe" set "PYEXE=C:\Python311\python.exe"
if not defined PYEXE if exist "C:\Python310\python.exe" set "PYEXE=C:\Python310\python.exe"
if not defined PYEXE if exist "C:\Python39\python.exe" set "PYEXE=C:\Python39\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python313\python.exe" set "PYEXE=%ProgramFiles%\Python313\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python312\python.exe" set "PYEXE=%ProgramFiles%\Python312\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python311\python.exe" set "PYEXE=%ProgramFiles%\Python311\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python310\python.exe" set "PYEXE=%ProgramFiles%\Python310\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python39\python.exe" set "PYEXE=%ProgramFiles%\Python39\python.exe"
if defined PYEXE goto :pycheck

rem 5）PATH 里的 python
for /f "delims=" %%i in ('where python 2^>nul') do set "PYEXE=%%i"

:pycheck
if not defined PYEXE goto :pyinstall
"%PYEXE%" -c "import sys" >nul 2>nul
if errorlevel 1 set "PYEXE="
if not defined PYEXE goto :pyinstall
echo       使用 Python: %PYEXE%
goto :pyok

:pyinstall
echo       没有找到 Python，开始下载并安装到本文件夹 python\（约 30MB，需联网）...
set "PYURL=https://www.python.org/ftp/python/3.13.3/python-3.13.3-amd64.exe"
set "PYMIRROR=https://mirrors.huaweicloud.com/python/3.13.3/python-3.13.3-amd64.exe"
set "PYDST=%~dp0python-installer.exe"
powershell -NoProfile -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri $env:PYURL -OutFile $env:PYDST -UseBasicParsing -TimeoutSec 60; exit 0 } catch { try { Invoke-WebRequest -Uri $env:PYMIRROR -OutFile $env:PYDST -UseBasicParsing -TimeoutSec 60; exit 0 } catch { exit 1 } }" >nul 2>nul
if errorlevel 1 (
    echo       [错误] Python 下载失败，请检查网络后重试，或在 python-path.txt 里写 python.exe 完整路径
    pause
    exit /b 1
)
"%PYDST%" /quiet InstallAllUsers=0 TargetDir="%~dp0python" Include_launcher=0 Include_test=0 Include_doc=0 Shortcuts=0 AssociateFiles=0 PrependPath=0
del "%PYDST%" >nul 2>nul
if exist "%~dp0python\python.exe" set "PYEXE=%~dp0python\python.exe"
if not defined PYEXE (
    echo       [错误] Python 安装失败，请稍后重试，或在 python-path.txt 里写 python.exe 完整路径
    pause
    exit /b 1
)
echo       使用 Python: %PYEXE%

:pyok
echo [1/5] 检查端口 8000...
set KILLED=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /r ":8000.*LISTENING"') do (
    if not "%%a"=="0" (
        echo       端口 8000 被占用，PID %%a，正在结束旧服务...
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

echo [2/5] 检查依赖包（已装的直接跳过）...
"%PYEXE%" -c "import fastapi, uvicorn, playwright, apscheduler, multipart, pydantic" >nul 2>nul
if errorlevel 1 (
    echo       正在安装依赖，请稍候...
    "%PYEXE%" -m pip install -r requirements.txt --retries 1 --timeout 10
    if errorlevel 1 (
        echo       常规源失败，可能是本地代理没开。清代理 + 国内镜像重试...
        set "HTTP_PROXY="
        set "HTTPS_PROXY="
        set "ALL_PROXY="
        set "http_proxy="
        set "https_proxy="
        set "all_proxy="
        "%PYEXE%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --retries 1 --timeout 15
        if errorlevel 1 (
            echo [错误] 依赖安装失败，请检查网络，或关闭代理软件后重试
            pause
            exit /b 1
        )
    )
)

echo [3/5] 检查 Chromium 浏览器（已装的直接跳过）...
set CHROME_FOUND=0
rem 1）本文件夹 browsers\ 里有没有
for /d %%d in ("%~dp0browsers\chromium-*") do (
    if exist "%%d\chrome-win\chrome.exe" set CHROME_FOUND=1
    if exist "%%d\chrome-win64\chrome.exe" set CHROME_FOUND=1
    if exist "%%d\chrome-linux\chrome" set CHROME_FOUND=1
)
rem 2）系统默认位置 ms-playwright 里有没有
if "%CHROME_FOUND%"=="0" for /d %%d in ("%LOCALAPPDATA%\ms-playwright\chromium-*") do (
    if exist "%%d\chrome-win\chrome.exe" set CHROME_FOUND=1
    if exist "%%d\chrome-win64\chrome.exe" set CHROME_FOUND=1
    if exist "%%d\chrome-linux\chrome" set CHROME_FOUND=1
)
if "%CHROME_FOUND%"=="0" (
    echo       Chromium 未找到，下载到本文件夹 browsers\，约 150MB...
    set "HTTP_PROXY="
    set "HTTPS_PROXY="
    set "ALL_PROXY="
    set "http_proxy="
    set "https_proxy="
    set "all_proxy="
    set "PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright"
    "%PYEXE%" -m playwright install chromium
    if errorlevel 1 (
        echo       国内镜像失败，换官方源重试...
        set "PLAYWRIGHT_DOWNLOAD_HOST="
        "%PYEXE%" -m playwright install chromium
    )
    if errorlevel 1 (
        echo       [警告] Chromium 下载失败，服务仍会启动，但发送/扫码功能需要它
    )
)

echo [4/5] 启动服务...
rem 只有本文件夹 browsers\ 有浏览器时才用它，否则用系统默认位置
set "LOCAL_CHROME=0"
for /d %%d in ("%~dp0browsers\chromium-*") do set "LOCAL_CHROME=1"
if "%LOCAL_CHROME%"=="1" set "PLAYWRIGHT_BROWSERS_PATH=%~dp0browsers"
echo       网页地址: http://127.0.0.1:8000
echo       访问令牌: 见本目录 .env 文件（首次启动自动生成并显示在上方）
echo       按 Ctrl+C 停止服务
echo.
rem 等 3 秒后用默认浏览器打开网页
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000"
"%PYEXE%" app.py
echo.
echo [错误] 服务异常退出了！请把上面窗口里的提示信息发给我排查。
echo 日志文件位置: data\logs\app.log
pause
