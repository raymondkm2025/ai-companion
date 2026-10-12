@echo off
setlocal
cd /d "%~dp0"

if "%ANIMA_PORT%"=="" set ANIMA_PORT=8731
if "%ANIMA_HOST%"=="" set ANIMA_HOST=127.0.0.1

echo [anima] starting memory service on %ANIMA_HOST%:%ANIMA_PORT%
python "%~dp0companion\server.py" --host %ANIMA_HOST% --port %ANIMA_PORT% --data "%~dp0companion\data\memory.jsonl"
