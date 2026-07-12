@echo off
setlocal
cd /d "%~dp0"

call build-exe.bat --no-pause
if errorlevel 1 exit /b %errorlevel%

set "ISCC="
where iscc.exe >nul 2>nul && set "ISCC=iscc.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo.
    echo Inno Setup 6 is required to build the installer.
    echo Install it with: winget install --id JRSoftware.InnoSetup -e
    echo Then run build-installer.bat again.
    pause
    exit /b 2
)

"%ISCC%" "installer\SophiasMicBridge.iss"
if errorlevel 1 exit /b %errorlevel%

echo.
echo Installer built: %~dp0dist\installer\SophiasMicBridgeSetup.exe
pause
