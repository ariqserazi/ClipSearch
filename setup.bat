@echo off
setlocal

echo Creating Docker network drama-net if needed...
docker network inspect drama-net >nul 2>&1 || docker network create drama-net >nul

:: Check if a container named hermes exists
docker ps -a --format "{{.Names}}" | findstr /x "hermes" >nul 2>&1
if %errorlevel%==0 (
    docker inspect -f "{{json .NetworkSettings.Networks}}" hermes | findstr "drama-net" >nul 2>&1
    if %errorlevel%==0 (
        echo Hermes container is already connected to drama-net.
    ) else (
        echo Connecting existing hermes container to drama-net...
        docker network connect drama-net hermes
    )
) else (
    echo No Docker container named hermes was found. Skipping Hermes network connection.
)

if not exist .env (
    copy .env.example .env >nul
    echo Created .env from .env.example.
) else (
    echo .env already exists. Leaving it unchanged.
)

if not exist data mkdir data

echo.
echo Add your Reddit and X API credentials manually to:
echo %~dp0.env
echo.
echo Safety note: this setup does not delete Hermes data.
