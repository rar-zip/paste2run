@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo.
  echo   [Paste2Run] 没找到 Python。
  echo   请先安装 Python 3.8 或更高版本：https://www.python.org/downloads/
  echo   安装时记得勾选 "Add Python to PATH"。
  echo.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

%PY% p2r.py up %*
pause
