@echo off
REM Ручной запуск bot.py (личная торговля) в текущем окне. Для фонового
REM автозапуска с перезапуском при падении используй install_service.ps1.
cd /d "%~dp0"
if exist "..\venv\Scripts\python.exe" (
    set PYTHON=..\venv\Scripts\python.exe
) else (
    set PYTHON=python
)
echo Starting Kraken Bot (%PYTHON%)...
%PYTHON% bot.py
pause
