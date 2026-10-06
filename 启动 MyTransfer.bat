@echo off
cd /d "%~dp0"
title MyTransfer
echo ============================================
echo   MyTransfer  Windows -> iPhone 局域网传输
echo ============================================
echo.
where python >nul 2>nul
if %errorlevel%==0 goto havepy
where py >nul 2>nul
if %errorlevel%==0 goto havepy3
echo [错误] 没有检测到 Python。
echo 请到 python.org 安装 Python 3.10 以上版本，安装时勾选 Add python.exe to PATH。
echo 或者在 MyTransfer 目录里执行：  pip install -r requirements.txt
echo.
pause
exit /b 1

:havepy
python MyTransfer.py
goto end

:havepy3
py -3 MyTransfer.py
goto end

:end
pause
