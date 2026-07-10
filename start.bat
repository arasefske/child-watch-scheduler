@echo off
title Child Watch Scheduler

echo Starting Child Watch Scheduler...
echo.

:: Verify Docker Desktop is running before attempting anything.
echo Checking Docker status...
docker info >nul 2>&1
if errorlevel 1 (
    echo.
    echo Docker is not running.
    echo Please open Docker Desktop, wait for it to finish starting, then run this again.
    pause
    exit /b 1
)

echo Checking for updates...
git pull
echo.

echo Applying any updates...
docker-compose down
docker-compose up --build -d

echo Waiting for app to be ready...
timeout /t 5 /nobreak >nul

start http://localhost:8501

echo.
echo Scheduler is running at http://localhost:8501
echo Run stop.bat to shut it down.
echo.
pause
