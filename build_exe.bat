@echo off
setlocal
cd /d "%~dp0"
echo Building Ova Viewer executable...
echo.

REM Build inside the project's virtual environment (.venv, Python 3.15), so the
REM exe bundles exactly the versions in requirements.txt - never whatever
REM happens to be installed globally.
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv ^(Python 3.15^)...
    py -3.15 -m venv .venv || goto :error
)
set "PY=.venv\Scripts\python.exe"

echo Installing / updating dependencies...
"%PY%" -m pip install --upgrade pip -q || goto :error
"%PY%" -m pip install -r requirements-dev.txt -q || goto :error

REM All build settings live in "Ova Viewer.spec". It is a one-folder build:
REM a one-file exe unpacks its ~350 MB bundle to %TEMP% on every launch,
REM which made startup ~4.3 s instead of ~0.9 s.
"%PY%" -m PyInstaller --noconfirm "Ova Viewer.spec" || goto :error

echo.
echo Build complete! Run dist\Ova Viewer\Ova Viewer.exe (keep the whole folder together).
echo Point shortcuts / file associations at that exe; it shows the icon in taskbar and file explorer.
pause
exit /b 0

:error
echo.
echo Build FAILED - see the messages above.
pause
exit /b 1
