@echo off
echo Building Ova Viewer executable...
echo.

REM Check if PyInstaller is installed
python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install pyinstaller
)

REM Create icon if it doesn't exist
if not exist "icon.ico" (
    if not exist "icon.png" (
        echo Creating icon files...
        python create_simple_icon.py
    )
)

REM Build the executable (Qt splash handles loading feedback)
if exist "icon.ico" (
    echo Building with icon...
    pyinstaller --noconfirm --onefile --windowed --icon=icon.ico --add-data="icon.png;." --add-data="icon.ico;." --add-data="ova_viewer.png;." --name="Ova Viewer" main.py
) else (
    echo Building without icon...
    pyinstaller --noconfirm --onefile --windowed --add-data="ova_viewer.png;." --name="Ova Viewer" main.py
)

echo.
echo Build complete! Check the dist\ folder for your executable.
echo The executable will show the icon in taskbar and file explorer.
pause
