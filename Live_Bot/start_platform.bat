@echo off
cd /d "%~dp0"
echo ============================================
echo   Fibonacci Bot - PLATFORM (multi-user SaaS)
echo ============================================
echo Запуск platform_bot.py (мульти-юзер, подписки)
echo ВНИМАНИЕ: не запускай одновременно со старым bot.py (один Telegram-токен!)
echo.
python platform_bot.py
pause
