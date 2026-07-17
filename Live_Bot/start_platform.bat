@echo off
echo ============================================
echo   Fibonacci Bot - PLATFORM (multi-user SaaS)
echo ============================================
echo Запуск platform_bot.py (мульти-юзер, подписки)
echo ВНИМАНИЕ: не запускай одновременно со старым bot.py (один Telegram-токен!)
echo.
REM Ручной запуск в текущем окне. Для фонового автозапуска с перезапуском
REM при падении используй: install_service.ps1 -Script platform_bot.py
cd /d "%~dp0"
if exist "..\venv\Scripts\python.exe" (
    set PYTHON=..\venv\Scripts\python.exe
) else (
    set PYTHON=python
)
%PYTHON% platform_bot.py
pause
