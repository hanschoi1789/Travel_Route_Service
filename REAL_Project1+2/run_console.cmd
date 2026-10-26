@echo off
chcp 65001 >nul
color 07
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run run.cmd first to install Python packages.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" selector.py
pause
