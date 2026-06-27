@echo off
echo Stopping Kraken Bot...
taskkill /f /im pythonw.exe 2>nul
taskkill /f /im python.exe /fi "WINDOWTITLE eq bot.py" 2>nul
echo Done.
pause
