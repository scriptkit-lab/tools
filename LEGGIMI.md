# Prova schermo sulla colonnina — 2.0.1

Acquisizione dati per il rilevamento del distacco dello schermo, eseguita direttamente sulla colonnina, senza PC.

## File

- `generatore_prova_schermo.html`: si apre con il browser. Si scelgono passi, tempi, orari, controlli e regole; produce **un unico file** `installa_prova_schermo.sh` da dare al collega.
- `prova_schermo.sh`: lo script (è già dentro l'installatore; si può anche usare da solo, vedi sotto).
- `seq_T1.conf`, `seq_T2.conf`: esempi modificabili (T1 caratterizzazione 3 h 20, T2 taratura 46 min); le sequenze si possono anche scrivere direttamente nel comando.

## Sul posto

1. Copiare `installa_prova_schermo.sh` sulla colonnina, ad esempio in `/home/root/`.
2. `sh /home/root/installa_prova_schermo.sh`: installa script, impostazioni, orari e guardia in `/home/root/test`.
3. Primo controllo, senza comandare nulla: `sh /home/root/test/prova_schermo.sh --controlli 5`. Nel file `…_pagina.txt` ci sono tutti i nomi della pagina, da cui leggere i nomi esatti dei flag di display e PSG4X; se sono diversi, correggerli nel generatore e reinstallare.
4. Raccolta: `sh /home/root/test/prova_schermo.sh --archivio` crea un `.tar.gz` con tutti i dati da prelevare.

Altri comandi: `--stato`, `--ferma`, `--ripristina`. Per togliere orari e guardia: `sh installa_prova_schermo.sh --rimuovi`.

## Senza installare nulla: sequenze con i tempi scritti nel comando

Copiare in `/home/root/test/` solo `prova_schermo.sh` (e, se comodi, i file `seq_*.conf`). Nessun timer, nessun file di sistema: il piano è un processo in attesa che sparisce a fine piano, con `--annulla` o al riavvio della colonnina. Un solo piano alla volta.

**Sequenza** = `"NOME=ruolo:secondi ruolo:secondi ..."`, ruoli `rif` (tutto spento), `carica` (riscaldatore), `misura` (ventola dopo una carica), `ventola` (ventola), `recupero` (tutto spento). Più sequenze nel comando = eseguite in fila. Al posto della sequenza scritta si può dare il nome di un file `seq_NOME.conf` (es. `T1`, `T2`): i tempi stanno dentro e si modificano.

**Quando:** `--subito`, `--tra MINUTI`, `--alle HH:MM` (più orari: `--alle 21:00,01:00`).

**Opzioni:** `--giorni N` ripete ogni giorno (predefinito 1 = una volta) · `--nome attaccato|tolto` · `--note "testo"` · `--scala X` moltiplica tutti i tempi (0.1 = un decimo) · `--pausa MIN` tra sequenze (predefinito 60) · `--base MIN` di base stabile prima di ogni sequenza (predefinito 30, 0 = non aspetta) · `--prova` pausa e base a 0, nessun vincolo di accensione, dati in `collaudo/`.

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

sh prova_schermo.sh --stato        # piano e avvii
tail -f programmata.log            # segue il piano (Ctrl+C esce dalla lettura, il piano continua)
sh prova_schermo.sh --annulla      # niente più avvii (la prova in corso finisce)
sh prova_schermo.sh --ferma        # ferma la prova in corso, con ripristino
```

- Il comando mostra subito l'elenco dei passi con i tempi e l'ora della colonnina: controllare che sia ora italiana.
- Un avvio in ritardo di oltre 30 min (`LATE_MAX_MIN`) perché la sequenza precedente è ancora in corso viene saltato e scritto in `programmata.log`.
- Nei risultati la configurazione diventa `T2-attaccato`; il riferimento (Q) è separato per sequenza e fascia oraria (`T2@21`). Con `--prova` tutto resta in `collaudo/` e non tocca i riferimenti veri.
- T1 non ha due coppie carica/misura: in `risultati.csv` risulta «non valida» per gli indici; i dati si analizzano in PoleDesk.
- Sicurezza senza installazione: ripristino a fine prova o su arresto, guardiano a tempo, soglie. Mancano systemd e il timer di guardia: dopo un riavvio a metà prova lanciare `sh prova_schermo.sh --ripristina`.

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
