# Сборка CopierAdmin.exe (окно админки копировщика). Запуск:
#   powershell -ExecutionPolicy Bypass -File Copier\desktop\build_admin_exe.ps1
# Результат: Copier\desktop\dist\CopierAdmin.exe. Рядом с exe при первом запуске
# появятся copier_admin.json (адрес сервера, без паролей) и copier_admin.log.
#
# Без $ErrorActionPreference='Stop': PyInstaller пишет INFO в stderr, и
# PowerShell 5.1 принял бы это за ошибку. Успех проверяется по коду выхода.

Set-Location -Path $PSScriptRoot

python -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) { python -m pip install pyinstaller }

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name CopierAdmin `
    --hidden-import tkinter --hidden-import tkinter.ttk --hidden-import tkinter.filedialog `
    --paths . `
    admin_app.py 2>&1 | Select-Object -Last 3

if ($LASTEXITCODE -ne 0) { Write-Host "Сборка не удалась (код $LASTEXITCODE)"; exit 1 }
Write-Host "Готово: $PSScriptRoot\dist\CopierAdmin.exe"
