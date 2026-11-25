@echo off
REM TA Gyaani - One-Click Starter for Windows
REM Double-click this file to start the application

setlocal EnableDelayedExpansion

REM Change to script directory
cd /d "%~dp0"

REM Set console colors
color 0F

echo.
echo ================================================================
echo                           TA Gyaani
echo ================================================================
echo.

REM ============================================
REM Step 1: Check Docker installation
REM ============================================
echo [1/5] Checking Docker installation...

where docker >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Docker is not installed.
    echo.
    echo Please install Docker Desktop from:
    echo   https://www.docker.com/products/docker-desktop/
    echo.
    echo After installation:
    echo   1. Open Docker Desktop
    echo   2. Wait for it to start
    echo   3. Double-click this file again
    echo.
    echo Opening Docker download page...
    start https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

echo [OK] Docker is installed.

REM ============================================
REM Step 2: Check Docker is running
REM ============================================
echo [2/5] Checking Docker status...

docker info >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [INFO] Docker Desktop is not running. Starting it now...

    REM Try to start Docker Desktop
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" 2>nul
    if %ERRORLEVEL% NEQ 0 (
        start "" "%LOCALAPPDATA%\Docker\Docker Desktop.exe" 2>nul
    )

    echo Waiting for Docker to start...
    set /a counter=0

    :waitloop
    timeout /t 2 /nobreak >nul
    docker info >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        echo.
        echo [OK] Docker is now running.
        goto :docker_ready
    )

    set /a counter+=1
    if !counter! LSS 60 (
        echo|set /p="."
        goto :waitloop
    )

    echo.
    echo [ERROR] Docker failed to start within 2 minutes.
    echo Please start Docker Desktop manually and try again.
    pause
    exit /b 1
)

:docker_ready
echo [OK] Docker is running.

REM ============================================
REM Step 3: Check the settings file. The app refuses to start without the
REM Dhan login and a username and password for the dashboard.
REM ============================================
echo [3/5] Checking the settings file (.env)...

set ENV_OK=1
if not exist ".env" set ENV_OK=0
if "%ENV_OK%"=="1" (
    for %%K in (DHAN_CLIENT_ID DHAN_ACCESS_TOKEN DASHBOARD_USER DASHBOARD_PASSWORD) do (
        findstr /r /c:"^ *%%K *= *[^ #]" ".env" >nul 2>&1 || set ENV_OK=0
    )
)
if "%ENV_OK%"=="0" (
    echo.
    echo [ERROR] The settings file .env is missing or incomplete.
    echo.
    echo Copy .env.example to a file named .env in this folder and fill in
    echo DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, DASHBOARD_USER and DASHBOARD_PASSWORD.
    echo See README.md, "Setup".
    pause
    exit /b 1
)
echo [OK] Settings found.

REM ============================================
REM Step 4: Build and start containers
REM ============================================
echo [4/5] Preparing containers...

REM --build on EVERY start: without it, a code change keeps running the old
REM image until someone rebuilds by hand. When nothing changed, Docker's layer
REM cache makes the build take seconds.
echo [INFO] Building (if the code changed) and starting TA Gyaani...
echo The first run takes a few minutes.

docker compose up -d --build
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Failed to start services.
    pause
    exit /b 1
)

echo.
echo [OK] All services started successfully.

REM ============================================
REM Step 5: Show status and open dashboard
REM ============================================
echo [5/5] Launching application...
echo.
echo Service Status:
docker compose ps

echo.
echo ================================================================
echo              Application Started Successfully!
echo ================================================================
echo.
echo Dashboard URL: http://localhost:8054
echo.
echo To stop the application:
echo   - Double-click Stop.bat
echo   - Or, in Docker Desktop, find 'ta-trading-system' in the Containers
echo     list and click its Stop button
echo.
echo Or run: docker compose down
echo.

REM Wait a moment then open dashboard
timeout /t 10 /nobreak >nul
start http://localhost:8054

echo Done! You can close this window.
echo.
pause
