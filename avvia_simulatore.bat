@echo off
cd /d "%~dp0"
set "PY="
if exist "%USERPROFILE%\PoleDesk\venv\Scripts\python.exe" set "PY=%USERPROFILE%\PoleDesk\venv\Scripts\python.exe"
if not defined PY py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY set "PY=python"
%PY% "%~dp0simulatore.py"
pause
