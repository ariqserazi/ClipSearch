@echo off
setlocal

if not exist .env (
    copy .env.example .env >nul
    echo Created .env from .env.example.
    echo Reminder: add your Reddit and X API credentials to .env when you are ready.
    echo.
)

docker network inspect drama-net >nul 2>&1
if %errorlevel% neq 0 (
    echo Docker network drama-net does not exist yet.
    echo Run setup.bat first so Drama Clip Scout can share a network with Hermes.
    exit /b 1
)

docker compose up -d --build drama-clip-scout

echo.
echo Drama Clip Scout UI:
echo http://127.0.0.1:8787/ui
echo.
echo Drama Clip Scout API:
echo http://127.0.0.1:8787
echo.
echo Drama Clip Scout API Docs:
echo http://127.0.0.1:8787/docs
echo.
echo Hermes Dashboard:
echo http://127.0.0.1:9119
echo.
echo Hermes Gateway:
echo http://127.0.0.1:8642
echo.
echo Internal Docker URL for Hermes to call:
echo http://drama-clip-scout:8787/agent/search-clips
