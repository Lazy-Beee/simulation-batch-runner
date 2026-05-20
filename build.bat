@echo off
REM Build the two onefile Windows x64 binaries for a release.
REM Assumes pyinstaller is installed in the active Python env:
REM     pip install pyinstaller
REM
REM Outputs (written to dist/):
REM     dist\batch_simu_tui.exe   - Textual TUI frontend
REM     dist\batch_simu_cli.exe   - CLI frontend
REM
REM Both exes load config.json from the same directory as the exe (see
REM simulation.py:_app_root). config.json is gitignored and never bundled
REM into the exe - users copy config.example.json next to the exe and edit.

setlocal
set HERE=%~dp0
cd /d "%HERE%"

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
del /q batch_simu_tui.spec 2>nul
del /q batch_simu_cli.spec 2>nul

REM textual lazy-loads widget submodules via __getattr__, so PyInstaller's
REM static analysis misses them. --collect-submodules pulls them all in.
pyinstaller --onefile --console --name batch_simu_tui --collect-submodules textual batch_simu_tui.py
if errorlevel 1 (
    echo Build failed: batch_simu_tui
    exit /b 1
)

pyinstaller --onefile --console --name batch_simu_cli batch_simu_cli.py
if errorlevel 1 (
    echo Build failed: batch_simu_cli
    exit /b 1
)

echo.
echo Build complete:
dir /b dist
endlocal
