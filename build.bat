@echo off
:: Build DualNet.exe (PyInstaller) lalu installer (Inno Setup)
:: Copyright (c) 2026 Bayu Saputra
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --windowed --uac-admin --name DualNet ^
  --icon assets\dualnet.ico --version-file version_info.txt ^
  --add-data "fonts;fonts" --add-data "assets;assets" ^
  --exclude-module unittest --exclude-module pydoc --exclude-module email ^
  --exclude-module http --exclude-module xml --exclude-module PIL ^
  dualnet.py || exit /b 1

set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
%ISCC% installer.iss || exit /b 1
echo.
echo Selesai: dist\DualNet\DualNet.exe  dan  release\DualNet-Setup-1.0.0.exe
