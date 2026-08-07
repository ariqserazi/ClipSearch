@echo off
setlocal

docker compose build drama-clip-scout
docker compose up -d --no-deps --force-recreate drama-clip-scout

echo Updated only drama-clip-scout. Hermes and local data were not touched.
