#!/bin/sh
# prova_schermo.sh 2.3.1 — acquisizione dati per il rilevamento del distacco dello schermo, sulla colonnina.
#
# Tutti i parametri stanno in prova_schermo.conf (creato dal generatore). Senza file valgono i valori qui sotto.
# Un altro file di impostazioni: CONF=/percorso/altro.conf sh prova_schermo.sh ...
#
# Uso:
#   sh prova_schermo.sh [configurazione] [note]   una prova (passi, tempi e controlli dal file .conf)
#   sh prova_schermo.sh --seq T2 [nome] [note]     una prova subito con la sequenza del file seq_T2.conf
#   sh prova_schermo.sh --subito | --tra MIN | --alle HH:MM[,HH:MM]  [opzioni]  SEQUENZA [SEQUENZA ...]
#       piano senza installare nulla: a ogni avvio esegue le sequenze in fila.
#       SEQUENZA = "T2=rif:300 carica:300 misura:480 recupero:900 carica:300 misura:480"
#                  (ruoli rif carica misura ventola recupero; ruolo:secondi:riscaldatore:ventola per forzare le uscite)
#                  oppure il nome di un file seq_NOME.conf accanto allo script.
#       opzioni: --giorni N (ripete ogni giorno, predefinito 1), --nome NOME, --note TESTO,
#                --scala X (tempi × X, es. 0.1 per una prova corta), --pausa MIN tra sequenze,
#                --base MIN di base stabile (0 = non aspetta), --prova (pausa e base 0, dati in collaudo/),
#                --parti-sempre (nessuna precondizione all'avvio, resta la pausa tra sequenze),
#                --ip-schermo IP (ping al computer dello schermo), --calendario "T2,T2L;-;T2L,T2" (sequenze notte per notte,
#                "-" = notte senza prove), --monitora MIN (nelle notti senza prove: monitoraggio passivo per MIN minuti)
#   sh prova_schermo.sh --diagnosi [minuti]        livello 0, nessun comando: ping allo schermo, AUX FAULT, dati AUX fermi
#   sh prova_schermo.sh --processi                 elenca piani in attesa, prove in corso e blocchi (anche di versioni vecchie)
#   sh prova_schermo.sh --pulisci                  chiude tutto (piani, prove, doppioni, blocchi) e ripristina: colonnina pulita
#   sh prova_schermo.sh --annulla                  annulla il piano (la prova in corso finisce; per fermarla --ferma)
#   sh prova_schermo.sh --controlli [minuti] [nome] solo lettura di temperature, flag e ping: non comanda nulla
#   sh prova_schermo.sh --ferma                    annulla il piano e ferma la prova in corso (con ripristino)
#   sh prova_schermo.sh --ripristina               uscite spente e controllo termico riattivato, verificato
#   sh prova_schermo.sh --guardia                  (timer e avvio) ripristina se una prova è rimasta a metà
#   sh prova_schermo.sh --verifica                 colonnina pulita? (processi, file di stato, TempContEn): VERIFICA=OK|KO;dettaglio
#   sh prova_schermo.sh --stato                    stato, ultime prove, valore di TempContEn
#                                                  (ultima riga STATO_BREVE=libera|programmata|pausa|attesa_base|in_corso|monitoraggio|diagnosi;dettaglio)
#   sh prova_schermo.sh --archivio                 un unico file .tar.gz con tutti i dati, da prelevare
#
# Sicurezza: il controllo termico automatico (TempContEn) torna sempre a 1, con cinque livelli:
#   1. a fine prova e su Ctrl+C / kill (trap), con verifica rileggendo TempContEn;
#   2. guardiano a tempo lanciato a inizio prova: se lo script si blocca, ripristina dopo durata + margine;
#   3. systemd: ExecStopPost e TimeoutStartSec sul servizio della prova;
#   4. timer di guardia ogni 10 min e all'avvio della colonnina: se c'è il segnale ".tc_disattivo"
#      e nessuna prova attiva, ripristina;
#   5. soglie di sicurezza, ricarica, FAULT e pagina muta interrompono subito la prova.

VERSIONE="2.3.1"
CONF="${CONF:-$(cd "$(dirname "$0")" 2>/dev/null && pwd)/prova_schermo.conf}"
[ -f "$CONF" ] && . "$CONF"
BASE="${DIR:-/home/root/test}"       # blocco, segnali e piano stanno qui anche se una sequenza sposta i dati altrove

# ---------------- sequenze: file seq_NOME.conf accanto allo script (passi ed eventuali altri parametri)
seq_file() {                         # percorso completo del file della sequenza ("" se non c'è)
    sd=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
    for f in "$1" "$sd/seq_$1.conf" "$sd/$1.conf" "${DIR:-/home/root/test}/seq_$1.conf"; do
        [ -f "$f" ] && { echo "$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"; return; }
    done
}
seq_total() {                        # secondi di passi di una sequenza ("-" = quella del file .conf)
    if [ "$1" = - ]; then echo "$TOTAL"; return; fi
    ( STEPS=""; . "$1"; echo "$STEPS" | awk -F';' 'NF >= 3 && $1 !~ /^#/ { t += $3 } END { print t + 0 }' )
}
if [ "$1" = "--seq" ]; then          # una sequenza subito, in primo piano: sh prova_schermo.sh --seq T2 [nome] [note]
    SEQ=$(seq_file "$2"); [ -z "$SEQ" ] && { echo "Sequenza non trovata: $2 (per scriverla nel comando usa --subito)"; exit 1; }; shift 2
fi
SEQNAME=""
if [ -n "$SEQ" ]; then [ -f "$SEQ" ] || { echo "File di sequenza non trovato: $SEQ"; exit 1; }
    . "$SEQ"; SEQNAME=$(basename "$SEQ" .conf | sed 's/^seq_//'); fi

# ---------------- valori predefiniti (sovrascritti dal file .conf)
: "${DIR:=/home/root/test}"
: "${URL:=http://127.0.0.1/}"
: "${CMD:=/crve/command}"
: "${AUXLOG:=/crve/log/PS4GAux.log}"
: "${INTERVAL:=5}"
: "${STEPS:=
Riferimento;rif;300;0;0
Carica 1;carica;180;1;0
Misura 1;misura;300;0;1
Recupero;recupero;600;0;0
Carica 2;carica;180;1;0
Misura 2;misura;300;0;1
}"
: "${AUX_MAX:=60}"; : "${TEMP_MAX:=60}"
: "${MIN_UPTIME_H:=3}"; : "${USER_OK=IDLE}"; : "${CHARGE_RX:=}"     # USER_OK="" = nessun controllo dello stato
: "${REST_MIN:=60}"
: "${STABLE_MIN:=30}"; : "${STABLE_TOL:=0.3}"; : "${PRE_WAIT_MIN:=60}"
: "${DRIFT_MAX:=0.05}"; : "${HEATER_TOL_S:=15}"; : "${LED_MAX:=1.0}"
: "${WATCHDOG_MARGIN_MIN:=15}"
: "${DISPLAY_FLAG:=DISPLAY CONNECTED}"; : "${DISPLAY_OK:=^(CONNECTED|ON|OK|TRUE|YES|1)$}"
: "${PSG_FLAG:=PSG4X CONNECTED}"; : "${PSG_OK:=^(CONNECTED|ON|OK|TRUE|YES|1)$}"
: "${DISPLAY_IP:=}"; : "${PING_EVERY:=1}"
: "${BASELINE_N:=3}"; : "${Q_LOW:=1.25}"; : "${Q_HIGH:=1.5}"
: "${A_HIGH:=}"; : "${C_MAX:=0.35}"
: "${FLAG_MIN_PCT:=50}"; : "${PING_FAIL_MAX:=50}"
: "${RULES:=Q DISPLAY PSG PING}"
: "${PAGE_SNAPSHOT:=1}"; : "${MIN_FREE_KB:=5000}"
: "${CONFIG_DEFAULT:=campo}"
: "${LATE_MAX_MIN:=30}"
: "${V5_NAME:=AUX SUPPLY 5V}"; : "${AUX_FAULT_OK:=^(0|NO|FALSE|OK|NONE|NO FAULT)?$}"
: "${DIAG_PING_KO_PCT:=80}"; : "${DIAG_FAULT_PCT:=50}"
[ -n "$IP_SCHERMO" ] && DISPLAY_IP="$IP_SCHERMO"      # dal comando (--ip-schermo) o dall'ambiente: vale su .conf e sequenze

PASSO="$BASE/.passo"
LOCK="$BASE/.lock"; MARK="$BASE/.tc_disattivo"; LAST="$DIR/.ultima_prova"; GLOG="$BASE/guardia.log"; SCHED="$BASE/.programmata"
INC="$BASE/.prova_in_corso"; mkdir -p "$BASE" || exit 1
mkdir -p "$DIR" || exit 1
now() { date +%s; }
ts()  { date '+%Y-%m-%dT%H:%M:%S%z'; }
glog() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$GLOG"; }
RUNLOG="$GLOG"
log() { echo "$(date '+%H:%M:%S') $*" >> "$RUNLOG"; echo "$*"; }

# ---------------- comandi PS4GAux e verifica di TempContEn
send() { printf '%s\n' "$1" > "$CMD"; echo "$(date '+%H:%M:%S') comando $1" >> "$RUNLOG"; }
tc_read() {                          # stampa il valore di TempContEn letto nel log di PS4GAux ("" se non risponde)
    [ -f "$AUXLOG" ] || { echo ""; return; }
    off=$(wc -c < "$AUXLOG" 2>/dev/null); off=${off:-0}
    printf '04;00;TempContEn\n' > "$CMD"
    i=0
    while [ "$i" -lt 8 ]; do
        sleep 1
        sz=$(wc -c < "$AUXLOG" 2>/dev/null); sz=${sz:-0}
        [ "$sz" -lt "$off" ] && off=0
        v=$(tail -c +"$((off + 1))" "$AUXLOG" 2>/dev/null | sed -n 's/.*Value=\([^ ,;]*\).*/\1/p' | tail -n 1)
        [ -n "$v" ] && { echo "$v"; return; }
        i=$((i + 1))
    done
    echo ""
}
wd_stop() {                          # chiude il guardiano a tempo e il suo timer (sleep)
    w=$(cat "$BASE/.wd_pid" 2>/dev/null)
    if [ -n "$w" ]; then
        for c in /proc/[0-9]*; do [ "$(awk '{ print $4 }' "$c/stat" 2>/dev/null)" = "$w" ] && kill "${c#/proc/}" 2>/dev/null; done
        kill "$w" 2>/dev/null
    fi
    rm -f "$BASE/.wd_pid"
}
restore() {                          # uscite spente, TempContEn = 1, verifica; fino a 3 tentativi
    v=""
    k=1
    while [ "$k" -le 3 ]; do
        send "07;00;00;01;00;01"; sleep 2
        send "05;00;TempContEn;1"; sleep 2
        v=$(tc_read)
        [ "$v" = "1" ] && break
        [ -z "$v" ] && break         # log non disponibile: non verificabile
        k=$((k + 1))
    done
    if [ "$v" = "1" ]; then
        rm -f "$MARK"; wd_stop; log "Ripristino verificato: uscite spente, TempContEn = 1."
    elif [ -z "$v" ]; then
        rm -f "$MARK"; wd_stop; log "Ripristino inviato (uscite spente, TempContEn = 1); verifica non possibile: nessuna risposta nel log."
    else
        log "ATTENZIONE: TempContEn letto = $v dopo 3 tentativi. Il timer di guardia riproverà."; glog "TempContEn = $v dopo il ripristino"
    fi
}
running_pid() {                     # PID della prova attiva (non zombie, ed è davvero questo script)
    [ -f "$LOCK/pid" ] || return 0
    p=$(cat "$LOCK/pid"); [ -n "$p" ] && [ -d "/proc/$p" ] || return 0
    [ "$(awk '{ print $3 }' "/proc/$p/stat" 2>/dev/null)" = "Z" ] && return 0
    tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q prova_schermo && echo "$p"
    return 0
}
sched_pid() {                       # PID del piano in attesa (--alle / --tra / --subito): vivo, non zombie, ed è davvero questo script
    [ -f "$SCHED" ] || return 0
    p=$(sed -n 's/^PID=//p' "$SCHED"); [ -n "$p" ] && [ -d "/proc/$p" ] || return 0
    [ "$(awk '{ print $3 }' "/proc/$p/stat" 2>/dev/null)" = "Z" ] && return 0
    tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q prova_schermo && echo "$p"
    return 0
}
prova_procs() {                     # "PID ruolo comando" di ogni processo prova_schermo (anche di altre versioni), escluso questo comando
    WDP=$(cat "$BASE/.wd_pid" 2>/dev/null)
    MYNS=$(readlink /proc/$$/ns/mnt 2>/dev/null)
    for d in /proc/[0-9]*; do
        q=${d#/proc/}
        [ "$q" = "$$" ] || [ "$q" = "$PPID" ] && continue
        # solo i processi di questo sistema: quelli dei container (altro spazio dei mount) non si toccano
        if [ -n "$MYNS" ]; then ns=$(readlink "$d/ns/mnt" 2>/dev/null); [ -n "$ns" ] && [ "$ns" != "$MYNS" ] && continue; fi
        c=$(tr '\0' ' ' 2>/dev/null < "$d/cmdline") 2>/dev/null || continue
        case "$c" in *prova_schermo*) ;; *) continue;; esac
        set -- $(cat "$d/stat" 2>/dev/null) 2>/dev/null
        [ "$3" = Z ] && continue
        pp=$4; a=$4; n=0; mine=0                             # discendenti di questo comando (sottoshell, pipeline): esclusi
        while [ -n "$a" ] && [ "$a" -gt 1 ] && [ "$n" -lt 12 ]; do
            [ "$a" = "$$" ] && { mine=1; break; }
            a=$(awk '{ print $4 }' "/proc/$a/stat" 2>/dev/null); n=$((n + 1))
        done
        [ "$mine" = 1 ] && continue
        case "$c" in
            *grep*|*awk*|*tail*|*" cat "*) continue;;
            *--alle*|*--tra*|*--subito*) r=piano;;
            *--controlli*|*--diagnosi*) r=controlli;;
            *--guardia*|*--stato*|*--ferma*|*--pulisci*|*--processi*|*--verifica*|*--archivio*|*--ripristina*|*--annulla*) r=servizio;;
            *) r=prova
               # sottoshell della prova (stesso comando): il guardiano a tempo è quella registrata in .wd_pid,
               # le altre sono letture di un istante e non si elencano
               pc=$(tr '\0' ' ' 2>/dev/null < "/proc/$pp/cmdline") 2>/dev/null
               case "$pc" in *--alle*|*--tra*|*--subito*) ;; *prova_schermo*) [ "$q" = "$WDP" ] && r=guardiano || continue;; esac;;
        esac
        echo "$q $r $c"
    done
}
kill_piani() {                      # chiude ogni piano in attesa (anche doppioni o di versioni vecchie); stampa i PID chiusi
    k=""
    for q in $(prova_procs | awk '$2 == "piano" { print $1 }'); do kill "$q" 2>/dev/null && k="$k $q"; done
    rm -f "$SCHED"
    echo "$k"
}

# riga di risultati.csv senza indici (non partita, interrotta): esito, colonne vuote, motivo nella validità
RES_HEAD="data;cuid;configurazione;fascia;esito;regole;I1;I2;I_medio;C;deriva;deriva2;S_medio;calo60;Q;base_n;led_max_A;display_ok_pct;psg_ok_pct;ping_ok_pct;ping_ms;validita;note;file"
row_vuota() { printf '%s;;;;;;;;;;;;;;;;;%s' "$1" "$2"; }
res_file() {                         # risultati.csv con l'intestazione di questa versione (quello di una versione vecchia viene rinominato)
    R="$1/risultati.csv"
    if [ -f "$R" ] && [ "$(head -n 1 "$R")" != "$RES_HEAD" ]; then mv -f "$R" "$1/risultati_fino_$(date '+%Y%m%d_%H%M').csv"; fi
    [ -f "$R" ] || echo "$RES_HEAD" > "$R"
    echo "$R"
}

# ---------------- lettura della pagina
read_page() {
    PAGE="$(wget -q -T 5 -O- "$URL" 2>/dev/null)"
    LINES="$(echo "$PAGE" | sed 's/<[^>]*>/\n/g' | sed '/^[[:space:]]*$/d' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
}
get_value() {                        # riga uguale al nome (con o senza ":"), valore nella riga dopo
    v="$(echo "$LINES" | grep -x -F -A1 "$1" | sed -n '2p')"
    [ -z "$v" ] && v="$(echo "$LINES" | grep -x -F -A1 "$1:" | sed -n '2p')"
    echo "$v"
}
num() { echo "$1" | awk '{ gsub(",", "."); if ($1 ~ /^-?[0-9.]+$/) print $1; else print "" }'; }
gt() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a != "" && b != "" && a + 0 > b + 0) }'; }
flag_ok() {                          # 1 se il valore corrisponde all'espressione "ok", 0 se no, vuoto se il flag non c'è
    [ -z "$1" ] && { echo ""; return; }
    echo "$1" | grep -q -i -E "$2" && echo 1 || echo 0
}
ping_ms() {
    [ -z "$DISPLAY_IP" ] && { echo ""; return; }
    r=$(ping -c 1 -W 1 "$DISPLAY_IP" 2>/dev/null | sed -n 's/.*time[=<]\([0-9.]*\).*/\1/p' | head -n 1)
    [ -n "$r" ] && echo "$r" || echo "ko"
}

HEADER="epoch;timestamp;cuid;passo;nome_passo;cmd_riscaldatore;cmd_ventola;aux_heater_power;aux_fan_power;aux_temperature;temperature;cpu_temperature;aux_leds_current;aux_lux;main_power_voltage;aux_supply_12v;user_status;aux_fault;antitamper;evse_task_0;evse_task_1;ok;display_flag;display_ok;psg_flag;psg_ok;ping_ms;ping_ok;aux_supply_5v"
SN=0
sample() {                           # una riga nel CSV; controlli di sicurezza solo durante la prova (SAFETY=1)
    read_page
    NOW=$(now); SN=$((SN + 1))
    P=""; [ $((SN % PING_EVERY)) -eq 0 ] && P=$(ping_ms)
    PO=""; [ -n "$P" ] && { [ "$P" = ko ] && PO=0 || PO=1; }
    if [ -z "$PAGE" ]; then
        echo "$NOW;$(ts);;$PH;$PHNAME;$CH;$CF;;;;;;;;;;;;;;;0;;;;;$P;$PO;" >> "$CSV"
        LOST=$((LOST + INTERVAL))
        if [ "$SAFETY" = 1 ] && [ "$LOST" -gt 30 ]; then RESULT="interrotta"; REASON="pagina non leggibile da più di 30 s"; exit 3; fi
        return
    fi
    LOST=0
    CUID="$(get_value "CUID")"
    AUXT="$(num "$(get_value "AUX TEMPERATURE")")"; TEMP="$(num "$(get_value "TEMPERATURE")")"
    USERST="$(get_value "USER STATUS")"; EVSE0="$(get_value "EVSE TASK 0 STATUS")"; EVSE1="$(get_value "EVSE TASK 1 STATUS")"
    DF="$(get_value "$DISPLAY_FLAG")"; PF="$(get_value "$PSG_FLAG")"
    echo "$NOW;$(ts);$CUID;$PH;$PHNAME;$CH;$CF;$(get_value "AUX HEATER POWER");$(get_value "AUX FAN POWER");$AUXT;$TEMP;$(num "$(get_value "CPU-TEMPERATURE")");$(num "$(get_value "AUX LEDS CURRENT")");$(num "$(get_value "AUX LUX")");$(num "$(get_value "MAIN POWER VOLTAGE")");$(num "$(get_value "AUX SUPPLY 12V")");$USERST;$(get_value "AUX FAULT");$(get_value "ANTITAMPER OK STATUS");$EVSE0;$EVSE1;1;$DF;$(flag_ok "$DF" "$DISPLAY_OK");$PF;$(flag_ok "$PF" "$PSG_OK");$P;$PO;$(num "$(get_value "$V5_NAME")")" >> "$CSV"
    [ "$SAFETY" = 1 ] || return
    if gt "$AUXT" "$AUX_MAX"; then RESULT="interrotta"; REASON="AUX TEMPERATURE $AUXT oltre $AUX_MAX"; exit 4; fi
    if gt "$TEMP" "$TEMP_MAX"; then RESULT="interrotta"; REASON="TEMPERATURE $TEMP oltre $TEMP_MAX"; exit 4; fi
    case "$USERST" in *FAULT*) RESULT="interrotta"; REASON="USER STATUS = $USERST"; exit 5;; esac
    # colonnina libera per tutta la prova: un cambio di stato (ricarica, prenotazione) la rende non confrontabile
    if [ -n "$USER_OK" ] && [ -n "$USERST" ] && [ "$USERST" != "$USER_OK" ]; then RESULT="interrotta"; REASON="colonnina non libera (USER STATUS = $USERST)"; exit 6; fi
    if [ -n "$CHARGE_RX" ] && echo "$EVSE0 $EVSE1" | grep -q -E "$CHARGE_RX"; then RESULT="interrotta"; REASON="inizio di una ricarica"; exit 6; fi
}
pace() {                             # attende il prossimo campione agganciato a $1 (inizio) senza superare $2 (fine)
    K=$((K + 1)); NEXT=$(($1 + K * INTERVAL)); NOW=$(now)
    while [ "$NEXT" -le "$NOW" ]; do K=$((K + 1)); NEXT=$(($1 + K * INTERVAL)); done
    [ -n "$2" ] && [ "$NEXT" -gt "$2" ] && NEXT=$2
    W=$((NEXT - $(now))); [ "$W" -gt 0 ] && sleep "$W"
}

# ---------------- passi dal file .conf: "nome;ruolo;secondi;riscaldatore;ventola"
NS=0; TOTAL=0
while IFS=';' read -r n r s h f; do
    [ -z "$n" ] && continue
    case "$n" in \#*) continue;; esac
    NS=$((NS + 1))
    eval "SN_$NS=\"\$n\"; SR_$NS=\"\$r\"; SS_$NS=\$((s + 0)); SH_$NS=\$((h + 0)); SF_$NS=\$((f + 0))"
    TOTAL=$((TOTAL + s))
done <<EOF_STEPS
$STEPS
EOF_STEPS

# ================= modi di servizio =================
case "$1" in
--ripristina)
    log "Ripristino manuale."; restore; exit 0;;
--guardia)
    p=$(running_pid)
    if [ -n "$p" ]; then exit 0; fi
    if [ -f "$MARK" ] || [ -d "$LOCK" ]; then
        glog "guardia: prova non attiva ma controllo termico ancora disattivato o blocco rimasto: ripristino"
        RUNLOG="$GLOG"; restore; rm -rf "$LOCK"
        [ -f "$INC" ] && { . "$INC"
            echo "$(date '+%Y-%m-%d %H:%M');$CUID;$CONFIG;$SLOT;$(row_vuota interrotta "prova rimasta a metà (riavvio, blocco o arresto) chiusa dalla guardia");$NOTE;$(basename "$CSV")" >> "$(res_file "$(dirname "$CSV")")"
            rm -f "$INC"; }
    fi
    exit 0;;
--ferma)
    # ferma tutto: prima il piano (altrimenti partirebbe la sequenza successiva), poi la prova in corso
    had=0; [ -f "$SCHED" ] && had=1
    k=$(kill_piani); if [ -n "$k" ] || [ "$had" = 1 ]; then echo "Piano annullato: nessun nuovo avvio.${k:+ (chiusi PID$k)}"; fi
    p=$(running_pid)
    if [ -n "$p" ]; then kill -TERM "$p"; echo "Arresto richiesto (PID $p): la prova si chiude con il ripristino."; else echo "Nessuna prova attiva."; restore; fi
    exit 0;;
--verifica)
    # colonnina pulita? nessun processo dello script, nessun file di stato rimasto, TempContEn = 1 (solo lettura se c'è una prova)
    K=""
    L=$(prova_procs | awk '$2 != "servizio"'); [ -n "$L" ] && K="$K processi: $(echo "$L" | awk '{ printf "%s%s(%s)", (NR > 1 ? ", " : ""), $1, $2 }');"
    F=$(for f in .lock .prova_in_corso .tc_disattivo .programmata .passo .wd_pid; do [ -e "$BASE/$f" ] && printf '%s ' "$f"; done)   # .piano: solo copie delle sequenze, non conta
    [ -n "$F" ] && K="$K file di stato rimasti: $F;"
    if [ -z "$L" ]; then
        if [ ! -f "$AUXLOG" ]; then K="$K TempContEn non verificabile ($AUXLOG assente);"
        else v=$(tc_read); [ "$v" = 1 ] || K="$K TempContEn = ${v:-nessuna risposta da PS4GAux};"; fi
    fi
    echo "Script $VERSIONE in $BASE"
    if [ -z "$K" ]; then echo "VERIFICA=OK;nessun processo, nessun file di stato, TempContEn = 1"
    else echo "VERIFICA=KO;$(echo "$K" | sed 's/^ //;s/;$//')"; fi
    exit 0;;
--processi)
    echo "== processi prova_schermo (PID ruolo comando)"
    L=$(prova_procs); [ -n "$L" ] && echo "$L" || echo "nessuno"
    echo "== piani in attesa: $(echo "$L" | awk '$2 == "piano"' | grep -c .)  prove in corso: $(echo "$L" | awk '$2 == "prova"' | grep -c .)"
    [ -f "$SCHED" ] && { echo "== piano registrato"; cat "$SCHED"; }
    [ -d "$LOCK" ] && echo "== blocco $LOCK (PID $(cat "$LOCK/pid" 2>/dev/null))"
    ls -d /tmp/prova_schermo.lock* 2>/dev/null | sed 's/^/== blocco di un altro script: /'
    exit 0;;
--pulisci)
    # azzeramento: niente piani, niente prove, niente blocchi, uscite spente e TempContEn = 1 verificato
    k=$(kill_piani); echo "Piani chiusi:${k:- nessuno}"
    p=$(running_pid)
    if [ -n "$p" ]; then kill -TERM "$p"; echo "Prova in corso (PID $p): arresto con ripristino"; i=0; while [ -d "/proc/$p" ] && [ "$i" -lt 45 ]; do sleep 1; i=$((i + 1)); done; fi
    # processi rimasti (anche sottoprocessi di un istante del ripristino): fino a 4 giri, l'ultimo a forza
    attivi() { prova_procs | awk '$2 == "prova" || $2 == "guardiano" || $2 == "controlli" || $2 == "piano" { print $1 }'; }
    i=0; CH=""
    while [ "$i" -lt 4 ]; do
        R=$(attivi); [ -z "$R" ] && break
        CH="$CH $R"
        if [ "$i" -ge 2 ]; then kill -9 $R 2>/dev/null; else kill $R 2>/dev/null; fi
        sleep 3; i=$((i + 1))
    done
    [ -n "$CH" ] && echo "Altri processi prova_schermo chiusi:$(echo $CH | tr ' ' '\n' | sort -u | tr '\n' ' ')"
    rm -rf /tmp/prova_schermo.lock* "$LOCK" "$INC" "$PASSO" "$BASE/.piano" "$BASE/.wd_pid"
    RUNLOG="$GLOG"; restore
    # verifica: conta solo ciò che resta in due letture a 3 s di distanza (i processi di un istante spariscono da soli)
    L1=$(prova_procs | awk '$2 != "servizio" { print $1 }'); sleep 3; L2=$(prova_procs | awk '$2 != "servizio"')
    RESTA=$(echo "$L2" | awk -v a=" $(echo $L1) " 'NF && index(a, " " $1 " ")')
    if [ -n "$RESTA" ]; then kill -9 $(echo "$RESTA" | awk '{ print $1 }') 2>/dev/null; sleep 2
        RESTA=$(prova_procs | awk -v a=" $(echo "$RESTA" | awk '{ print $1 }' | tr '\n' ' ') " '$2 != "servizio" && index(a, " " $1 " ")'); fi
    [ -n "$RESTA" ] && { echo "ATTENZIONE: restano processi:"; echo "$RESTA"; exit 1; }
    echo "Colonnina pulita: nessun piano, nessuna prova, nessun blocco."
    exit 0;;
--stato)
    p=$(running_pid); q=$(sched_pid)
    echo "prova_schermo $VERSIONE, file di impostazioni: $CONF $( [ -f "$CONF" ] && echo trovato || echo 'NON trovato: valori predefiniti')"
    [ -n "$q" ] || echo "Senza piano: passi predefiniti dello script $NS, durata $((TOTAL / 60)) min."
    echo "Prova attiva: ${p:-no}"
    np=$(prova_procs | awk '$2 == "piano"' | grep -c .); [ "$np" -gt 1 ] && echo "ATTENZIONE: $np piani in attesa (doppioni): sh $0 --pulisci e riprogramma"
    [ -n "$q" ] && { echo "Piano programmato: sequenze [$(sed -n 's/^SEQUENZE=//p' "$SCHED")], nome $(sed -n 's/^NOME=//p' "$SCHED"), pausa $(sed -n 's/^PAUSA=//p' "$SCHED") min, $( [ "$(sed -n 's/^PARTI_SEMPRE=//p' "$SCHED")" = 1 ] && echo "parti sempre" || echo "base stabile $(sed -n 's/^BASE=//p' "$SCHED") min")$( [ "$(sed -n 's/^SCALA=//p' "$SCHED")" != 1 ] && echo ", tempi × $(sed -n 's/^SCALA=//p' "$SCHED")"); avvii:"
        sed -n 's/^AVVIO=/  /p' "$SCHED"; }
    [ -f "$INC" ] && cat "$INC"
    [ -f "$MARK" ] && echo "Segnale: controllo termico disattivato da una prova ($(cat "$MARK"))"
    [ -z "$p" ] && { [ -f "$INC" ] || [ -d "$LOCK" ]; } && echo "ATTENZIONE: c'è una prova lasciata a metà (processo terminato senza chiusura): sh $0 --pulisci ripristina uscite e TempContEn"
    [ -f "$SCHED" ] && [ -z "$q" ] && echo "ATTENZIONE: piano registrato ma il suo processo non c'è più: sh $0 --pulisci"
    # durante una prova non si scrive nel file comando: una lettura potrebbe sovrascrivere il comando di un passo
    if [ -n "$p" ] || [ -d "$LOCK" ]; then echo "TempContEn: non letto durante la prova (la prova lo tiene a 0; Stato non scrive comandi)"
    else echo "TempContEn letto: $(tc_read)"; fi
    [ -f "$DIR/risultati.csv" ] && { echo "Ultime prove:"; tail -n 5 "$DIR/risultati.csv"; }
    df -k "$DIR" | tail -n 1
    # riga per PoleDesk: STATO_BREVE=stato;dettaglio
    if [ -n "$p" ]; then
        c=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)
        case "$c" in
            *--diagnosi*) SB="diagnosi;";;
            *--controlli*) SB="monitoraggio;$(echo "$c" | awk '{ for (i = 1; i <= NF; i++) if ($i == "--controlli") print $(i + 1) " min" }')";;
            *) if [ -f "$PASSO" ] && [ -f "$INC" ]; then SB="in_corso;$(cut -d';' -f1,2 "$PASSO" | tr ';' ' ') fino alle $(date -d "@$(cut -d';' -f3 "$PASSO")" '+%H:%M' 2>/dev/null)"
               elif [ -f "$INC" ]; then SB="in_corso;$(sed -n "s/^CONFIG='\(.*\)'/\1/p" "$INC") avvio"
               else SB="attesa_base;"; fi;;
        esac
    elif [ -f "$INC" ] || [ -d "$LOCK" ]; then
        SB="anomala;prova terminata senza chiusura (processo chiuso dall'esterno): premi Ferma per ripristinare"
    elif [ -n "$q" ]; then
        NX=$(sed -n 's/^AVVIO=//p' "$SCHED" | awk -v n="$(date '+%Y-%m-%d %H:%M')" '$0 > n { print; exit }')
        if tail -n 1 "$BASE/programmata.log" 2>/dev/null | grep -q " pausa "; then SB="pausa;$(tail -n 1 "$BASE/programmata.log" | sed -n 's/.* pausa \(fino alle [0-9:]*\).*/\1/p')"
        else SB="programmata;${NX:-in corso di avvio}"; fi
    else SB="libera;"; fi
    echo "STATO_BREVE=$SB"
    for f in "$DIR/risultati.csv" "$DIR/collaudo/risultati.csv"; do [ -f "$f" ] && tail -n 4 "$f" | grep -v '^data;' | sed "s|^|RIS=$(basename "$(dirname "$f")")/|"; done
    exit 0;;
--alle|--tra|--subito)
    # Piano senza installare nulla: uno o più orari, una o più sequenze in fila (con i tempi scritti nel comando
    # o in file seq_NOME.conf), ripetuto per N giorni. Il processo in attesa sparisce a fine piano,
    # con --annulla o se la colonnina si riavvia.
    USO="Uso: --subito | --tra MINUTI | --alle HH:MM[,HH:MM]  [--giorni N] [--nome NOME] [--note TESTO]
          [--scala X] [--pausa MIN] [--base MIN] [--parti-sempre] [--prova] [--ip-schermo IP]
          [--calendario \"T2,T2L;-;T2L,T2\" [--monitora MIN]]  SEQUENZA [SEQUENZA ...]
  SEQUENZA = \"NOME=ruolo:secondi ruolo:secondi ...\"  (ruoli: rif carica misura ventola recupero)
             oppure il nome di un file seq_NOME.conf accanto allo script"
    MODE="$1"; shift
    if [ "$MODE" = "--subito" ]; then MODE="--tra"; WHEN=0
    else WHEN="$1"; [ -z "$WHEN" ] && { echo "$USO"; exit 1; }; shift; fi
    k=$(kill_piani); [ -n "$k" ] && echo "Piano precedente sostituito (chiusi PID$k)."
    GIORNI=1; NOME="$CONFIG_DEFAULT"; NT=""; SCALA=1; O_PAUSA=""; O_BASE=""; PROVA=0; NSQ=0; CAL=""; MON=0; PS=0
    PIANO="$BASE/.piano"; rm -rf "$PIANO"; mkdir -p "$PIANO"
    while [ $# -gt 0 ]; do
        case "$1" in
            --giorni) GIORNI="$2"; shift 2;;
            --nome)   NOME="$2"; shift 2;;
            --note)   NT="$2"; shift 2;;
            --scala)  SCALA="$2"; shift 2;;
            --pausa)  O_PAUSA="$2"; shift 2;;
            --base)   O_BASE="$2"; shift 2;;
            --prova)  PROVA=1; shift;;
            --parti-sempre) PS=1; shift;;
            --ip-schermo) IP_SCHERMO="$2"; shift 2;;
            --calendario) CAL="$2"; shift 2;;
            --monitora) MON="$2"; shift 2;;
            -*) echo "Opzione sconosciuta: $1"; echo "$USO"; exit 1;;
            *)  NSQ=$((NSQ + 1)); eval "SQ_$NSQ=\"\$1\""; shift;;
        esac
    done
    case "$GIORNI" in ''|*[!0-9]*|0) echo "--giorni vuole un numero da 1 in su"; exit 1;; esac
    case "$MON" in ''|*[!0-9]*) echo "--monitora vuole i minuti"; exit 1;; esac
    if [ -n "$CAL" ]; then                 # calendario: le sequenze delle notti sono quelle scritte nel calendario
        [ "$NSQ" = 0 ] || { echo "Con --calendario le sequenze si scrivono solo nel calendario"; exit 1; }
        GIORNI=$(echo "$CAL" | awk -F';' '{ print NF }')
        for x in $(echo "$CAL" | tr ';,' '  ' | tr ' ' '\n' | grep -v '^-$' | grep . | awk '!v[$0]++'); do NSQ=$((NSQ + 1)); eval "SQ_$NSQ=\"\$x\""; done
        case "$WHEN" in *,*) echo "Con --calendario un solo orario di avvio"; exit 1;; esac
    fi
    export IP_SCHERMO
    echo "$SCALA" | grep -q -E '^[0-9]*\.?[0-9]+$' || { echo "--scala vuole un numero (es. 0.1 = tempi a un decimo)"; exit 1; }
    if [ "$PROVA" = 1 ]; then              # prova di funzionamento: subito pronta, dati a parte
        RDIR="$BASE/collaudo"; [ -z "$O_PAUSA" ] && O_PAUSA=0; [ -z "$O_BASE" ] && O_BASE=0
    else RDIR="$BASE"; fi
    [ "$NSQ" = 0 ] && { NSQ=1; SQ_1="-"; }   # nessuna sequenza: quella del file .conf
    # ogni sequenza diventa un file in .piano/N/seq_NOME.conf (tempi scalati, eventuali opzioni del comando)
    SEQS=""; TOT=0; LIST=""; i=1
    while [ "$i" -le "$NSQ" ]; do
        eval "A=\$SQ_$i"; D="$PIANO/$i"; mkdir -p "$D"
        case "$A" in
            -)  SNAME="conf"; SRC="$CONF"; RAW="$STEPS";;
            *=*|*:*)                            # sequenza scritta nel comando
                SNAME="${A%%=*}"; [ "$SNAME" = "$A" ] && SNAME="S$i"; SPEC="${A#*=}"; SRC=""
                RAW=$(echo "$SPEC" | tr ', ' '\n\n' | awk -F':' -v sn="$SNAME" '
                    NF == 0 || $0 == "" { next }
                    { r = tolower($1); s = $2 + 0
                      if (NF < 2 || s <= 0) { print "ERR " $0; exit }
                      if (r == "rif" || r == "riferimento") { role = "rif"; h = 0; f = 0; nm = "Riferimento" }
                      else if (r == "carica" || r == "risc" || r == "riscaldatore") { role = "carica"; h = 1; f = 0; nm = "Carica" }
                      else if (r == "misura") { role = "misura"; h = 0; f = 1; nm = "Misura" }
                      else if (r == "ventola") { role = "ventola"; h = 0; f = 1; nm = "Ventola" }
                      else if (r == "recupero" || r == "riposo") { role = "recupero"; h = 0; f = 0; nm = "Recupero" }
                      else { print "ERR " $0; exit }
                      if (NF >= 4) { h = $3 + 0; f = $4 + 0 }
                      c[nm]++; printf "%s %d;%s;%d;%d;%d\n", nm, c[nm], role, s, h, f }')
                case "$RAW" in *ERR*|"") echo "Sequenza non valida: $A"; echo "  passo non riconosciuto:$(echo "$RAW" | sed -n 's/^ERR//p')"; echo "$USO"; exit 1;; esac;;
            *)  F=$(seq_file "$A"); [ -z "$F" ] && { echo "Sequenza non trovata: $A (né scritta come NOME=ruolo:secondi, né file seq_$A.conf)"; exit 1; }
                SNAME=$(basename "$F" .conf | sed 's/^seq_//'); SRC="$F"; RAW=$( STEPS=""; . "$F"; echo "$STEPS" );;
        esac
        SCALED=$(echo "$RAW" | awk -F';' -v k="$SCALA" -v mn="$((INTERVAL * 2))" 'NF >= 5 && $1 !~ /^#/ { s = int($3 * k + 0.5); if (s < mn) s = mn; print $1 ";" $2 ";" s ";" $4 ";" $5 }')
        { [ -n "$SRC" ] && [ -f "$SRC" ] && echo ". \"$SRC\""
          echo "STEPS=\"
$SCALED
\""
          echo "DIR=\"$RDIR\""
          [ -n "$O_BASE" ] && echo "STABLE_MIN=$O_BASE"
          [ -n "$O_PAUSA" ] && echo "REST_MIN=$O_PAUSA"
          [ "$PROVA" = 1 ] && echo "MIN_UPTIME_H=0"
          # parti sempre: nessuna precondizione all'avvio (base stabile, accensione, USER STATUS, prova precedente);
          # la pausa tra le sequenze resta (la fa il piano). Le sicurezze durante la prova restano tutte.
          [ "$PS" = 1 ] && printf 'STABLE_MIN=0\nMIN_UPTIME_H=0\nUSER_OK=""\nREST_MIN=0\n'
          :; } > "$D/seq_$SNAME.conf"
        T=$(echo "$SCALED" | awk -F';' '{ t += $3 } END { print t + 0 }'); TOT=$((TOT + T))
        SEQS="$SEQS $D/seq_$SNAME.conf"
        LIST="$LIST
  $i. $SNAME ($((T / 60)) min $((T % 60)) s): $(echo "$SCALED" | awk -F';' '{ printf "%s%s %ds", (NR > 1 ? " → " : ""), $1, $3 }')"
        i=$((i + 1))
    done
    if [ "$MODE" = "--tra" ]; then
        case "$WHEN" in ''|*[!0-9]*) echo "$USO"; exit 1;; esac
        FIRST="$(( $(now) + WHEN * 60 ))"
    else
        FIRST=$(echo "$WHEN" | tr ',' ' ' | awk -v n="$(now)" -v hms="$(date '+%H %M %S')" '
            BEGIN { split(hms, c, " "); sod = c[1] * 3600 + c[2] * 60 + c[3] }
            { for (i = 1; i <= NF; i++) { if ($i !~ /^[0-9]?[0-9]:[0-5][0-9]$/) { print "ERR"; exit }
                split($i, a, ":"); if (a[1] > 23) { print "ERR"; exit }
                w = a[1] * 3600 + a[2] * 60 - sod; if (w <= 0) w += 86400; printf "%d ", n + w } }')
        case "$FIRST" in *ERR*|"") echo "Orari non validi: $WHEN (usa HH:MM, più orari separati da virgola, es. 21:00,01:00)"; exit 1;; esac
    fi
    p=$(running_pid); [ -n "$p" ] && [ "$WHEN" = 0 ] && { echo "Una prova è in corso (PID $p): aspetta che finisca o fermala con --ferma."; exit 1; }
    EVENTS=$(for e in $FIRST; do d=0; while [ "$d" -lt "$GIORNI" ]; do echo "$((e + d * 86400)):$((d + 1))"; d=$((d + 1)); done; done | sort -n | tr '\n' ' ')
    SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    PAUSA="${O_PAUSA:-$REST_MIN}"; RLAST="$RDIR/.ultima_prova"; mkdir -p "$RDIR"
    export CONF
    ( trap '' HUP INT
      for evd in $EVENTS; do
          ev=${evd%%:*}; dd=${evd#*:}
          if [ -n "$CAL" ]; then           # sequenze di questa notte, in quest'ordine
              DS=$(echo "$CAL" | awk -F';' -v d="$dd" '{ print $d }' | tr ',' ' ')
              RUNSEQ=""; for x in $DS; do [ "$x" = "-" ] && continue; for sq in $SEQS; do [ "$(basename "$sq")" = "seq_$x.conf" ] && RUNSEQ="$RUNSEQ $sq"; done; done
          else RUNSEQ="$SEQS"; fi
          while [ "$(now)" -lt "$ev" ]; do
              [ -f "$SCHED" ] || exit 0
              R=$((ev - $(now))); [ "$R" -gt 60 ] && R=60; [ "$R" -gt 0 ] && sleep "$R"
          done
          [ -f "$SCHED" ] || exit 0
          LATE=$(( ($(now) - ev) / 60 ))
          if [ "$LATE" -gt "$LATE_MAX_MIN" ]; then
              echo "$(date '+%Y-%m-%d %H:%M:%S') avvio delle $(date -d "@$ev" '+%H:%M' 2>/dev/null) saltato: in ritardo di $LATE min (la sequenza precedente era ancora in corso)"
              continue
          fi
          if [ -z "$RUNSEQ" ]; then
              if [ "$MON" -gt 0 ]; then echo "$(date '+%Y-%m-%d %H:%M:%S') notte $dd senza prove: monitoraggio passivo per $MON min"
                  sh "$SELF" --controlli "$MON" monitor
              else echo "$(date '+%Y-%m-%d %H:%M:%S') notte $dd senza prove"; fi
              continue
          fi
          PRIMA=1
          for sq in $RUNSEQ; do
              [ -f "$SCHED" ] || exit 0
              if [ -f "$RLAST" ] && [ "$PAUSA" -gt 0 ] && ! { [ "$PS" = 1 ] && [ "$PRIMA" = 1 ]; }; then
                  W=$(( PAUSA * 60 - ($(now) - $(cat "$RLAST")) ))
                  [ "$W" -gt 0 ] && { echo "$(date '+%Y-%m-%d %H:%M:%S') pausa fino alle $(date -d "@$(( $(now) + W ))" '+%H:%M' 2>/dev/null) ($(( (W + 59) / 60 )) min) prima della prossima sequenza"; sleep "$W"; }
              fi
              PRIMA=0
              [ -f "$SCHED" ] || exit 0
              echo "$(date '+%Y-%m-%d %H:%M:%S') avvio: sequenza $(basename "$sq" .conf | sed 's/^seq_//'), nome $NOME"
              SEQ="$sq" sh "$SELF" "$NOME" "$NT"
          done
      done
      rm -f "$SCHED"; rm -rf "$PIANO"; echo "$(date '+%Y-%m-%d %H:%M:%S') piano finito" ) </dev/null >>"$BASE/programmata.log" 2>&1 &
    { echo "PID=$!"; echo "NOME=$NOME"; echo "PAUSA=$PAUSA"; echo "BASE=${O_BASE:-$STABLE_MIN}"; echo "PARTI_SEMPRE=$PS"; echo "SCALA=$SCALA"; echo "SEQUENZE=$(for sq in $SEQS; do printf '%s ' "$(basename "$sq" .conf | sed 's/^seq_//')"; done)"
      for evd in $EVENTS; do ev=${evd%%:*}; dd=${evd#*:}
          echo "AVVIO=$(date -d "@$ev" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "$ev")$( [ -n "$CAL" ] && echo " $(echo "$CAL" | awk -F';' -v d="$dd" '{ print $d }' | sed 's/^-$/(nessuna prova)/')")"; done; } > "$SCHED"
    echo "Ora sulla colonnina: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "Sequenze in fila$( [ "$SCALA" != 1 ] && echo " (tempi × $SCALA)"):$LIST"
    echo "Nome \"$NOME\"; pausa tra sequenze $PAUSA min; $( [ "$PS" = 1 ] && echo "PARTI SEMPRE: nessuna precondizione" || echo "base stabile ${O_BASE:-$STABLE_MIN} min")$( [ "$PROVA" = 1 ] && echo "; PROVA: dati in $RDIR")$( [ -n "$IP_SCHERMO" ] && echo "; ping allo schermo $IP_SCHERMO")$( [ "$MON" -gt 0 ] && echo "; notti senza prove: monitoraggio $MON min")."
    echo "Avvii:"; sed -n 's/^AVVIO=/  /p' "$SCHED"
    echo "Durata di ogni avvio: $((TOT / 60)) min di passi, più pause e attesa della base stabile."
    echo "Nulla è installato: se la colonnina si riavvia il piano si perde. Annulla: sh $SELF --annulla"
    echo "Segui: tail -f $BASE/programmata.log   ·   esiti in $RDIR/risultati.csv"
    exit 0;;
--annulla)
    had=0; [ -f "$SCHED" ] && had=1
    k=$(kill_piani)
    if [ -n "$k" ] || [ "$had" = 1 ]; then echo "Piano annullato: nessun nuovo avvio.${k:+ (chiusi PID$k)}"
    else echo "Nessun piano programmato."; fi
    p=$(running_pid); [ -n "$p" ] && echo "Una prova è in corso (PID $p) e continua fino alla fine. Per fermarla subito: --ferma"
    exit 0;;
--archivio)
    read_page; CU="$(get_value "CUID")"
    A="$DIR/archivio_${CU:-colonnina}_$(date '+%Y%m%d_%H%M').tar.gz"
    # dati della cartella e delle prove di funzionamento (collaudo/), senza archivi precedenti
    L=$(cd "$DIR" && for f in ./*.csv ./*.log ./*.txt ./*.conf ./collaudo/*.csv ./collaudo/*.log ./collaudo/*.txt; do [ -f "$f" ] && echo "$f"; done)
    [ -z "$L" ] && { echo "Nessun dato da archiviare in $DIR"; exit 1; }
    (cd "$DIR" && tar czf "$A" $L)
    echo "Archivio pronto: $A"; exit 0;;
esac

# ---------------- indici della prova e regole (chiamata a fine prova)
# Validità: corrente LED < LED_MAX, colonnina libera (USER STATUS = USER_OK) per tutta la prova, |deriva| ≤ DRIFT_MAX,
# riscaldatore e ventola letti accesi per la durata prevista, due misure coerenti (C ≤ C_MAX).
# Deriva di ogni misura: ultimi 3 min del passo che precede la sua carica (Riferimento per la 1, Recupero per la 2).
analyse() {
    ROLES=""; i=1
    while [ "$i" -le "$NS" ]; do eval "ROLES=\"\$ROLES \$i:\$SR_$i:\$SS_$i\""; i=$((i + 1)); done
    A=$(awk -F';' -v roles="$ROLES" -v htol="$HEATER_TOL_S" -v dmax="$DRIFT_MAX" -v lmax="$LED_MAX" -v uok="$USER_OK" -v cmax="$C_MAX" -v iv="$INTERVAL" '
    function on(v) { v = toupper(v); if (v ~ /^(ON|TRUE|YES)/) return 1; if (v ~ /^OFF/) return 0; return (v + 0 > 0) }
    function slope(a, b,   k, c, sx, sy, mx, my, nu, de) { c = 0; sx = 0; sy = 0
        for (k = 1; k <= n; k++) if (t[k] >= a && t[k] < b) { sx += t[k]; sy += T[k]; c++ }
        if (c < 3) return ""; mx = sx / c; my = sy / c; nu = 0; de = 0
        for (k = 1; k <= n; k++) if (t[k] >= a && t[k] < b) { nu += (t[k] - mx) * (T[k] - my); de += (t[k] - mx) ^ 2 }
        return de > 0 ? 60 * nu / de : "" }
    function near(x,   k, b, bd) { b = ""; for (k = 1; k <= n; k++) if (bd == "" || (t[k] - x) ^ 2 < bd) { bd = (t[k] - x) ^ 2; b = T[k] } return b }
    function endof(k) { return (k + 1 in st) ? st[k + 1] : st[k] + dur[k] }
    BEGIN { m = split(roles, rr, " "); for (k = 1; k <= m; k++) { split(rr[k], q, ":"); role[q[1]] = q[2]; dur[q[1]] = q[3]; ns = q[1] } lm = "" }
    NR > 1 && $4 >= 1 {
        if ($24 != "") { dn++; dd += $24 } if ($26 != "") { pn++; pp += $26 }
        if ($27 != "") { gn++; if ($27 != "ko") { g++; gm += $27 } }
        if ($13 != "" && (lm == "" || $13 + 0 > lm)) lm = $13 + 0
        if (uok != "" && $17 != "" && $17 != uok) ub = $17
        if ($22 != 1 || $10 == "" || $10 == 0) next
        n++; t[n] = $1; T[n] = $10; p[n] = $4; h[n] = on($8); f[n] = on($9); if (!(p[n] in st)) st[p[n]] = t[n] }
    END {
        why = ""; valid = 1; d1 = ""; d2 = ""
        if (lm != "" && lm >= lmax) { valid = 0; why = why sprintf(" corrente LED %.2f A (massimo %s);", lm, lmax) }
        if (ub != "") { valid = 0; why = why " colonnina non libera (USER STATUS " ub ");" }
        mi = 0
        for (k = 2; k <= ns; k++) {
            if (role[k] != "misura" || role[k - 1] != "carica" || !(k in st)) continue
            mi++; c = k - 1; hon = ""; hoff = ""; ton = ""; flast = ""
            # deriva: ultimi 3 min del passo prima della carica (rif o recupero)
            dk = ""; if (c - 1 >= 1 && (role[c - 1] == "rif" || role[c - 1] == "recupero") && (c - 1 in st)) { e = endof(c - 1); dk = slope(e - 180, e) }
            D[mi] = dk
            for (i = 1; i <= n; i++) {
                if (p[i] == c && h[i] && hon == "") hon = t[i]
                if (hon != "" && (p[i] == c || p[i] == k) && !h[i] && hoff == "" && t[i] > hon) hoff = t[i]
                if (p[i] == k && f[i]) { if (ton == "") ton = t[i]; flast = t[i] }
            }
            if (ton == "") { valid = 0; why = why " misura " mi ": ventola mai letta accesa;"; ton = st[k] }
            else { fu = flast + iv - ton; if (fu < dur[k] - htol) { valid = 0; why = why " misura " mi ": ventola accesa " fu " s invece di " dur[k] ";" } }
            if (hon == "") { valid = 0; why = why " misura " mi ": riscaldatore mai letto acceso;" }
            else if (hoff != "") { du = hoff - hon; if (du < dur[c] - htol || du > dur[c] + htol) { valid = 0; why = why " misura " mi ": riscaldatore " du " s invece di " dur[c] ";" } }
            dc = (dk == "") ? 0 : dk
            best = ""
            for (i = 1; i <= n; i++) {
                if (t[i] < ton || t[i] > ton + 45) continue
                Dd = slope(t[i] - 15, t[i] + 15.001); if (Dd != "" && (best == "" || Dd < best)) best = Dd }
            I[mi] = (best == "") ? "" : -best + dc
            a0 = near(ton); a60 = near(ton + 60); cl[mi] = a0 - (a60 - dc)
            sl = slope(st[c], st[k]); S[mi] = (sl == "") ? "" : sl - dc
        }
        d1 = D[1]; d2 = D[2]
        if (d1 == "") why = why " deriva non misurata (manca il riferimento);"
        else if (d1 > dmax || d1 < -dmax) { valid = 0; why = why sprintf(" base in deriva %+.3f °C/min;", d1) }
        if (mi < 2 || I[1] == "" || I[2] == "") { valid = 0; why = why " indice non calcolabile su due misure;" }
        Im = (I[1] != "" && I[2] != "") ? (I[1] + I[2]) / 2 : ""
        C = (Im != "" && Im != 0) ? ((I[1] > I[2] ? I[1] - I[2] : I[2] - I[1]) / Im) : ""
        if (C != "" && C > cmax) { valid = 0; why = why sprintf(" misure non coerenti (C %.2f oltre %s);", C, cmax) }
        Sm = (S[1] != "" && S[2] != "") ? (S[1] + S[2]) / 2 : ""
        cm = (mi >= 2) ? (cl[1] + cl[2]) / 2 : ""
        f3 = "%.3f"; gsub(";", ",", why)
        printf "%d;%s;%s;%s;%s;%s;%s;%s;%s;%s;%s;%s;%s;%s;%s\n", valid, why,
            I[1] == "" ? "" : sprintf(f3, I[1]), I[2] == "" ? "" : sprintf(f3, I[2]), Im == "" ? "" : sprintf(f3, Im), C == "" ? "" : sprintf(f3, C),
            d1 == "" ? "" : sprintf("%.4f", d1), Sm == "" ? "" : sprintf(f3, Sm), cm == "" ? "" : sprintf(f3, cm),
            dn ? int(100 * dd / dn) : "", pn ? int(100 * pp / pn) : "", gn ? int(100 * g / gn) : "", g ? sprintf("%.1f", gm / g) : "",
            d2 == "" ? "" : sprintf("%.4f", d2), lm == "" ? "" : sprintf("%.2f", lm) }' "$CSV")
    VALID=$(echo "$A" | cut -d';' -f1); WHY=$(echo "$A" | cut -d';' -f2); I1=$(echo "$A" | cut -d';' -f3); I2=$(echo "$A" | cut -d';' -f4)
    IM=$(echo "$A" | cut -d';' -f5); CC=$(echo "$A" | cut -d';' -f6); DR=$(echo "$A" | cut -d';' -f7); SM=$(echo "$A" | cut -d';' -f8); CL=$(echo "$A" | cut -d';' -f9)
    DP=$(echo "$A" | cut -d';' -f10); PP=$(echo "$A" | cut -d';' -f11); GP=$(echo "$A" | cut -d';' -f12); GM=$(echo "$A" | cut -d';' -f13)
    D2=$(echo "$A" | cut -d';' -f14); LM=$(echo "$A" | cut -d';' -f15)
    # riferimento della colonnina nella stessa fascia oraria (mediana delle prime BASELINE_N prove valide)
    B="$DIR/baseline.csv"; [ -f "$B" ] || echo "cuid;fascia;I_medio;data;file" > "$B"
    BN=$(awk -F';' -v c="$CUID" -v s="$BKEY" '$1 == c && $2 == s' "$B" | wc -l)
    BM=$(awk -F';' -v c="$CUID" -v s="$BKEY" '$1 == c && $2 == s { print $3 }' "$B" | sort -n | awk '{ v[NR] = $1 } END { if (NR) print (NR % 2) ? v[(NR + 1) / 2] : (v[NR / 2] + v[NR / 2 + 1]) / 2 }')
    Q=""; [ -n "$BM" ] && [ -n "$IM" ] && [ "$BN" -ge "$BASELINE_N" ] && Q=$(awk -v a="$IM" -v b="$BM" 'BEGIN { if (b > 0) printf "%.3f", a / b }')
    FIRED=""
    for r in $RULES; do
        case "$r" in
            Q)       [ -n "$Q" ] && ! gt "$Q_HIGH" "$Q" && FIRED="$FIRED Q=$Q≥$Q_HIGH";;
            DISPLAY) [ -n "$DP" ] && gt "$FLAG_MIN_PCT" "$DP" && FIRED="$FIRED display_ok=$DP%";;
            PSG)     [ -n "$PP" ] && gt "$FLAG_MIN_PCT" "$PP" && FIRED="$FIRED psg_ok=$PP%";;
            PING)    [ -n "$GP" ] && gt "$((100 - GP))" "$PING_FAIL_MAX" && FIRED="$FIRED ping_ko=$((100 - GP))%";;
            ABS)     [ -n "$A_HIGH" ] && [ -n "$IM" ] && ! gt "$A_HIGH" "$IM" && FIRED="$FIRED Ī=$IM≥$A_HIGH";;
        esac
    done
    FIRED=$(echo "$FIRED" | sed 's/^ //')
    if [ -n "$FIRED" ]; then ES="sospetto distacco"
    elif [ "$VALID" != 1 ]; then ES="non valida"
    elif [ "$BN" -lt "$BASELINE_N" ]; then
        echo "$CUID;$BKEY;$IM;$(date '+%Y-%m-%d %H:%M');$(basename "$CSV")" >> "$B"; BN=$((BN + 1)); ES="riferimento $BN/$BASELINE_N"
    elif [ -n "$Q" ] && ! gt "$Q" "$Q_LOW"; then ES="attaccato"
    else ES="incerto"; fi
    [ "$VALID" = 1 ] && V="valida" || V="non valida:$WHY"
    ROW="$ES;$FIRED;$I1;$I2;$IM;$CC;$DR;$D2;$SM;$CL;$Q;$BN;$LM;$DP;$PP;$GP;$GM;$V"
}

# ================= una prova o i soli controlli =================
CONTROLLI=0
DIAG=0
if [ "$1" = "--diagnosi" ]; then CONTROLLI=1; DIAG=1; DURATA_C=$(( ${2:-2} * 60 )); CONFIG="diagnosi"; NOTE=""
elif [ "$1" = "--controlli" ]; then CONTROLLI=1; DURATA_C=$(( ${2:-10} * 60 )); CONFIG="${3:-controlli}"; NOTE=""; else
    CONFIG="${1:-$CONFIG_DEFAULT}"; NOTE="${2:-}"; fi

if ! mkdir "$LOCK" 2>/dev/null; then
    p=$(running_pid)
    if [ -n "$p" ]; then echo "Una prova è già in corso (PID $p)."; exit 1; fi
    rm -rf "$LOCK"; mkdir "$LOCK" || exit 1           # blocco rimasto da un arresto brusco
fi
echo $$ > "$LOCK/pid"

FREE=$(df -k "$DIR" | awk 'NR == 2 { print $4 }')
read_page
CUID="$(get_value "CUID")"; [ -z "$CUID" ] && CUID="sconosciuto"
STAMP="$(date '+%Y%m%d_%H%M')"; SLOT="$(date '+%H')"
[ -n "$SEQNAME" ] && [ "$CONTROLLI" = 0 ] && CONFIG="$SEQNAME-$CONFIG"
BKEY="${SEQNAME:+$SEQNAME@}$SLOT"     # riferimento separato per sequenza e fascia oraria
SAFE_CFG="$(printf '%s' "$CONFIG" | tr -c 'A-Za-z0-9_-' '-')"
CSV="$DIR/${CUID}_${SAFE_CFG}_${STAMP}.csv"; RUNLOG="$DIR/${CUID}_${SAFE_CFG}_${STAMP}.log"
echo "$HEADER" > "$CSV"
[ "$PAGE_SNAPSHOT" = 1 ] && [ -n "$PAGE" ] && echo "$LINES" > "$DIR/${CUID}_${SAFE_CFG}_${STAMP}_pagina.txt"
RESULT="completata"; REASON=""; LOST=0; SAFETY=0; PH=0; PHNAME="Attesa"; CH=0; CF=0
DISABLED=0
log "prova_schermo $VERSIONE: $CONFIG su $CUID, fascia oraria $SLOT, file $(basename "$CSV")"

finish() {
    if [ "$DISABLED" = 1 ]; then restore; fi
    rm -rf "$LOCK"
    [ "$DIAG" = 1 ] && { summary_diagnosi; return; }
    [ "$CONTROLLI" = 1 ] && { summary_controlli; return; }
    [ "$STARTED" = 1 ] || return
    rm -f "$INC"; now > "$LAST"
    rm -f "$PASSO"
    [ "$RESULT" = "completata" ] && analyse || ROW=$(row_vuota interrotta "$REASON")
    echo "$(date '+%Y-%m-%d %H:%M');$CUID;$CONFIG;$SLOT;$ROW;$NOTE;$(basename "$CSV")" >> "$(res_file "$DIR")"
    log "Esito: $ROW"
}
STARTED=0
trap 'finish' EXIT
trap '' HUP
trap 'RESULT="interrotta"; REASON="arresto manuale"; exit 2' INT TERM

# ---------------- solo controlli: nessun comando, solo lettura
summary_controlli() {
    awk -F';' 'NR > 1 { n++; if ($22 == 1) ok++; if ($24 != "") { dn++; d += $24 } if ($26 != "") { pn++; p += $26 }
                         if ($27 != "") { gn++; if ($27 != "ko") { g++; gm += $27 } } if ($10 != "") { a = $10; if (amin == "" || a < amin) amin = a; if (amax == "" || a > amax) amax = a } }
        END { printf "campioni %d; pagina ok %d%%; display ok %s; PSG ok %s; ping ok %s (%s ms); AUX %s–%s\n", n, n ? 100 * ok / n : 0,
              dn ? int(100 * d / dn) "%" : "flag non trovato", pn ? int(100 * p / pn) "%" : "flag non trovato",
              gn ? int(100 * g / gn) "%" : "non configurato", g ? sprintf("%.1f", gm / g) : "-", amin, amax }' "$CSV" > "$DIR/.controlli_tmp"
    [ -f "$DIR/controlli.csv" ] || echo "data;cuid;riepilogo;file" > "$DIR/controlli.csv"
    echo "$(date '+%Y-%m-%d %H:%M');$CUID;$(cat "$DIR/.controlli_tmp");$(basename "$CSV")" >> "$DIR/controlli.csv"
    log "Controlli: $(cat "$DIR/.controlli_tmp")"; rm -f "$DIR/.controlli_tmp"
}
# diagnostica di livello 0: ping al computer dello schermo, AUX FAULT, dati AUX fermi (nessun comando)
summary_diagnosi() {
    L=$(awk -F';' -v fok="$AUX_FAULT_OK" -v pko="$DIAG_PING_KO_PCT" -v fpct="$DIAG_FAULT_PCT" -v ip="$DISPLAY_IP" '
        NR > 1 { n++; if ($22 == 1) ok++
            if ($27 != "") { gn++; if ($27 != "ko") { g++; gm += $27 } }
            if ($22 == 1) { fn++; v = toupper($18); if (v !~ fok) { fk++; fv = $18 } }
            k = $10 "|" $16 "|" $13 "|" $14 "|" $29; if ($22 == 1) { an++; if (k == "||||") av++; if (n1 == "") n1 = k; else if (k != n1) mv = 1 } }
        END { pk = gn ? int(100 * (gn - g) / gn) : ""; fp = fn ? int(100 * fk / fn) : ""; fermi = (an >= 6 && !mv) ? 1 : 0; vu = an ? int(100 * av / an) : ""
              m = ""; if (pk != "" && pk >= pko) m = m " ping_ko=" pk "%"; if (fp != "" && fp >= fpct) m = m " aux_fault=" fv; if (fermi) m = m " dati_aux_fermi"
              if (an == 0) m = m " pagina_non_leggibile"
              printf "%d;%s;%s;%s;%s;%s;%s;%s;%s;%s", n, (ip == "" ? "" : gn), pk, (g ? sprintf("%.1f", gm / g) : ""), fv, fp, fermi, vu, (m == "" ? "OK" : "STACCATO"), substr(m, 2) }' "$CSV")
    D="$DIR/diagnosi.csv"; [ -f "$D" ] || echo "data;cuid;campioni;ping_tentativi;ping_ko_pct;ping_ms;aux_fault;aux_fault_pct;aux_fermi;aux_vuoti_pct;esito;motivi;file" > "$D"
    echo "$(date '+%Y-%m-%d %H:%M');$CUID;$L;$(basename "$CSV")" >> "$D"
    echo "DIAGNOSI;$CUID;$L"
    [ -z "$DISPLAY_IP" ] && echo "Nota: indirizzo dello schermo non impostato (--ip-schermo o IP_SCHERMO): ping non eseguito."
}
if [ "$CONTROLLI" = 1 ]; then
    PH=0; PHNAME="Controlli"; [ "$DIAG" = 1 ] && PHNAME="Diagnosi"; T1=$(now); K=0
    while [ "$(now)" -lt $((T1 + DURATA_C)) ]; do sample; pace "$T1" $((T1 + DURATA_C)); done
    exit 0
fi

# ---------------- condizioni per partire
fail_start() { log "Non parte: $1"
    echo "$(date '+%Y-%m-%d %H:%M');$CUID;$CONFIG;$SLOT;$(row_vuota "non partita" "$1");$NOTE;$(basename "$CSV")" >> "$(res_file "$DIR")"; exit 0; }
[ -z "$PAGE" ] && fail_start "pagina web non leggibile"
[ "${FREE:-0}" -lt "$MIN_FREE_KB" ] && fail_start "spazio libero insufficiente (${FREE} kB)"
UPT=$(awk '{ printf "%d", $1 / 3600 }' /proc/uptime)
[ "$UPT" -lt "$MIN_UPTIME_H" ] && fail_start "colonnina accesa da $UPT h (servono $MIN_UPTIME_H h)"
if [ -f "$LAST" ] && [ "$REST_MIN" -gt 0 ]; then
    AGO=$(( ($(now) - $(cat "$LAST")) / 60 )); [ "$AGO" -lt "$REST_MIN" ] && fail_start "prova precedente finita $AGO min fa (pausa minima $REST_MIN)"
fi
USERST="$(get_value "USER STATUS")"
[ -n "$USER_OK" ] && [ "$USERST" != "$USER_OK" ] && fail_start "USER STATUS = $USERST invece di $USER_OK"
[ "$NS" -lt 1 ] && fail_start "nessun passo nel file di impostazioni"

# base stabile: AUX entro STABLE_TOL per STABLE_MIN minuti, attesa massima PRE_WAIT_MIN (campioni con passo 0)
if [ "$STABLE_MIN" -gt 0 ]; then
    T1=$(now); K=0; WIN=$((STABLE_MIN * 60)); OKW=0; SPAN=""; COV=0
    PW=$((PRE_WAIT_MIN * 60)); [ "$PW" -lt $((WIN + 3 * INTERVAL)) ] && PW=$((WIN + 3 * INTERVAL))   # la finestra deve poter riempirsi
    log "Attendo la base stabile: escursione di AUX (massimo − minimo) entro $STABLE_TOL °C per $STABLE_MIN min (attesa massima $((PW / 60)) min $((PW % 60)) s)"
    while [ "$(now)" -lt $((T1 + PW)) ]; do
        sample
        R=$(awk -F';' -v w="$WIN" -v tol="$STABLE_TOL" -v t="$(now)" 'NR > 1 && $10 != "" && $1 >= t - w { if (f == "") f = $1; if (lo == "" || $10 < lo) lo = $10; if (hi == "" || $10 > hi) hi = $10 }
            END { c = (f == "") ? 0 : t - f; printf "%d %s %d\n", (f != "" && c >= w * 0.95 && hi - lo <= tol) ? 1 : 0, (f == "" ? "" : sprintf("%.2f", hi - lo)), c }' "$CSV")
        set -- $R; OKW=$1; SPAN=$2; COV=$3
        [ "$OKW" = 1 ] && break
        pace "$T1" ""
    done
    if [ "$OKW" != 1 ]; then
        if [ "$COV" -lt $((WIN * 95 / 100)) ]; then fail_start "base non verificata: in $((PW / 60)) min di attesa massima non si è riempita la finestra di $STABLE_MIN min (letti $COV s; aumenta l'attesa massima)"
        else fail_start "base non stabile dopo $((PW / 60)) min: escursione di AUX ${SPAN:-?} °C negli ultimi $STABLE_MIN min, limite $STABLE_TOL °C"; fi
    fi
    log "Base stabile: escursione ${SPAN} °C negli ultimi $STABLE_MIN min."
fi

# ---------------- prova: guardiano, controllo termico sospeso, passi agganciati all'orario T0
SAFETY=1; STARTED=1
T0=$(now)
{ echo "T0=$T0"; echo "CUID='$CUID'"; echo "CONFIG='$SAFE_CFG'"; echo "SLOT='$SLOT'"; echo "NOTE=''"; echo "CSV='$CSV'"; } > "$INC"
WDS=$((TOTAL + WATCHDOG_MARGIN_MIN * 60))
( trap '' HUP; sleep "$WDS"; printf '07;00;00;01;00;01\n' > "$CMD"; sleep 3; printf '05;00;TempContEn;1\n' > "$CMD"
  echo "$(date '+%Y-%m-%d %H:%M:%S') guardiano a tempo: ripristino dopo $WDS s (prova non chiusa)" >> "$GLOG" ) </dev/null >/dev/null 2>&1 &
echo $! > "$BASE/.wd_pid"
log "Guardiano a tempo: ripristino automatico tra $((WDS / 60)) min se la prova non si chiude."
log "Limiti di sicurezza di questa sequenza: AUX TEMPERATURE ≤ $AUX_MAX °C, TEMPERATURE ≤ $TEMP_MAX °C, LED < $LED_MAX A per la validità."
echo "$(date '+%Y-%m-%d %H:%M') $CUID" > "$MARK"; DISABLED=1
send "05;00;TempContEn;0"; sleep 2
log "Passi: $NS, durata $((TOTAL / 60)) min, fine prevista alle $(date -d "@$((T0 + TOTAL))" '+%H:%M' 2>/dev/null)"
S=$T0
i=1
while [ "$i" -le "$NS" ]; do
    eval "PHNAME=\$SN_$i; PDUR=\$SS_$i; CH=\$SH_$i; CF=\$SF_$i"
    PH=$i; E=$((S + PDUR))
    echo "${SEQNAME:-conf};$i/$NS $PHNAME;$E" > "$PASSO"
    send "07;0$CF;0$CH;01;00;01"
    K=0
    while [ "$(now)" -lt "$E" ]; do sample; pace "$S" "$E"; done
    S=$E; i=$((i + 1))
done
exit 0
