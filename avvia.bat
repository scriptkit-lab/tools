@echo off
setlocal
cd /d "%~dp0"
rem Ambiente Python e dati stanno in %USERPROFILE%\PoleDesk, fuori dalla cartella del programma,
rem cosi restano uguali tra una versione e l'altra. Non si usa AppData perche il Python
rem del Microsoft Store virtualizza quella cartella e l'ambiente diventerebbe invisibile.
set "PD_HOME=%USERPROFILE%\PoleDesk"
set "PD_VENV=%PD_HOME%\venv"
set "POLEDESK_DATA=%PD_HOME%\data"
if not exist "%PD_HOME%" mkdir "%PD_HOME%"

rem Si accetta solo un Python che parte davvero: il segnaposto "python" del Microsoft Store
rem esiste anche quando Python non e' installato, ma non esegue codice.
set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"

if not exist "%PD_VENV%\Scripts\python.exe" (
  if not defined PYEXE (
    echo ERRORE: Python non e' installato su questo PC.
    echo Il comando "python" presente in Windows e' solo un collegamento al Microsoft Store.
    echo.
    echo Installa Python 3.11 o 3.12 da python.org:
    echo  - spunta "Add python.exe to PATH"
    echo  - senza permessi di amministratore: Customize installation, togli "Install for all users"
    echo Poi chiudi questa finestra e riavvia avvia.bat.
    pause
    exit /b 1
  )
  echo Prima installazione: creo l'ambiente Python in %PD_VENV% ...
  %PYEXE% -m venv "%PD_VENV%"
)
if not exist "%PD_VENV%\Scripts\python.exe" (
  echo.
  echo ERRORE: l'ambiente Python non e' stato creato in %PD_VENV%
  echo Prova a reinstallare Python da python.org, spuntando "Add python.exe to PATH", e riavvia avvia.bat.
  pause
  exit /b 1
)

rem paramiko deve essere della serie 3: dalla 4 non supporta piu le chiavi SSH usate dalle colonnine piu vecchie
"%PD_VENV%\Scripts\python" -c "import paramiko,sys; sys.exit(0 if paramiko.__version__.split('.')[0]=='3' else 1)" 2>nul || (
  echo Installo le librerie necessarie...
  "%PD_VENV%\Scripts\python" -m pip install -r requirements.txt
)

rem Alla prima esecuzione recupera i dati di una versione precedente copiati in .\data
if not exist "%POLEDESK_DATA%\stations.json" if exist "%~dp0data\stations.json" (
  echo Trasferisco i dati della versione precedente in %POLEDESK_DATA% ...
  xcopy /E /I /Y "%~dp0data" "%POLEDESK_DATA%" >nul
)

"%PD_VENV%\Scripts\python" server.py
pause
