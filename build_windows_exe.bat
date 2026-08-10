@echo off
REM Optional developer packaging step. PyInstaller is NOT required at runtime.
REM The bundled portfolio seed is embedded so the installed app remains offline.
cd /d "%~dp0"
py -m pip install -r requirements-build.txt
py -m PyInstaller --noconfirm --clean --windowed --name "InvoiceIntelligence" --add-data "data\pm.csv;data" main.py
pause
