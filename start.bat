@echo off
title Child Watch Scheduler

:: If we were re-launched after a git pull, skip straight to the docker steps.
if "%1"=="launch" goto :launch

echo Starting Child Watch Scheduler...
echo.

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

:: Re-launch this script as a new process so the docker commands below
:: run from the freshly-pulled file. Without this, Windows reads the
:: batch file line-by-line from disk and can land mid-word if git pull
:: changes the file length while it is still executing.
cmd /c "%~f0" launch
exit /b

:launch
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
