@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 阿里云余额批量看板

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

where python >nul 2>nul
if %errorlevel%==0 (
  python -m aliyun_balance %*
  goto done
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m aliyun_balance %*
  goto done
)

echo.
echo  [错误] 没有找到 Python 3。
echo  请先安装 Python 3.10+（安装时勾选 Add Python to PATH），再双击本文件。
echo.
pause
exit /b 1

:done
if errorlevel 1 pause
