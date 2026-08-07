@echo off
setlocal

curl -sS -X POST http://127.0.0.1:8787/collect/reddit -H "Content-Type: application/json" -d "{}"
echo.
