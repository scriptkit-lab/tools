# PoleDesk 1.16.2

Strumento locale per lavorare in SSH su una o più colonnine: comandi in parallelo, file (lettura, modifica, creazione, scrittura su tutta la flotta), log, procedure guidate, monitor di variabili con grafici, registro di tutte le operazioni.

## Avvio su Windows

1. Installa Python 3.11 o 3.12 da python.org (spunta "Add python.exe to PATH"; senza permessi di amministratore scegli "Customize installation" e togli "Install for all users"). Il comando "python" già presente in Windows è solo un collegamento al Microsoft Store e non basta.
2. Doppio clic su `avvia.bat`. La prima volta crea l'ambiente e installa `paramiko` (serve internet o il proxy aziendale).
3. Si apre il browser su http://127.0.0.1:8765. Il server ascolta solo sul PC, non è raggiungibile da altri.

Il PC deve raggiungere l'IP delle colonnine (verifica con `ping <IP>` dal Prompt dei comandi).

## Eseguibile senza installazione (PoleDesk.exe)

Per usare PoleDesk su un PC dove non si può installare Python (es. PC aziendale senza permessi):

1. Su un PC Windows **con** Python e internet, nella cartella `poledesk`, fai doppio clic su `crea_exe.bat`. In circa un minuto crea `dist\PoleDesk.exe`. Non modifica il Python del PC: lavora in un ambiente temporaneo che poi cancella.
2. Copia `PoleDesk.exe` sul PC dove ti serve (chiavetta, cartella condivisa) e avvialo con doppio clic. Si apre una finestra nera da lasciare aperta e il browser con PoleDesk.
3. I dati stanno in `%USERPROFILE%\PoleDesk\data`, come con `avvia.bat`: le due modalità condividono colonnine, impostazioni e prove.

A ogni nuova versione di PoleDesk va rifatto l'eseguibile con `crea_exe.bat`. L'eseguibile non è firmato: se l'antivirus aziendale lo blocca, va chiesta l'autorizzazione a IT oppure si usa `avvia.bat`.

## Aggiornare a una nuova versione

Con PoleDesk.exe creato dalla 1.16.2 in poi: copia `server.py` e `console.html` della nuova versione nella stessa cartella di PoleDesk.exe e riavvialo. Non serve ricreare l'eseguibile né installare nulla.

Scompatta la nuova versione in una cartella qualsiasi e avvia `avvia.bat`. Ambiente Python e dati stanno in `%USERPROFILE%\PoleDesk` e vengono riusati: niente reinstallazioni e niente copie. Le vecchie cartelle del programma si possono cancellare.

Passando dalla 1.2.0 o precedenti: copia una volta la cartella `data` della vecchia versione dentro la nuova cartella `poledesk`, prima del primo avvio. `avvia.bat` la trasferisce da solo nella cartella condivisa.

## Anagrafica

Aggiungi le colonnine a mano o importa un CSV (separatore `;` o `,`):

```
name;host;port;username;password;key_path;tags;display_ip
WP21-Lab;10.20.30.40;22;root;;C:\Users\nome\.ssh\id_ed25519;lab|roma;192.168.10.20
```

Ogni colonna che non è tra quelle standard diventa una variabile della colonnina, usabile nei comandi come `{{display_ip}}`. Utente, password e chiave lasciati vuoti prendono i valori predefiniti dalle Impostazioni.

## Schede

- **Comandi**: esegue il comando su tutte le selezionate in parallelo. Azioni pronte (ping schermo, pagina web interna, temperature, vicini di rete…) e possibilità di salvarne di nuove. I comandi che modificano la colonnina chiedono conferma. Usa comandi che terminano da soli: `ping -c 3`, `tail -n 100`, `top -b -n 1`.
- **Console AUX**: costruisci un comando per la scheda AUX dall'interfaccia (leggi o scrivi un parametro, imposta le uscite con gli interruttori, oppure riga libera), lo invii e vedi la risposta nel log. Nella stessa schermata la pagina web della colonnina viene letta in diretta: tutte le variabili, quelle cambiate evidenziate, la variazione rispetto all'ultimo comando e, per le variabili con la stella, un riquadro con l'andamento e il segno dei comandi inviati. La prima volta imposta l'indirizzo della pagina in "Fonte dati" e usa "Prova lettura" per verificare cosa viene riconosciuto.
- **Sequenze**: serie di comandi a tempo. Ogni passo è un comando AUX, un comando shell o una semplice attesa, con il tempo da aspettare prima del passo successivo. I passi Iniziali girano una volta, il Ciclo si ripete N volte (0 = finché non lo fermi), i Finali girano sempre, anche se fermi la sequenza o c'è un errore: mettici lo spegnimento delle uscite e la riattivazione del controllo termico. Si avvia su tutte le colonnine selezionate in parallelo; con ↗ apri la Console AUX della colonnina per vederne le variabili in diretta.
- **Sequenze, opzioni dei passi**: per un comando AUX puoi chiedere di verificare l'attuazione sulla pagina web (variabile = valore atteso entro N s). Per ogni passo scegli quando avanzare: dopo un tempo, alla pressione di Avanti, oppure quando una variabile della pagina (es. la temperatura della testa) resta entro ± tolleranza per tutta la finestra indicata, con un tempo massimo. "Leggi le variabili della pagina web" riempie l'elenco dei nomi. Se la connessione cade la sequenza aspetta e rifà lo stesso passo; se PoleDesk viene chiuso, alla riapertura trovi l'esecuzione interrotta con "Riprendi".
- **Grafico e sessione** (Console AUX): aggiungi al grafico le variabili numeriche che ti interessano e, per ognuna, la derivata. Tutto quello che succede durante la diretta, campioni e attuazioni da qualunque scheda, viene salvato in `data\series\sessione-*.csv`; "Esporta sessione CSV" scarica la stessa sessione in forma di tabella per Excel. Comandi SSH liberi: dalla scheda Comandi su più colonnine insieme, oppure dalla modalità Shell del costruttore nella Console AUX.
- **Profilo termico (specifica v3)**: prova per il rilevamento del distacco dello schermo con l'indice I. Prima volta: in "Impostazioni del profilo" premi "Leggi variabili" e associa ogni grandezza; sono obbligatori lo stato letto del riscaldatore e della ventola (servono a verificare i 180 s e l'istante t_on) e le soglie di sicurezza. Due tipi di prova: **Taratura** (schermo verificato attaccato, almeno 3 per colonnina: PoleDesk calcola I₀ e S₀ come mediane e mostra la coerenza con le tarature precedenti, senza esito di rilevamento) e **Rilevamento** (Q = Ī / I₀ per l'indice, P = S̄ / S₀ per la pendenza in carica S; esito con solo I, solo S o I+S secondo le impostazioni). Con "Confronta con il riferimento di" una colonnina senza tarature proprie (es. la gemella senza schermo) usa I₀ e S₀ di un'altra. All'aperto indica sempre la temperatura esterna. In Prove registrate ogni riga si può aprire nei grafici, forzare valida (decisione dell'operatore, resta segnata) o cancellare (va in `data\profiles\cestino`). I riquadri si chiudono cliccando sul titolo.
- **Prova in coppia** (consigliata all'aperto): seleziona due colonnine, spunta "Prova in coppia" e indica la sentinella con schermo attaccato. Partono insieme e la prova è confrontata con la sentinella dello stesso momento (Q coppia, P coppia), così vento, sole e temperatura si annullano.
- **Rete di sicurezza**: durante ogni prova sulla colonnina resta attivo un comando a tempo che, se PoleDesk non chiude la prova, spegne le uscite e riattiva il controllo termico. A fine prova PoleDesk rilegge TempContEn per verificarlo.
- **Prove fatte sulla colonnina**: con "Importa" in Prove registrate i CSV di prova_schermo.sh vengono valutati con lo stesso calcolo. "Ricalcola tutte" rielabora le prove dopo aver cambiato soglie o parametri.
- **Analisi**: scegli due gruppi (es. schermo attaccato e tolto, nella stessa campagna) e vedi quale indicatore li separa meglio e con quale soglia. "Stampa report" produce il riepilogo della campagna.
- **Quando fare le prove**: al buio (almeno 2 ore dopo il tramonto), con la colonnina accesa da almeno 3 ore e ferma nello stesso posto, a più di 60 minuti dalla prova precedente. Le tarature: 3, in 3 notti diverse. Di giorno solo prove in coppia. PoleDesk controlla queste condizioni e rende non valide le prove con la base in deriva.
- **Metodo assoluto**: giudica la singola prova senza riferimento con F (quanto il sensore si avvicina alla temperatura dell'aria in 2 minuti di ventola) e τ (costante di tempo della discesa), con regole per modello in Impostazioni del profilo. Serve la temperatura esterna di ogni prova (scritta o dal meteo). Per le prove già fatte premi "Ricalcola tutte".
- **Conclusioni**: riassunto della campagna con i valori emersi (mediane per colonnina, schermo e luogo), le frasi calcolate dai dati, le tue conclusioni e la spiegazione di ogni parametro; si stampa o si salva in PDF.
- **Grafici prove**: sovrappone fino a 8 prove registrate (AUX, TEMPERATURE, CPU, derivata di AUX o Δ) con le fasi, allineate all'inizio della sequenza o alla ventola della misura 1. "Stato per colonnina" mostra la decisione nel tempo (ALLARME, misura impossibile, ripetere). File in `data\profiles`: un CSV per prova, un JSON con parametri e risultati, `riepilogo_v3.csv` e `riferimenti.json`.
- **Sequenze in tempo reale**: con "Tempi: reali, allineati all'orologio" i passi seguono l'orologio dall'avvio. Più colonnine avviate insieme restano in passo: se una perde la connessione, al rientro salta i passi già scaduti e riprende insieme alle altre. Con "dal passo precedente" (comportamento di prima) ogni attesa conta dalla fine del passo.
- **File**: naviga le cartelle, apre e modifica file di testo, crea file e cartelle. Al salvataggio fa una copia `.bak-data` del file originale. "Scrivi sulle selezionate" copia lo stesso file su tutta la flotta.
- **Log**: legge le ultime righe di un file di log, con filtro e aggiornamento continuo.
- **Procedure**: sequenze guidate. Per ogni passo PoleDesk scrive il file comando sulla colonnina, legge le righe nuove del log fino alla conferma attesa e, se serve, chiede all'operatore una verifica visiva (Sì, OK / No, KO). Se un passo fallisce o la procedura viene interrotta, esegue i passi di messa in sicurezza. Ogni esecuzione produce un rapporto in `data\reports`.
- **Monitor**: le sonde sono comandi che restituiscono un numero (es. temperatura della testa letta dalla pagina web interna con `curl` più un'espressione regolare) oppure un esito (es. ping dello schermo: 1 raggiungibile, 0 no). I campioni vanno nei grafici e in `data\series`.
- **Registro**: ogni comando e ogni scrittura, con colonnina, esito e ora.

## Collaudo fan e heater

La procedura è già precompilata con i sette file `command` della cartella "Collaudo PS4GAUX" (da 1. disable thermal control a 7. check thermal control). Se Ducati Energia li aggiorna, si ricaricano da "Modifica procedura" con "Carica file comando". Verifica percorso del log e del file comando (predefiniti `/crve/log/PS4GAux.log` e `/crve/command`). Le credenziali della colonnina si impostano nell'anagrafica, non nella procedura.

## Da adattare alla JuicePole 2.1

Le azioni e le sonde iniziali sono generiche: vanno aggiornati percorsi e URL reali (pagina web interna, file di configurazione, log dello schermo, variabile NTC della testa). Si modificano dall'interfaccia e restano nella cartella dei dati.

## Collegamento tramite jump host

Se le colonnine si raggiungono solo passando da un server ponte: Impostazioni → **Gestisci jump host…**, inserisci nome, indirizzo, utente e password o chiave, poi **Prova** (anche verso l'IP di una colonnina, es. `192.168.0.111:22`). Scegli il **jump host predefinito** nelle Impostazioni oppure, per ogni colonnina, nel campo **Collegamento** della sua scheda (predefinito, diretto o un jump host specifico). Un jump host può a sua volta essere raggiunto tramite un altro. La pagina web viene letta attraverso il jump host; se è HTTPS, in Console AUX → Fonte dati scegli la lettura dalla colonnina via SSH.

## Colonnine con lo stesso IP

Se tutte le colonnine hanno lo stesso indirizzo e sposti il cavo dall'una all'altra, premi **Riconnetti** nell'intestazione dopo ogni cambio. PoleDesk legge alla connessione il MAC della colonnina collegata (visibile nell'elenco accanto all'IP) e lo riporta nei rapporti, così sai sempre su quale macchina hai lavorato. Se Windows continua a non raggiungere la colonnina appena collegata, da un Prompt dei comandi come amministratore `arp -d *` svuota la cache degli indirizzi, con lo stesso effetto di staccare e riattaccare il cavo.

### Due colonnine collegate insieme (porta del PC e adattatore USB)

1. Dai a ciascuna scheda di rete del PC un IP fisso diverso nella stessa rete delle colonnine, per esempio 192.168.0.100 alla porta del PC e 192.168.0.101 all'adattatore USB, subnet 255.255.255.0, senza gateway. Si fa da **Rete PC** nell'intestazione (serve la conferma da amministratore di Windows); non toccare la scheda segnata "rete aziendale".
2. In PoleDesk crea una colonnina per cavo, entrambe con IP 192.168.0.111, e in "Scheda di rete del PC" metti 192.168.0.100 per una e 192.168.0.101 per l'altra.
3. Controlla nell'elenco che le due righe mostrino MAC diversi: è la conferma che stai parlando con due colonnine distinte.

## Sicurezza

- Le password salvate restano in chiaro nella cartella dei dati: per la flotta usa le chiavi SSH.
- Senza `known_hosts` nella cartella dei dati le chiavi host vengono accettate senza verifica. Per l'uso in produzione copia lì le chiavi delle colonnine (formato OpenSSH): da quel momento le colonnine sconosciute vengono rifiutate.
- Il parallelismo predefinito è 20 colonnine alla volta; si cambia in Impostazioni (vale dal riavvio).
- Avviando `server.py` senza `avvia.bat` i dati restano nella cartella `data` accanto al programma.
