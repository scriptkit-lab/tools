# Prova schermo sulla colonnina — 2.0.1

Acquisizione dati per il rilevamento del distacco dello schermo, eseguita direttamente sulla colonnina, senza PC.

## File

- `generatore_prova_schermo.html`: si apre con il browser. Si scelgono passi, tempi, orari, controlli e regole; produce **un unico file** `installa_prova_schermo.sh` da dare al collega.
- `prova_schermo.sh`: lo script (è già dentro l'installatore; si può anche usare da solo, vedi sotto).
- `seq_T1.conf`, `seq_T2.conf`, `seq_collaudo.conf`: sequenze pronte (T1 caratterizzazione 3 h 20, T2 taratura 46 min, collaudo 5 min).

## Sul posto

1. Copiare `installa_prova_schermo.sh` sulla colonnina, ad esempio in `/home/root/`.
2. `sh /home/root/installa_prova_schermo.sh`: installa script, impostazioni, orari e guardia in `/home/root/test`.
3. Primo controllo, senza comandare nulla: `sh /home/root/test/prova_schermo.sh --controlli 5`. Nel file `…_pagina.txt` ci sono tutti i nomi della pagina, da cui leggere i nomi esatti dei flag di display e PSG4X; se sono diversi, correggerli nel generatore e reinstallare.
4. Raccolta: `sh /home/root/test/prova_schermo.sh --archivio` crea un `.tar.gz` con tutti i dati da prelevare.

Altri comandi: `--stato`, `--ferma`, `--ripristina`. Per togliere orari e guardia: `sh installa_prova_schermo.sh --rimuovi`.

## Senza installare nulla (one shot o piano ripetuto)

Copiare in `/home/root/test/` solo `prova_schermo.sh` e i file `seq_*.conf` che servono (più `prova_schermo.conf` se si vogliono impostazioni diverse dalle predefinite). Nessun timer, nessun file di sistema: il piano è un processo in attesa che sparisce a fine piano, con `--annulla` o al riavvio della colonnina.

| Cosa | Comando (`cd /home/root/test` prima) |
|---|---|
| Collaudo, subito (5 min) | `sh prova_schermo.sh --seq collaudo` |
| Una sequenza subito | `sh prova_schermo.sh --seq T2 attaccato` |
| Una sequenza, una volta, alle 21 | `sh prova_schermo.sh --alle 21:00 --nome attaccato T2` |
| T1 e poi T2 in fila, una volta | `sh prova_schermo.sh --alle 21:00 --nome attaccato T1 T2` |
| T2 alle 21 e all'1 per 3 notti | `sh prova_schermo.sh --alle 21:00,01:00 --giorni 3 --nome attaccato T2` |
| Prova tra N minuti | `sh prova_schermo.sh --tra 2 --nome attaccato T2` |
| Vedere il piano | `sh prova_schermo.sh --stato` |
| Annullare il piano | `sh prova_schermo.sh --annulla` (la prova in corso finisce; per fermarla `--ferma`) |

- Senza sequenze nel comando si usa quella del file `.conf` (o la Classica 31 min).
- Tra una sequenza e la successiva si aspetta la pausa minima (`REST_MIN`, 60 min) e, all'inizio di ognuna, la base stabile (30 min, al massimo 60).
- Un avvio in ritardo di oltre 30 min (`LATE_MAX_MIN`) perché la sequenza precedente è ancora in corso viene saltato e scritto in `programmata.log`.
- Gli orari sono quelli dell'orologio della colonnina: il comando li mostra (`Ora sulla colonnina`), verificare che sia ora italiana.
- La configurazione nei risultati diventa `T2-attaccato`; il riferimento (Q) è separato per sequenza e fascia oraria (`T2@21`).
- T1 non ha due coppie carica/misura: in `risultati.csv` risulta «non valida» per gli indici; i dati si analizzano in PoleDesk.
- Sicurezza senza installazione: restano il ripristino a fine prova o su arresto, il guardiano a tempo e le soglie (T1: 55 °C). Mancano systemd e il timer di guardia: dopo un riavvio a metà prova lanciare `sh prova_schermo.sh --ripristina`.

## Cosa registra

- Un CSV per prova, un campione ogni 5 s: temperature, LED, LUX, tensioni, stati, comandi, stato letto di riscaldatore e ventola, **flag del display, flag della PSG4X, ping al display**. Si importa in PoleDesk (Prove registrate → Importa).
- `risultati.csv`: una riga per prova con esito, regole scattate, Ī, C, deriva, S̄, calo a 60 s, Q, percentuali di flag e ping, validità.
- `baseline.csv`: il riferimento di ogni colonnina per fascia oraria (le prime prove valide).
- `controlli.csv`: il riepilogo delle sessioni di soli controlli.
- `guardia.log` e un `.log` per prova: comandi inviati e ripristini.

## Esito: regole in OR

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
