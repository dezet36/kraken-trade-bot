@echo off
REM Останавливает ручной (не-сервисный) запуск bot.py/platform_bot.py.
REM Если бот поставлен через install_service.ps1 — используй:
REM   schtasks /end /tn KrakenBot          (bot.py)
REM   schtasks /end /tn KrakenPlatformBot  (platform_bot.py)
echo Stopping Kraken Bot...
taskkill /f /im python.exe /fi "WINDOWTITLE eq bot.py*" 2>nul
taskkill /f /im python.exe /fi "WINDOWTITLE eq platform_bot.py*" 2>nul
echo Done.
pause
