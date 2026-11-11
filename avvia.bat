@echo off
setlocal
cd /d "%~dp0"
rem Manutenzione 2.1: usa il Python di PoleDesk se c'e', altrimenti quello installato.
set "PY="
if exist "%USERPROFILE%\PoleDesk\venv\Scripts\python.exe" set "PY=%USERPROFILE%\PoleDesk\venv\Scripts\python.exe"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
  echo ERRORE: Python non trovato. Installa Python 3.11 o 3.12 da python.org oppure avvia prima PoleDesk.
  pause
  exit /b 1
)
%PY% -c "import paramiko,sys; sys.exit(0 if paramiko.__version__.startswith('3') else 1)" >nul 2>nul || (
  echo paramiko 3 non trovato: provo a installarlo per i controlli SSH. Senza, il tool usa solo la pagina di stato.
  %PY% -m pip install "paramiko>=3.4,<4" >nul 2>nul || %PY% -m pip install --user "paramiko>=3.4,<4" >nul 2>nul
)
%PY% "%~dp0server.py"
pause
