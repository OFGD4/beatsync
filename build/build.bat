@echo off
setlocal
rem ---------------------------------------------------------------------------
rem  Builds the Windows app:
rem    dist\BeatSync\BeatSync.exe             portable app folder
rem    dist\BeatSync-Setup-VERSION.exe      installer (needs Inno Setup 6)
rem  Run it by double-clicking, or from a terminal in the repo:  build\build.bat
rem ---------------------------------------------------------------------------
cd /d "%~dp0.."

set "PY=python"
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"

if not exist ".venv-build\Scripts\python.exe" (
    echo [1/5] Creating build environment with %PY% ...
    %PY% -m venv .venv-build || goto :fail
)
set "VPY=.venv-build\Scripts\python.exe"

echo [2/5] Installing dependencies ...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r requirements-desktop.txt --quiet || goto :fail

echo [3/5] Bundling Rubber Band ...
"%VPY%" build\get_tools.py rubberband || echo     (skipped - the app will use its basic stretch)

echo [4/5] Building BeatSync.exe ...
"%VPY%" -m PyInstaller build\beatsync.spec --noconfirm --clean --workpath .pyi-work --distpath dist --log-level WARN || goto :fail
if exist "build\bin" xcopy /y /i /q "build\bin\*" "dist\BeatSync\bin\" >nul

for /f "delims=" %%v in ('"%VPY%" -c "import app_info; print(app_info.APP_VERSION)"') do set "VER=%%v"
for /f "delims=" %%u in ('"%VPY%" -c "import app_info; print(app_info.GITHUB_REPO)"') do set "REPOURL=https://github.com/%%u"

echo [5/5] Building installer ...
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC (
    "%ISCC%" /Q "/DAppVersion=%VER%" "/DAppURL=%REPOURL%" build\installer.iss || goto :fail
    echo.
    echo Installer: dist\BeatSync-Setup-%VER%.exe
) else (
    echo     Inno Setup 6 not found - skipped. Get it free from https://jrsoftware.org/isdl.php
)

echo.
echo Done. Portable app: dist\BeatSync\BeatSync.exe
exit /b 0

:fail
echo.
echo BUILD FAILED - see the messages above.
exit /b 1
