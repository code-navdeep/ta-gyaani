@echo off
REM TA Gyaani - One-Click Stop for Windows
REM Double-click this file to stop the application

setlocal EnableDelayedExpansion

REM Change to script directory
cd /d "%~dp0"

echo.
echo ================================================================
echo                     TA Gyaani - Stopping
echo ================================================================
echo.

REM Check if Docker is available
where docker >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Docker is not installed.
    pause
    exit /b 1
)

REM Check if Docker is running
docker info >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] Docker is not running. Nothing to stop.
    pause
    exit /b 0
)

REM Stop containers
echo [INFO] Stopping TA Gyaani...

docker compose down
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [OK] All services stopped successfully.
) else (
    echo.
    echo [ERROR] Failed to stop some services.
)

echo.
echo Application stopped.
echo.
pause
