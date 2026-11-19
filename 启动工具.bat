@echo off
chcp 65001 >nul
title dtbtool - DTS 编辑编译工具
cd /d "%~dp0"

echo ============================================================
echo   dtbtool - DTS 编辑 -^> 编译/分析 DTB (Windows 免安装)
echo   支持纯 DTS 与带 #include 的源码树 DTS
echo   内置硬件分析: 外设 / 引脚 / 频率 / 中断
echo ============================================================
echo.

REM ---------- 查找 Python ----------
set "PYEXE="
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -c "import sys" >nul 2>&1
    if %errorlevel%==0 set "PYEXE=py -3"
)
if not defined PYEXE (
    where python >nul 2>&1
    if %errorlevel%==0 set "PYEXE=python"
)
if not defined PYEXE (
    if exist "C:\Python313\python.exe" set "PYEXE=C:\Python313\python.exe"
)
if not defined PYEXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)

if not defined PYEXE (
    echo [错误] 未找到 Python。
    echo 本工具需要 Python 3 (任意 3.8 以上版本)，且无需安装任何第三方库。
    echo 请到 https://www.python.org/downloads/ 下载安装，安装时勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

echo 使用 Python: %PYEXE%
echo 正在启动浏览器界面 ...
echo 启动后浏览器会自动打开 http://127.0.0.1:8080
echo 关闭此窗口即可停止工具。
echo.

%PYEXE% dtbtool.py web --port 8080

echo.
pause
