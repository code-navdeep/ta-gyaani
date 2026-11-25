@echo off
setlocal
cd /d "%~dp0"

where docker-compose >nul 2>&1
if errorlevel 1 (
    set DC=docker compose
) else (
    set DC=docker-compose
)

cls
echo ================================================
echo   TA Gyaani - Dhan Token Renewal
echo ================================================
echo.

docker info >nul 2>&1
if errorlevel 1 (
    echo FAILED: Docker is not running.
    echo Please open Docker Desktop and try again.
    echo.
    pause
    exit /b 1
)

set IMG_ID=
for /f "delims=" %%i in ('%DC% images -q trading_system 2^>nul') do set IMG_ID=%%i
if "%IMG_ID%"=="" (
    echo First-time setup: building image ^(takes ~1 minute^)...
    echo.
    %DC% build trading_system
    if errorlevel 1 (
        echo.
        echo FAILED: Image build failed.
        echo.
        pause
        exit /b 1
    )
    echo.
)

%DC% run --rm trading_system python scripts/renew_token.py
set EXIT_CODE=%errorlevel%

echo.
pause
exit /b %EXIT_CODE%
