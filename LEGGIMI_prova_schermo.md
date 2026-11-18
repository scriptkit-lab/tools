# Prova schermo sulla colonnina — 2.3.1

Acquisizione dati per il rilevamento del distacco dello schermo, eseguita direttamente sulla colonnina, senza PC.

## File

- `generatore_prova_schermo.html`: si apre con il browser. Si scelgono passi, tempi, orari, controlli e regole; produce **un unico file** `installa_prova_schermo.sh` da dare al collega.
- `prova_schermo.sh`: lo script (è già dentro l'installatore; si può anche usare da solo, vedi sotto).
- `seq_T1.conf`, `seq_T2.conf`, `seq_T2L.conf`: sequenze modificabili (T1 caratterizzazione 3 h 20, T2 46 min, T2L 61 min con carica 600 s e recupero 1200 s). Ogni file ha anche i suoi limiti di sicurezza `AUX_MAX` e `TEMP_MAX`; il guardiano a tempo si calcola dalla durata della sequenza. Le sequenze si possono anche scrivere direttamente nel comando. PoleDesk le gestisce nella pagina Test di laboratorio → Sequenze.

## Sul posto

1. Copiare `installa_prova_schermo.sh` sulla colonnina, ad esempio in `/home/root/`.
2. `sh /home/root/installa_prova_schermo.sh`: installa script, impostazioni, orari e guardia in `/home/root/test`.
3. Primo controllo, senza comandare nulla: `sh /home/root/test/prova_schermo.sh --controlli 5`. Nel file `…_pagina.txt` ci sono tutti i nomi della pagina, da cui leggere i nomi esatti dei flag di display e PSG4X; se sono diversi, correggerli nel generatore e reinstallare.
4. Raccolta: `sh /home/root/test/prova_schermo.sh --archivio` crea un `.tar.gz` con tutti i dati da prelevare (anche quelli delle prove di funzionamento in `collaudo/`). Con PoleDesk 1.17, scheda Distribuzione, pacchetto «raccogli i dati», su tutte le colonnine insieme.

Altri comandi: `--stato`, `--ferma`, `--ripristina`. Per togliere orari e guardia: `sh installa_prova_schermo.sh --rimuovi`.

## Senza installare nulla: sequenze con i tempi scritti nel comando

Copiare in `/home/root/test/` solo `prova_schermo.sh` (e, se comodi, i file `seq_*.conf`). Nessun timer, nessun file di sistema: il piano è un processo in attesa che sparisce a fine piano, con `--annulla` o al riavvio della colonnina. Un solo piano alla volta.

**Sequenza** = `"NOME=ruolo:secondi ruolo:secondi ..."`, ruoli `rif` (tutto spento), `carica` (riscaldatore), `misura` (ventola dopo una carica), `ventola` (ventola), `recupero` (tutto spento). Più sequenze nel comando = eseguite in fila. Al posto della sequenza scritta si può dare il nome di un file `seq_NOME.conf` (es. `T1`, `T2`): i tempi stanno dentro e si modificano.

**Quando:** `--subito`, `--tra MINUTI`, `--alle HH:MM` (più orari: `--alle 21:00,01:00`).

**Opzioni:** `--giorni N` ripete ogni giorno (predefinito 1 = una volta) · `--nome attaccato|tolto` · `--note "testo"` · `--scala X` moltiplica tutti i tempi (0.1 = un decimo) · `--pausa MIN` tra sequenze (predefinito 60) · `--base MIN` di base stabile prima di ogni sequenza (predefinito 30, 0 = non aspetta) · `--prova` pausa e base a 0, nessun vincolo di accensione, dati in `collaudo/` · `--ip-schermo IP` ping al computer dello schermo a ogni campione · `--calendario "T2,T2L;-;T2L,T2"` sequenze notte per notte (una notte per gruppo separato da `;`, `-` = notte senza prove; un solo orario con `--alle`) · `--monitora MIN` nelle notti senza prove registra per MIN minuti senza comandare nulla. · `--parti-sempre` nessuna precondizione all'avvio (base stabile, ore di accensione, USER STATUS, prova precedente): resta solo la pausa tra le sequenze, e restano tutte le sicurezze durante la prova.

```
cd /home/root/test
# prova di funzionamento ADESSO, T1 e T2 corte in fila (circa 2 min)
sh prova_schermo.sh --subito --prova --nome attaccato "T1=rif:20 ventola:20 recupero:20 carica:20 recupero:20" "T2=rif:20 carica:20 misura:30 recupero:20 carica:20 misura:30"

# le stesse dai file, tempi a un centesimo (T1 circa 2 min, T2 circa 1 min: nessun passo scende sotto 10 s)
sh prova_schermo.sh --subito --prova --scala 0.01 --nome attaccato T1 T2

# vere: T1 e poi T2, una volta, stasera alle 21
sh prova_schermo.sh --alle 21:00 --nome attaccato "T1=rif:1800 ventola:2400 recupero:3600 carica:600 recupero:3600" "T2=rif:300 carica:300 misura:480 recupero:900 carica:300 misura:480"

# vere: T2 alle 21 e all'1 per 3 notti
sh prova_schermo.sh --alle 21:00,01:00 --giorni 3 --nome attaccato T2

# laboratorio: 3 notti alle 22, ordine alternato, seconda notte solo monitoraggio passivo per 5 ore
sh prova_schermo.sh --alle 22:00 --calendario "T2,T2L;-;T2L,T2" --monitora 300 --ip-schermo 192.168.10.20 --nome attaccato

# campo: notte tipo (00:30 T2, pausa 60 min, base stabile, T2L; fine verso le 04:20)
sh prova_schermo.sh --alle 00:30 --pausa 60 --base 30 --nome campo --ip-schermo 192.168.10.20 T2 T2L

# diagnostica di livello 0 (2 minuti, nessun comando): ping allo schermo, AUX FAULT, dati AUX fermi
IP_SCHERMO=192.168.10.20 sh prova_schermo.sh --diagnosi 2

sh prova_schermo.sh --verifica     # dopo le prove: VERIFICA=OK se non resta niente (processi, file di stato) e TempContEn = 1
sh prova_schermo.sh --stato        # piano e avvii (avvisa se ci sono piani doppi); ultima riga STATO_BREVE= per PoleDesk
sh prova_schermo.sh --processi     # piani in attesa, prove in corso, blocchi (anche di versioni vecchie)
sh prova_schermo.sh --pulisci      # chiude tutto, toglie i blocchi e ripristina: colonnina pulita
tail -f programmata.log            # segue il piano (Ctrl+C esce dalla lettura, il piano continua)
sh prova_schermo.sh --annulla      # niente più avvii (la prova in corso finisce)
sh prova_schermo.sh --ferma        # annulla il piano E ferma la prova in corso, con ripristino
```

- Un nuovo piano sostituisce quelli già presenti (anche doppioni o di versioni vecchie): non restano processi appesi.
- Il comando mostra subito l'elenco dei passi con i tempi e l'ora della colonnina: controllare che sia ora italiana.
- Un avvio in ritardo di oltre 30 min (`LATE_MAX_MIN`) perché la sequenza precedente è ancora in corso viene saltato e scritto in `programmata.log`.
- Nei risultati la configurazione diventa `T2-attaccato`; il riferimento (Q) è separato per sequenza e fascia oraria (`T2@21`). Con `--prova` tutto resta in `collaudo/` e non tocca i riferimenti veri.
- T1 non ha due coppie carica/misura: in `risultati.csv` risulta «non valida» per gli indici; i dati si analizzano in PoleDesk.
- Sicurezza senza installazione: ripristino a fine prova o su arresto, guardiano a tempo, soglie. Mancano systemd e il timer di guardia: dopo un riavvio a metà prova lanciare `sh prova_schermo.sh --ripristina`.

## Cosa registra

- Un CSV per prova, un campione ogni 5 s: temperature, LED, LUX, tensioni (12 V e **5 V**), stati, **AUX FAULT**, comandi, stato letto di riscaldatore e ventola, flag del display, flag della PSG4X, **ping al computer dello schermo: latenza (`ping_ms`) ed esito (`ping_ok` 1/0)**. Le nuove colonne sono in fondo: i CSV restano leggibili dalle versioni precedenti di PoleDesk.
- `risultati.csv`: una riga per prova con esito, regole scattate, Ī, C, deriva della misura 1 e della misura 2, S̄, calo a 60 s, Q, corrente LED massima, percentuali di flag e ping, validità. Un `risultati.csv` di una versione precedente (colonne diverse) viene rinominato `risultati_fino_DATA.csv`.
- `diagnosi.csv`: una riga per diagnostica (ping falliti, AUX FAULT, dati AUX fermi, esito).
- `baseline.csv`: il riferimento di ogni colonnina per fascia oraria (le prime prove valide).
- `controlli.csv`: il riepilogo delle sessioni di soli controlli.
- `guardia.log` e un `.log` per prova: comandi inviati e ripristini.

## Validità della prova

Se ne manca una la prova è **non valida** e va ripetuta la notte dopo:
- corrente LED < `LED_MAX` (1 A) per tutta la prova (di notte la barra è a circa 0,5 A; a 3 A scalda quanto la scaldiglia);
- colonnina libera: USER STATUS = `USER_OK` (IDLE) all'avvio e per tutta la prova (se cambia, la prova si interrompe con ripristino);
- base stabile prima dell'avvio (AUX entro ±0,3 °C per 30 min), altrimenti la prova non parte;
- |deriva| ≤ `DRIFT_MAX` (0,05 °C/min) negli ultimi 3 min del Riferimento;
- scaldiglia e ventola lette accese per la durata prevista (± `HEATER_TOL_S`);
- misure coerenti: C = |Ī1 − Ī2| / Ī ≤ `C_MAX` (0,35).

La deriva della seconda misura si calcola sugli ultimi 3 min del Recupero (con lo schermo attaccato la base non torna al livello iniziale e sta ancora salendo) e corregge Ī2.

## Esito sulla colonnina: regole in OR

L'esito definitivo (indicatori Ī, Ī/A, τ, R, rimbalzo, In, Q e soglie) lo calcola PoleDesk dai CSV; in una fase successiva le soglie passeranno nello script.

Sospetto distacco se scatta almeno una regola attiva:
- **Q**: Ī rispetto a com'era la stessa colonnina nella stessa fascia oraria (Q = Ī / mediana del riferimento) oltre la soglia;
- **display**: flag del display connesso in meno della metà dei campioni;
- **PSG4X**: flag della PSG4X connessa in meno della metà dei campioni;
- **ping**: il display non risponde in più della metà dei tentativi;
- **assoluto** (spenta di default): Ī oltre una soglia fissa.

Le regole su flag e ping valgono anche se la parte termica della prova non è valida.

## Sicurezza

Il controllo termico automatico (`TempContEn`) torna sempre a 1:
1. a fine prova e su arresto o errore, con verifica rileggendo il valore nel log di PS4GAux;
2. guardiano a tempo sulla colonnina, se lo script si blocca;
3. systemd: tempo massimo del servizio e controllo quando termina;
4. timer di guardia ogni 10 min e all'accensione;
5. interruzione immediata per soglie di temperatura, FAULT, ricarica, pagina muta.

Da verificare sulla colonnina vera: la risposta di `04;00;TempContEn` nel log (`Value=`), la presenza di `ping`, `wget`, `awk`, `tar` in BusyBox, e la scrittura in `/etc/systemd/system`.
