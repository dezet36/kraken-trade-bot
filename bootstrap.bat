@echo off
rem ---------------------------------------------------------------------------
rem  Zapusk ustanovki dvoynym schelchkom.
rem
rem  Fayl .ps1 po dvoynomu schelchku ne zapuskaetsya - Windows otkryvaet ego v
rem  redaktore. Poetomu ryadom lezhit etot .bat: on prosto zovyot bootstrap.ps1
rem  s obhodom politiki vypolneniya i ostavlyaet okno otkrytym, chtoby oshibku
rem  bylo vidno, a ne uznavat o ney po ischeznuvshemu oknu.
rem
rem  Tekst zdes namerenno latinitsey: cmd.exe chitaet .bat v kodirovke konsoli,
rem  i kirillitsa v nyom prevrashchaetsya v musor na chasti sistem. Vsyo, chto
rem  uvidit polzovatel dalshe, pechataet uzhe PowerShell - tam s russkim vsyo v
rem  poryadke.
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

if not exist "%~dp0bootstrap.ps1" (
    echo.
    echo   [ERROR] Ryadom net fayla bootstrap.ps1
    echo.
    echo   Skopiruyte na server OBA fayla iz repozitoriya:
    echo       bootstrap.bat   ^(etot^)
    echo       bootstrap.ps1
    echo   i zapustite bootstrap.bat snova.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" %*
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
    echo   Ustanovka zavershilas s oshibkoy. Tekst oshibki vyshe.
)
pause
exit /b %RC%
