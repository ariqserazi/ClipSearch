@echo off
setlocal

docker compose stop drama-clip-scout 2>nul
docker rm drama-clip-scout 2>nul

echo Removed only the drama-clip-scout container.
echo SQLite data in .\data was not deleted.
echo Hermes and the hermes container were not touched.
echo.
echo Destructive data reset, if you truly want it later:
echo del "%~dp0data\clips.db" "%~dp0data\clips.db-shm" "%~dp0data\clips.db-wal"
