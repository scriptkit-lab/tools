@echo off
setlocal
cd /d "%~dp0"
rem DEMO di Manutenzione 2.1: colonnine finte + 30 giorni di storico finto.
rem Usa una cartella dati separata (%USERPROFILE%\Manut21\demo) e la porta 8771: i dati veri non vengono toccati.
set "PY="
if exist "%USERPROFILE%\PoleDesk\venv\Scripts\python.exe" set "PY=%USERPROFILE%\PoleDesk\venv\Scripts\python.exe"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY set "PY=python"
set "MANUT21_DATA=%USERPROFILE%\Manut21\demo"
set "MANUT21_PORT=8771"
if not exist "%MANUT21_DATA%" mkdir "%MANUT21_DATA%"
if not exist "%MANUT21_DATA%\colonnine.csv" copy /Y "%~dp0colonnine_prova.csv" "%MANUT21_DATA%\colonnine.csv" >nul
start "Manutenzione 2.1 - colonnine finte" /min %PY% "%~dp0simulatore.py"
if not exist "%MANUT21_DATA%\giri" (
  echo Preparo 30 giorni di storico di prova...
  %PY% "%~dp0genera_storico.py"
)
echo.
echo Demo su http://127.0.0.1:8771  - premi "Avvia diagnosi" nella Flotta per una lettura nuova.
%PY% "%~dp0..\server.py"
pause
