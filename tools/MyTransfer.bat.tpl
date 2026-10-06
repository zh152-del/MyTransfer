@echo off
setlocal
chcp 936 >nul
title MyTransfer - Windows 传文件到 iPhone

set "PROJ=F:\work Buddy\2026-10-05-17-12-22\MyTransfer"
cd /d "%PROJ%"
if errorlevel 1 (
  echo 项目目录没找到：%PROJ%
  pause
  exit /b 1
)

set "PY=C:\Program Files\Python313\python.exe"
if not exist "%PY%" set "PY="

rem 找一个能用的 python（要有 tkinter，否则界面起不来）
if not defined PY (
  for /f "delims=" %%I in ('where python 2^>nul') do (
    if not defined PY (
      "%%I" -c "import tkinter" >nul 2>nul
      if not errorlevel 1 set "PY=%%I"
    )
  )
)
if not defined PY (
  for /f "delims=" %%I in ('where py 2^>nul') do (
    if not defined PY set "PY=%%I"
  )
)
if not defined PY (
  echo.
  echo 没找到带 tkinter 的 Python。请装 Python 3.10 以上，
  echo 安装时务必勾选 Add python.exe to PATH，装完重新双击本文件。
  echo.
  pause
  exit /b 1
)

rem 界面用 pythonw 起：不弹黑窗，跟普通软件一样
set "PYW=%PY:\python.exe=\pythonw.exe%"
if not exist "%PYW%" set "PYW="

if defined PYW goto runw

echo 正在启动 MyTransfer（控制台模式）...
"%PY%" MyTransfer.py
goto :eof

:runw
echo 正在启动 MyTransfer（界面模式）...
start "" "%PYW%" MyTransfer.py
timeout /t 1 >nul
exit /b 0
