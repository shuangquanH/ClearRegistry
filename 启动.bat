@echo off
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if errorlevel 1 (
  echo pythonw.exe not found. Please install Python 3 first.
  pause
  exit /b 1
)
start "" pythonw.exe "RegistryCleaner.pyw"
