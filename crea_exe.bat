@echo off
setlocal
cd /d "%~dp0"
rem Crea dist\PoleDesk.exe: un unico file con Python, librerie e PoleDesk.
rem Va lanciato una sola volta, su un PC Windows con Python e internet.
rem L'eseguibile ottenuto si copia sugli altri PC e parte senza installare nulla.

echo.
echo === Creazione di PoleDesk.exe ===
echo.

set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
  echo ERRORE: su questo PC serve Python, solo per creare l'eseguibile.
  echo Installalo da python.org spuntando "Add python.exe to PATH", poi rilancia crea_exe.bat.
  pause
  exit /b 1
)

set "BUILD=%TEMP%\poledesk-build"
if exist "%BUILD%" rmdir /s /q "%BUILD%"

echo [1/3] Preparo un ambiente temporaneo in %BUILD% ...
%PYEXE% -m venv "%BUILD%\venv"
if not exist "%BUILD%\venv\Scripts\python.exe" (
  echo ERRORE: ambiente temporaneo non creato.
  pause
  exit /b 1
)

echo [2/3] Scarico paramiko e PyInstaller: serve internet o il proxy aziendale ...
"%BUILD%\venv\Scripts\python" -m pip install --quiet --upgrade pip
"%BUILD%\venv\Scripts\python" -m pip install --quiet "paramiko>=3.4,<4" "pyinstaller>=6,<7"
if errorlevel 1 (
  echo ERRORE: download delle librerie non riuscito. Controlla la connessione o il proxy.
  pause
  exit /b 1
)

echo [3/3] Creo l'eseguibile, ci vuole circa un minuto ...
"%BUILD%\venv\Scripts\python" -m PyInstaller --noconfirm --clean --onefile --console --name PoleDesk ^
  --hidden-import statistics --hidden-import zipfile --hidden-import tarfile --hidden-import decimal --hidden-import fractions ^
  --add-data "%~dp0console.html;." --add-data "%~dp0prova_schermo.sh;." --add-data "%~dp0seq_T2.conf;." --add-data "%~dp0seq_T2L.conf;." ^
  --distpath "%~dp0dist" --workpath "%BUILD%\work" --specpath "%BUILD%" ^
  "%~dp0server.py"
if not exist "%~dp0dist\PoleDesk.exe" (
  echo ERRORE: eseguibile non creato. Guarda i messaggi qui sopra.
  pause
  exit /b 1
)

rmdir /s /q "%BUILD%" >nul 2>nul
echo.
echo FATTO: %~dp0dist\PoleDesk.exe
echo Copialo sul PC dove ti serve e avvialo con doppio clic: non serve installare nulla.
echo I dati restano in %%USERPROFILE%%\PoleDesk\data, come con avvia.bat.
echo Aggiornamenti futuri: copia server.py e console.html della nuova versione accanto a PoleDesk.exe,
echo senza ricreare l'eseguibile.
pause
