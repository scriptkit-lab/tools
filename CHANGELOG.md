# Versioni di PoleDesk

## 1.16.2 (10 novembre 2026)
- Aggiornamento senza ricreare l'eseguibile: se accanto a PoleDesk.exe ci sono server.py e console.html di una versione più recente, PoleDesk.exe li usa con le librerie che contiene già. Vale da questa versione in poi: l'eseguibile va creato un'ultima volta con crea_exe.bat.

## 1.16.1 (10 novembre 2026)
- Jump host tramite PAM (es. One Identity Safeguard): la "stringa di connessione nome host" (vaultaddress=…@token=…@account@dominio@destinazione@indirizzo) si incolla intera nel campo Indirizzo; PoleDesk separa utente (tutto prima dell'ultima @) e indirizzo (dopo). Prima veniva usata intera come nome host ("'idna' codec can't encode… label too long").
- Autenticazione con il token nel nome utente: metodo "none" o keyboard-interactive senza risposte, senza password né chiave. Se il PAM rifiuta, il messaggio suggerisce di chiedere una nuova stringa.
- Collegamento verso le colonnine: se il jump host non permette l'inoltro di porte (direct-tcpip), PoleDesk apre il collegamento con nc (o /dev/tcp di bash) sul jump host. Scelta nel jump host: automatico, solo inoltro, solo nc.

## 1.16.0 (10 novembre 2026)
Tempi e condizioni, dalle prove del 5/11 (al chiuso) e del 9/11 (all'aperto da pranzo): di giorno Ī della stessa colonnina cambia del 50–60% e S̄ fino a 4 volte, mentre il rapporto tra due colonnine misurate insieme resta quasi fermo; diverse prove partivano con la base in salita; 5 minuti di recupero non bastavano.
- **Sequenza consigliata, 31 min**: riferimento 300 s, carica 180 s, misura 300 s, recupero 600 s, carica 180 s, misura 300 s. Il riferimento iniziale è obbligatorio (almeno 240 s, impostabile).
- **Deriva della base**: pendenza di AUX negli ultimi 3 minuti del riferimento. Oltre 0,03 °C/min la prova non è valida (colonnina non in equilibrio); sotto, Ī, S̄, calo a 60 s, F e τ vengono corretti sottraendo la deriva (disattivabile). Colonna deriva.
- **Solo al buio**: finestra oraria (predefinita 19:00–06:00) e, se impostato, AUX LUX massimo. Fuori finestra le prove singole aspettano; tarature e prove singole fatte di giorno risultano non valide; le prove in coppia partono e valgono anche di giorno. Il metodo assoluto si applica solo alle prove al buio. Colonna notte.
- **Pausa minima** tra due prove sulla stessa colonnina (60 min) tra le condizioni di partenza.
- Valori predefiniti: colonnina accesa da almeno 3 h, base stabile 30 min entro 0,3 °C. Le impostazioni salvate vengono portate a questi valori una volta (avviso in Impostazioni del profilo; si confermano con Salva).
- Simbolo ☾/☀ nelle prove registrate, deriva e condizione nel risultato, gruppo "al buio" in Analisi, prove separate per buio e giorno nelle Conclusioni.
- Corretto lo stile dell'esito "incerto" nelle tabelle.
- prova_schermo.sh: stessa sequenza da 31 minuti.

## 1.15.0 (10 novembre 2026)
Metodo assoluto (senza riferimento)
- Nuovi indicatori della singola prova, con la temperatura dell'aria come punto di arrivo (scritta nella prova o dal meteo):
  - **F** = frazione della distanza dall'aria recuperata nei primi 120 s di ventola (0 = fermo, 1 = arrivato all'aria);
  - **τ** = costante di tempo da un adattamento esponenziale con punto di arrivo libero (T∞), con R² della curva; sostituisce il τ della 1.13, che usava come arrivo la temperatura di partenza e non si calcolava quando AUX scendeva sotto di essa;
  - **T∞**, **AUX − T aria a riposo**, **k rispetto all'aria**.
- **Regole per modello** (Impostazioni del profilo → Metodo assoluto): il modello si riconosce da un testo contenuto nel CUID; sospetto se almeno N condizioni sono vere (N = 1: OR), attaccato se almeno M condizioni di attaccato sono vere (0 = tutte: AND), altrimenti incerto. Regole iniziali provvisorie per T3QQ (096, 097) e T5PQ (004): sospetto se F ≥ 0,6 oppure τ ≤ 60 s; attaccato se F ≤ 0,35 e τ ≥ 150 s.
- Esito finale: nuove scelte "metodo assoluto" e "assoluto O relativo" (sospetto se uno dei due è sospetto). La predefinita resta "solo I": l'esito del metodo assoluto viene calcolato e registrato comunque (colonne esito_abs e regole_abs).
- Pulsante "Sequenza assoluta (misure 5 min)": stessa sequenza con misure di 300 s, per stimare bene τ e T∞ con lo schermo attaccato.
- Nel risultato della prova un riquadro "Metodo assoluto" con F, τ, T∞, AUX − T aria, k aria e le condizioni verificate.
- Grafici prove: linea della temperatura dell'aria e curva esponenziale adattata sulle misure; F e τ nella tabella.
- Analisi: gruppi per modello e nuovi indicatori (F, k aria, AUX − T aria, T∞). Conclusioni: colonne F, τ, AUX − aria, frase su come si comportano le regole attuali e spiegazione dei nuovi parametri.
- Riepilogo: colonne modello, T_aria, F1, F2, F_medio, Tinf1, Tinf2, Tinf_medio, fit_r2, k_aria_medio, dT_riposo, esito_abs, regole_abs. Con "Ricalcola tutte" si ottengono anche per le prove già fatte (serve la temperatura esterna nella prova).
- Senza passo di riferimento, la temperatura di partenza è presa dai primi secondi della carica.

## 1.14.1 (10 novembre 2026)
- Corretto: Grafici prove vuoti e "Ricalcola tutte" che rendeva non valide le prove. I CSV delle prove contengono i valori con l'unità letta dalla pagina (es. "30,49 C") e la lettura dai file non li riconosceva come numeri.

## 1.14.0 (9 novembre 2026)
- Nuova scheda **Conclusioni**, per campagna o su tutte le prove:
  - numeri della campagna (prove, valide, non valide, interrotte, colonnine, luoghi, tarature, prove in coppia, periodo);
  - **Cosa emerge dai dati**: frasi calcolate dal riepilogo, cioè motivi più frequenti di non validità; per ogni luogo, schermo staccato contro attaccato con l'indicatore che separa meglio e la sua soglia; Q e Q coppia rispetto alle soglie attuali, con l'intervallo in cui dovrebbe stare la soglia "sospetto"; effetto del luogo su Ī con schermo attaccato; verso di S nei diversi luoghi; ripetibilità di Ī e coerenza C; colonnine in ALLARME o con misura impossibile;
  - tabella dei valori mediani per colonnina, schermo e luogo (Ī, C, Q, Q coppia, S̄, P, P coppia, k, τ, temperatura esterna, esiti);
  - riferimenti I₀ e S₀ e decisione attuale per colonnina;
  - **Conclusioni dell'operatore**, testo libero salvato con la campagna;
  - **Che cosa significano i parametri**: come funziona la prova e significato di ogni grandezza, con le soglie impostate;
  - Stampa / PDF.

## 1.13.0 (9 novembre 2026)
Metodo di misura
- **Prova in coppia**: due colonnine selezionate, una indicata come sentinella (schermo sicuramente attaccato). Partono nello stesso istante, quando entrambe hanno le condizioni di partenza (attesa massima impostabile). La prova viene confrontata con la sentinella misurata in quel momento: Q coppia = Ī / Ī sentinella, P coppia = S̄ / S̄ sentinella, con le stesse soglie B e P e la stessa combinazione; l'esito della prova è quello della coppia. Colonne del riepilogo: coppia, ruolo_coppia, file_coppia, Q_coppia, P_coppia, esito_coppia.
- **Indici normalizzati** registrati accanto a I e S (non entrano nella decisione): T di partenza (AUX medio negli ultimi 60 s del riferimento), k = Ī / (T_AUX,on − T di partenza) in 1/min, τ = costante di tempo del raffreddamento dalla regressione di ln(AUX − T di partenza). Colonne T_base, k1, k2, k_medio, tau1, tau2, tau_medio.
- Nuova scheda **Analisi**: due gruppi di prove definiti da tipo, schermo, luogo, CUID, ruolo in coppia e testo, filtrati per campagna; per ogni indicatore (Ī, Q, Q coppia, k, τ, I_n, S̄, P, P coppia, Δ, R, calo a 60 s, T_AUX,on, T di partenza) medie, deviazioni, separazione d′, soglia migliore con accuratezza e i valori dei due gruppi. Prove escludibili con un clic. Report stampabile.
- **Ricalcola tutte**: rielabora ogni prova dai suoi campioni con i parametri attuali (copia di riepilogo e riferimenti prima). Le tarature restano nel riferimento solo se c'erano già e sono ancora valide o forzate; gli azzeramenti sono rispettati (colonna fuori_rif).

Affidabilità e sicurezza
- **Rete di sicurezza sulla colonnina**: a inizio prova PoleDesk lancia sulla colonnina un comando a tempo (durata della sequenza + 15 min, impostabile) che spegne riscaldatore e ventola e riattiva il controllo termico se la prova non viene chiusa (PC spento, rete persa). A chiusura normale viene annullato.
- **Chiusura verificata**: dopo la riattivazione PoleDesk rilegge TempContEn (04;00;TempContEn) e, se non è 1, riprova; fino a tre tentativi anche per errori di connessione o autenticazione. Se non riesce resta l'avviso "Ripristina ora". Anche il ripristino manuale verifica TempContEn.
- Registro della prova consultabile sotto ogni prova (comandi, rete di sicurezza, verifica).
- Corretto un errore che poteva interrompere una prova quando due colonnine scrivevano lo stesso file nello stesso istante (prove in parallelo).
- **Importa** i CSV di prova_schermo.sh (o di PoleDesk): valutati con lo stesso calcolo delle prove dal PC, come taratura o rilevamento, con campagna e condizioni.

Uso quotidiano
- **Modifica dei dati** di una prova registrata: schermo, luogo, temperatura esterna, LED, note, campagna (le modifiche restano nel JSON della prova).
- **Condizioni**: AUX LUX medio nel riepilogo, con avviso "probabile sole diretto" sopra una soglia impostabile; meteo da Open-Meteo (temperatura, vento, nuvole, irraggiamento) a ogni prova se impostate le coordinate del sito, temperatura usata se non scritta; pulsante ☁ accanto alla temperatura esterna.
- **Campagne**: campo Campagna nella nuova prova, nell'importazione e nella modifica; obiettivo della campagna nella scheda Analisi; filtro per campagna in Grafici prove e Analisi.
- Prove registrate: azioni raccolte nel menu ⋯ (modifica, grafici con la prova in coppia, CSV, forza valida, cancella).

## 1.12.0 (9 novembre 2026)
- Nuova scheda **Grafici prove**: elenco delle prove registrate con filtri (colonnina, tipo, testo) e fino a 8 prove sovrapposte. Grafico di AUX, TEMPERATURE e CPU con le fasi della sequenza; sotto, derivata di AUX (°C/min, finestra regolabile) oppure Δ = TEMPERATURE − AUX. Asse del tempo dall'inizio della sequenza, dalla ventola della misura 1 (t_on) o come ora del giorno. Valori al passaggio del mouse, tabella con Ī, Q, S̄, P ed esito, esportazione PNG.
- Prove registrate (Profilo termico): azioni per riga per aprire il grafico, scaricare il CSV, **forzare valida** e **cancellare**.
  - Cancella: la prova esce dal riepilogo; se era una taratura esce dal riferimento e I₀ e S₀ vengono ricalcolati. CSV e JSON vanno in `data\profiles\cestino`.
  - Forza valida: solo se l'indice è stato calcolato su entrambe le misure. La prova viene ricalcolata con i parametri attuali (una taratura entra in I₀ e S₀), resta segnata "forzata" con i motivi originali (colonna `forzata` del riepilogo e JSON della prova).
- Profilo termico più ordinato: ogni riquadro e ogni gruppo di impostazioni si chiude e si riapre cliccando sul titolo (lo stato resta memorizzato); le Impostazioni del profilo partono chiuse. Tabella Prove registrate più compatta (esito I/S e C nel suggerimento e nel CSV).

## 1.11.0 (9 novembre 2026)
- Profilo termico: due tipi di prova al posto della casella "Prova di riferimento":
  - **Taratura (riferimento)**: schermo forzato "attaccato", nessun esito di rilevamento. Mostra Ī e S̄, il nuovo I₀ e S₀ con il conteggio n/3 e la coerenza con le tarature precedenti (Ī / I₀ precedente, S̄ / S₀ precedente; avviso se fuori 1/1,3–1,3).
  - **Rilevamento**: esito rispetto al riferimento della colonnina o, con "Confronta con il riferimento di", di un'altra colonnina (es. gemella senza schermo confrontata con I₀ e S₀ della 004).
- Secondo indicatore S = pendenza di AUX durante la carica (S₁, S₂, S̄), S₀ = mediana delle tarature, P = S̄ / S₀. Esito S: attaccato se 0,7 ≤ P ≤ 1,4, sospetto se P ≤ 0,4 o P ≥ 2,0, altrimenti incerto (parametri). Senza S₀ non si usa.
- Esito finale a scelta: solo I (predefinito), solo S, I+S (punteggio −1/0/+1 sommato: ≥ 1 sospetto, ≤ −1 attaccato, 0 incerto).
- Q e P in evidenza nei risultati, nell'elenco delle prove e in Prove registrate.
- riepilogo_v3.csv: nuove colonne tipo, rif_cuid, esito_I, S1, S2, S_medio, S0, P, esito_S, combinazione, n_rif. Il file esistente viene convertito alla prima apertura (copia di sicurezza riepilogo_v3_prima_di_1.11_*.csv); S1 e S2 sono ripresi dai JSON delle prove. Nelle righe di taratura I0/S0 sono i riferimenti precedenti e Q/P la coerenza con essi.
- riferimenti.json: ogni taratura registra anche S (e il file); per le tarature già fatte S viene ripreso dal riepilogo. "Stato per colonnina" mostra I₀, S₀ e tarature n/3.
- Stato letto predefinito: AUX HEATER POWER e AUX FAN POWER (se il campo era vuoto).
- Avviso all'avvio se manca la temperatura esterna.

## 1.10.1 (5 novembre 2026)
- Profilo termico: riquadro "Stato per colonnina" spostato in cima alla colonna di destra, con pulsante "Azzera riferimento" ben visibile.
- Nel risultato di una prova classificata con il riferimento (metodo B) c'è il pulsante "Azzera riferimento di questa colonnina".
- Pulsante "Togli le concluse" per liberare l'elenco delle prove; le prove restano in Prove registrate e nei file.

## 1.10.0 (5 novembre 2026)
- Jump host (come ssh -J): anagrafica dei server ponte con nome, indirizzo, porta, utente, password o chiave, scheda di rete del PC e catena opzionale (un jump host raggiunto tramite un altro). Pulsante Prova, anche verso un IP:porta di destinazione.
- Jump host predefinito nelle Impostazioni; per ogni colonnina: predefinito, diretto o un jump host specifico. Colonna "jump" in import ed export CSV (nome del jump host o "diretto").
- Tutto passa dal jump host: SSH, file comando, log, procedure, sequenze, profilo termico e lettura della pagina web (tunnel HTTP; per pagine HTTPS usare la lettura via SSH).
- Messaggi d'errore che indicano dove si interrompe il percorso (jump host non raggiungibile o jump host che non raggiunge la colonnina).

## 1.9.2 (4 novembre 2026)
- Nuovo script crea_exe.bat: crea dist\PoleDesk.exe, un unico eseguibile con Python, librerie e PoleDesk, da usare sui PC senza installare nulla.
- In modalità eseguibile i dati vanno in %USERPROFILE%\PoleDesk\data, la stessa cartella usata da avvia.bat.

## 1.9.1 (4 novembre 2026)
- Corretto l'avvio con Python 3.10 e 3.11: due messaggi usavano una sintassi accettata solo da Python 3.12 ("SyntaxError: f-string: unmatched '('").

## 1.9.0 (4 novembre 2026)
- Profilo termico aggiornato alla specifica v3 ("Rilevamento distacco schermo EVSE: specifica per il tool, v3"), che sostituisce la v2:
  - sequenza di 21 min con durate in secondi: riferimento 300 s, carica 180 s, misura 120 s, recupero 300 s, carica 180 s, misura 120 s; spegnimento del riscaldatore e accensione della ventola con un solo comando;
  - validità separata dall'interruzione: LED ±10%, USER STATUS = IDLE, AUX FAULT = 0, ANTITAMPER chiuso, durata del riscaldatore 180 ± 10 s e pausa riscaldatore→ventola ≤ 10 s verificate sullo stato letto, buchi > 15 s nei primi 60 s di ventola, campionamento ≤ 6 s, scarto dei campioni con OK = 0 o AUX = 0; interruzione immediata per soglie di sicurezza, ricarica, FAULT, comunicazione persa oltre 30 s;
  - indice di distacco: derivata per regressione su ±15 s, I = −min tra t_on e t_on + 45 s (t_on letto sulla pagina), Ī, coerenza C con esito "incerto" oltre 0,30; registrati anche calo a 60 s, Δ, pendenza in carica, R, T_AUX all'accensione e, se impostato ΔT_ref, l'indice compensato (ipotesi);
  - classificazione A con soglie assolute o B con riferimento I₀ per CUID (mediana di almeno 3 prove di riferimento valide);
  - decisione nel tempo per CUID: ALLARME con 2 "sospetto" sulle ultime 3 prove valide, "misura impossibile" con 5 incerte o non valide di fila;
  - riepilogo_v3.csv con le colonne della specifica; prove notturne una per notte se è impostato l'orario di partenza.
- Le impostazioni della v2 vengono convertite: si tengono variabili e soglie, si adotta la sequenza v3.

## 1.8.0 (3 novembre 2026)
- Sequenze in tempo reale: opzione "Tempi reali, allineati all'orologio". I passi sono agganciati all'istante di avvio, comune a tutte le colonnine avviate insieme; una colonnina che rientra dopo una perdita di connessione salta i passi già scaduti (segnati "saltato") e si riallinea alle altre. Sulle esecuzioni compare "in orario" o "in ritardo X s".
- Profilo termico: PoleDesk annota su disco quando disabilita il controllo termico; se si chiude a metà prova, alla riapertura avvisa e propone "Ripristina ora" (riscaldatore e ventola spenti, controllo termico riattivato). L'avviso resta finché il ripristino non è confermato.

## 1.7.2 (3 novembre 2026)
- Lettura della pagina web: tabelle con più coppie nome | valore sulla stessa riga (es. | CUID | N.21… | ACTIVE FLAG | ACTIVE |) vengono separate correttamente.
- I nomi attesi del profilo correggono i valori anche senza i due punti tra nome e valore; il CUID viene preso senza testo aggiuntivo.

## 1.7.1 (3 novembre 2026)
- Lettura della pagina web: più coppie "NOME: valore" sulla stessa riga vengono separate (es. "AUX TEMPERATURE: 30.49 C AUX LEDS CURRENT: 0.10 A"); i nomi impostati nel profilo termico fanno da guida per estrarre i valori giusti.
- Profilo termico: Δ = TEMPERATURE − AUX calcolato sui valori numerici.

## 1.7.0 (30 ottobre 2026)
- Nuova scheda Profilo termico, dalla specifica "Rilevamento distacco schermo EVSE: profilo di prova termico":
  - condizioni di partenza (accesa da N ore, AUX TEMPERATURE e TEMPERATURE stabili, corrente LED stabile, nessuna ricarica, comunicazione e latenza, nessun allarme), con attesa fino a un tempo massimo e orario "non prima delle" per le prove notturne;
  - sei fasi modificabili (durata, riscaldatore, ventola, ruolo per i calcoli), comandi verificati dal log e, se configurati, dagli stati sulla pagina;
  - condizioni di interruzione: soglie di sicurezza (obbligatorie), tensioni, USER STATUS, AUX FAULT, ANTITAMPER, ricarica, corrente LED fuori tolleranza, comunicazione persa, cambio di CUID;
  - chiusura sempre con riscaldatore e ventola spenti e controllo termico riattivato;
  - un CSV per prova per CUID (CUID_config_ripetizione_aaaammgg_hhmm.csv) con tutte le colonne del §5, più un JSON con informazioni, parametri e risultati;
  - grandezze del §6 calcolate a fine prova (Δ, pendenze, R, salita totale, stabilizzazione, rapporto AUX/TEMPERATURE, costante di tempo con adattamento esponenziale, calo di tensione) e riepilogo.csv di tutte le prove;
  - ripetizioni di fila e più colonnine in parallelo.

## 1.6.0 (30 ottobre 2026)
- Rete PC: elenco delle schede di rete (stato, MAC, IP, scheda della rete aziendale) e impostazione di IP statico o DHCP dall'interfaccia, con conferma da amministratore di Windows e controlli contro IP duplicati o uguali a quelli delle colonnine.
- Console AUX, grafico delle variabili della pagina web: scelta delle variabili, derivata (°C/min o al secondo, calcolata come pendenza su una finestra regolabile), finestra di tempo, segni dei comandi inviati e lettura dei valori al passaggio del mouse.
- Sessione completa in CSV: durante la diretta ogni campione di ogni variabile e ogni attuazione (Console AUX, sequenze, procedure, comandi shell) vengono scritti in data/series/sessione-*.csv; "Esporta sessione CSV" scarica una tabella per Excel con una colonna per variabile e la colonna attuazioni.
- Console AUX: modalità Shell nel costruttore per comandi SSH liberi sulla colonnina, con output e registrazione nella sessione.

## 1.5.0 (29 ottobre 2026)
- Robustezza: se la connessione SSH cade, sequenze e procedure attendono che torni (fino a 10 minuti) e ripetono lo stesso passo invece di andare in errore.
- Le sequenze salvano la posizione a ogni passo: se PoleDesk viene chiuso, alla riapertura l'esecuzione compare come interrotta, con "Riprendi da quel passo" o "Esegui solo i finali".
- Verifica dell'attuazione sulla pagina web: per ogni comando AUX (sequenze) e per ogni passo (procedure) si può indicare una variabile e il valore atteso, es. il flag della ventola; se non coincide entro il tempo indicato, il passo è KO.
- Pulsante "Avanti ▶" su ogni esecuzione di sequenza per passare subito al passo successivo; un passo può anche attendere esplicitamente la pressione di Avanti.
- Avanzamento a valore stabile: il passo attende che una variabile della pagina resti entro ± tolleranza per tutta una finestra di tempo (default ±0,3 per 60 s), con un tempo massimo oltre il quale si prosegue segnalandolo.
- Pulsante per leggere le variabili della pagina e sceglierle dai campi.

## 1.4.2 (29 ottobre 2026)
- Più colonnine con lo stesso IP collegate insieme su schede di rete diverse (es. porta del PC e adattatore USB): nuovo campo "Scheda di rete del PC" nella colonnina. SSH e lettura della pagina web escono dalla scheda indicata, così ogni connessione raggiunge la colonnina giusta.
- L'elenco colonnine mostra la scheda usata; import ed export CSV con la colonna source_ip.

## 1.4.1 (29 ottobre 2026)
- Colonnine con lo stesso IP: alla connessione PoleDesk legge MAC e nome della macchina, li mostra nell'elenco e li scrive nei rapporti; se sullo stesso IP risponde un'altra colonnina lo segnala e lo registra.
- Dopo un errore di connessione PoleDesk aspetta qualche secondo prima di riprovare, invece di ritentare di continuo.
- I messaggi di paramiko (es. "Socket exception ... 10054") vanno in data/ssh.log e non riempiono più la finestra.
- Errore 10054 spiegato in chiaro; nuovo pulsante Riconnetti nell'intestazione, da usare dopo aver spostato il cavo.

## 1.4.0 (29 ottobre 2026)
- Nuova scheda Sequenze: serie di comandi temporizzati (comandi AUX, comandi shell, attese) divisi in passi Iniziali, Ciclo ripetuto N volte o in continuo, e Finali eseguiti sempre, anche in caso di arresto o errore.
- Le sequenze girano in parallelo sulle colonnine selezionate, ognuna con avanzamento, conto alla rovescia al passo successivo, storico dei passi ed esito; arresto per singola colonnina o per tutte.
- Console AUX: casella "non attendere risposta nel log"; esito "inviato".
- Diagnostica SSH: se la colonnina offre chiavi non supportate, il messaggio d'errore riporta gli algoritmi offerti.

## 1.3.2 (29 ottobre 2026)
- paramiko fissato alla serie 3: dalla 4 non supporta più le chiavi host ssh-dss usate dalle colonnine con SSH datato, e la connessione falliva con "Incompatible ssh peer (no acceptable host key)". avvia.bat sostituisce automaticamente una versione non compatibile.
- Messaggio d'errore comprensibile in caso di SSH non compatibile, e avviso all'avvio se la versione di paramiko non è adatta.

## 1.3.1 (29 ottobre 2026)
- avvia.bat riconosce il collegamento "python" del Microsoft Store, presente anche quando Python non è installato, e mostra istruzioni chiare per l'installazione da python.org, anche senza permessi di amministratore.

## 1.3.0 (28 ottobre 2026)
- Interfaccia rinnovata: nuova intestazione, schede a pillola, card, interruttori, indicatore di diretta, finestre e notifiche più leggibili.
- Nuova scheda Console AUX:
  - costruttore guidato dei comandi PS4GAux (leggi parametro, scrivi parametro, uscite con interruttori, riga libera) con anteprima della riga, senza caricare file;
  - libreria di comandi salvabili, precaricata con i comandi del manuale;
  - pulsanti rapidi per disabilitare, abilitare e leggere il controllo termico;
  - variabili della pagina web della colonnina in diretta (JSON, tabelle HTML, elementi con id, righe nome: valore), con evidenza dei valori cambiati, variazione rispetto all'ultimo comando, riquadri con andamento e segno dei comandi inviati;
  - log PS4GAux in diretta e storico dei comandi con esito;
  - dati della diretta salvati in data/series.
- Il comando della Console AUX viene scritto direttamente in /crve/command, come fa WinSCP nel manuale.
- Corretto l'allineamento delle righe nell'elenco colonnine e nei passi delle procedure.

## 1.2.3 (28 ottobre 2026)
- Ambiente Python e dati spostati in %USERPROFILE%\PoleDesk: con il Python del Microsoft Store la cartella AppData\Local veniva virtualizzata e l'avvio falliva con "Impossibile trovare il percorso specificato".
- Messaggi di errore chiari se Python manca o se l'ambiente non viene creato.

## 1.2.2 (28 ottobre 2026)
- Procedura "Collaudo fan e heater" precompilata con i contenuti dei sette file comando della cartella "Collaudo PS4GAUX": non serve più caricarli a mano.
- Le procedure salvate con versioni precedenti e prive di contenuto vengono completate automaticamente.

## 1.2.1 (28 ottobre 2026)
- Ambiente Python e dati spostati in %LOCALAPPDATA%\PoleDesk, condivisi tra le versioni: aggiornare non reinstalla più le librerie e non richiede di copiare la cartella data.
- Le librerie vengono installate solo se mancano.
- All'avvio viene indicata la cartella dei dati.
- Ripristinato LEGGIMI.md, rimasto vuoto per errore nelle versioni 1.1.1 e 1.2.0.

## 1.2.0 (28 ottobre 2026)
- Nuova scheda Procedure: esecuzione guidata di sequenze di file comando con verifica nel log e conferme visive dell'operatore.
- Procedura pronta "Collaudo fan e heater (PS4GAUX)" basata sul manuale Functional Test PS4GAUX PCBA JP2.1, sette passi.
- Messa in sicurezza automatica in caso di KO, errore o interruzione (riscaldatore spento, ventola spenta, controllo termico riabilitato).
- Rapporto di ogni esecuzione in data/reports, scaricabile dall'interfaccia.

## 1.1.1 (28 ottobre 2026)
- Quando un comando supera il tempo massimo, l'output ricevuto fino a quel momento viene mostrato invece di essere perso.
- Esito "tempo scaduto" distinto da "non eseguito".
- Tempo massimo minimo portato a 5 secondi.

## 1.1.0 (28 ottobre 2026)
- Nome dello strumento: PoleDesk, con numero di versione mostrato nell'intestazione e all'avvio.
- Istruzioni aggiornate: collegamento diretto alle colonnine dalla rete aziendale, nessuna VPN richiesta.

## 1.0.0 (28 ottobre 2026)
- Prima versione: comandi in parallelo, file, log, monitor con grafici, registro operazioni.
