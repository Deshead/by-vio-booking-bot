@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found. Follow the setup steps in README.md.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" bot.py
if errorlevel 1 (
    echo Bot stopped with an error. See the output above.
    pause
    exit /b 1
)
