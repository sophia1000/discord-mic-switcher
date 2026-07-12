@echo off
setlocal
cd /d "%~dp0"
dotnet publish "MicBridge\MicBridge.csproj" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:PublishTrimmed=false -o "dist\MicBridge"
if errorlevel 1 exit /b %errorlevel%
if exist "dist\MicBridge\MicBridge.pdb" del /q "dist\MicBridge\MicBridge.pdb"
echo.
echo Built: %~dp0dist\MicBridge\MicBridge.exe
pause
