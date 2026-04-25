@echo off
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.10+ from https://www.python.org/ and ensure "Add to PATH" is checked.
  exit /b 1
)

python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt

pyinstaller --noconfirm radio_monitor.spec

echo.
echo Build output: dist\RadioMonitor\RadioMonitor.exe
echo Copy the entire dist\RadioMonitor folder when distributing (not only the .exe).
echo Optional: install FFmpeg and add it to PATH so MP3 decoding is reliable.
endlocal
