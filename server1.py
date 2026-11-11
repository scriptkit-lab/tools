"""
Manutenzione 2.1 - diagnosi di flotta per colonnine WayPole/JuicePole 2.1.

Legge la pagina di stato di N colonnine in parallelo (HTTP, nessun impatto sul servizio), aggiunge
facoltativamente qualche controllo in sola lettura via SSH, applica le regole di regole.csv e produce
un report con guasti, componente, ricambio da usare, manuale e kit di ricambi da portare.
Procedure Remote/On-Field e registro della manutenzione come nella JPWeb diagnostic della 2.2.

Avvio: avvia.bat (oppure python server.py) e poi il browser su http://127.0.0.1:8770
I dati stanno in %USERPROFILE%\\Manut21\\dati.
"""
import concurrent.futures as cf
import csv
import datetime as dt
import html as htmlmod
import http.client
import io
import json
import os
import re
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

VERSION = "0.3.0"
APP = "Manutenzione 2.1"
BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("MANUT21_DATA") or Path.home() / "Manut21" / "dati")
PD_DATA = Path(os.environ.get("POLEDESK_DATA") or Path.home() / "PoleDesk" / "data")
F_STATIONS, F_RULES, F_PARTS = DATA / "colonnine.csv", DATA / "regole.csv", DATA / "ricambi.csv"
F_SETTINGS, F_CONFIG, F_PROCS, F_LOG = DATA / "impostazioni.json", DATA / "configurazione.json", DATA / "procedure.json", DATA / "registro.json"
RUNS_DIR, REPORT_DIR = DATA / "giri", DATA / "report"
for d in (DATA, RUNS_DIR, REPORT_DIR):
    d.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()

try:
    import paramiko
    import logging
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)
    PARAMIKO = getattr(paramiko, "__version__", "?")
except Exception:
    paramiko = None
    PARAMIKO = ""

SEV_ORDER = {"GUASTO": 3, "ATTENZIONE": 2, "INFO": 1, "OK": 0}

# ------------------------------------------------------------------ dati predefiniti (creati alla prima esecuzione)
DEFAULT_SETTINGS = {"ssh_attivo": True, "utente": "root", "password": "", "parallelo": 20,
                    "timeout_http": 8, "timeout_ssh": 12, "porta": 8770, "aggiorna_s": 10}

STATION_COLS = ["nome", "ip", "porta_web", "porta_ssh", "cuid_atteso", "note"]

RULE_COLS = ["id", "guasto", "variabile", "condizione", "valore", "e_se_variabile", "e_se_condizione", "e_se_valore",
             "gravita", "componente", "messaggio"]
# {n} = presa 0 e 1. Condizioni: = != < > <= >= fuori(min|max) contiene noncontiene manca. {valore} {valore2} nel messaggio.
DEFAULT_RULES = [
    ("PAG", "ALARM PAGE UNREACHABLE", "_PAGINA", "=", "NON RAGGIUNGIBILE", "", "", "", "GUASTO", "RETE / CPU", "Pagina di stato non raggiungibile"),
    ("CUID", "WARNING CUID MISMATCH", "_CUID_DIVERSO", "=", "SI", "", "", "", "ATTENZIONE", "", "All'IP risponde una colonnina diversa da quella attesa"),
    ("DSP", "ALARM DISPLAY COMMUNICATION", "DISPLAY STATUS", "!=", "CONNECTED", "", "", "", "GUASTO", "DISPLAY", "Schermo non comunica ({valore})"),
    ("AUX", "ALARM AUX COMMUNICATION", "AUX CONNECTION", "!=", "CONNECTED", "", "", "", "GUASTO", "SCHEDA AUX TESTA", "Scheda AUX della testa non comunica ({valore})"),
    ("AUXF", "ALARM AUX FAULT", "AUX FAULT", "!=", "00000000", "", "", "", "GUASTO", "SCHEDA AUX TESTA", "Scheda AUX segnala guasti ({valore})"),
    ("RFID", "ALARM RFID COMMUNICATION", "RFID CONNECTION", "!=", "CONNECTED", "", "", "", "GUASTO", "LETTORE RFID", "Lettore RFID non comunica ({valore})"),
    ("MDM", "WARNING MODEM", "MODEM STATUS", "!=", "CONNECTED", "", "", "", "ATTENZIONE", "MODEM", "Modem non connesso ({valore})"),
    ("CSQ", "WARNING MODEM SIGNAL", "MODEM SIGNAL QUALITY", "fuori", "10|31", "MODEM STATUS", "=", "CONNECTED", "ATTENZIONE", "ANTENNA MODEM", "Segnale modem debole ({valore})"),
    ("AC", "ALARM AC POWER", "AC POWER", "!=", "ONLINE", "", "", "", "GUASTO", "ALIMENTAZIONE", "Rete non presente ({valore})"),
    ("V24", "WARNING MAIN POWER", "MAIN POWER VOLTAGE", "fuori", "23|26", "", "", "", "ATTENZIONE", "ALIMENTATORE 24V", "Tensione principale fuori range ({valore})"),
    ("VSEC", "WARNING SEC POWER", "SEC POWER VOLTAGE", "fuori", "12|14.5", "", "", "", "ATTENZIONE", "ALIMENTATORE SECONDARIO", "Tensione secondaria fuori range ({valore})"),
    ("SCAP", "WARNING SUPERCAP", "SUPERCAP VOLTAGE", "<", "11.5", "", "", "", "ATTENZIONE", "SUPERCAP", "Supercap bassa ({valore})"),
    ("A12", "WARNING AUX SUPPLY 12V", "AUX SUPPLY 12V", "fuori", "11.4|13.2", "", "", "", "ATTENZIONE", "SCHEDA AUX TESTA", "12 V scheda AUX fuori range ({valore})"),
    ("A5", "WARNING AUX SUPPLY 5V", "AUX SUPPLY 5V", "fuori", "4.7|5.3", "", "", "", "ATTENZIONE", "SCHEDA AUX TESTA", "5 V scheda AUX fuori range ({valore})"),
    ("AUSB", "WARNING AUX SUPPLY USB", "AUX SUPPLY USB", "fuori", "4.6|5.3", "", "", "", "ATTENZIONE", "SCHEDA AUX TESTA", "USB scheda AUX fuori range ({valore})"),
    ("TAMP", "WARNING ANTITAMPER", "ANTITAMPER OK STATUS", "!=", "CLOSED", "", "", "", "ATTENZIONE", "ANTITAMPER", "Antitamper aperto ({valore})"),
    ("TCPU", "WARNING CPU TEMPERATURE", "CPU-TEMPERATURE", ">", "85", "", "", "", "ATTENZIONE", "CPU", "Temperatura CPU alta ({valore})"),
    ("TBRD", "WARNING BOARD TEMPERATURE", "TEMPERATURE", ">", "70", "", "", "", "ATTENZIONE", "CPU", "Temperatura scheda alta ({valore})"),
    ("TAUX", "WARNING HEAD TEMPERATURE", "AUX TEMPERATURE", ">", "70", "", "", "", "ATTENZIONE", "SCHEDA AUX TESTA", "Temperatura testa alta ({valore})"),
    ("MCB{n}", "ALARM BREAKER OPEN", "SOCKET {n} POWER BREAKER", "!=", "CLOSE", "", "", "", "GUASTO", "MAGNETOTERMICO PRESA {n}", "Magnetotermico aperto"),
    ("RCCB{n}", "ALARM RCCB OPEN", "SOCKET {n} DIFF.BREAKER", "!=", "CLOSE", "", "", "", "GUASTO", "DIFFERENZIALE PRESA {n}", "Differenziale aperto"),
    ("CTT{n}", "ALARM CONTACTOR FAILURE", "SOCKET {n} POWER SWITCH", "=", "CLOSE", "EVSE TASK {n} STATUS", "=", "IDLE", "GUASTO", "CONTATTORE PRESA {n}", "Contattore chiuso a riposo: possibile contatto incollato"),
    ("CP{n}", "WARNING CONTROL PILOT", "SOCKET {n} CONTROL PILOT", "fuori", "11.4|12.6", "SOCKET {n} PROXIMITY", ">=", "65535", "ATTENZIONE", "SCHEDA PRESA {n}", "Control Pilot a vuoto fuori range ({valore})"),
    ("EVS{n}", "ALARM EVSE FAULT", "EVSE TASK {n} STATUS", "contiene", "FAULT", "", "", "", "GUASTO", "PRESA {n}", "Presa in guasto ({valore})"),
    ("EVE{n}", "ALARM EVSE ERROR", "EVSE TASK {n} STATUS", "contiene", "ERROR", "", "", "", "GUASTO", "PRESA {n}", "Presa in errore ({valore})"),
    ("SCP", "ALARM CP_MAIN STOPPED", "_SSH CP_MAIN", "!=", "active", "_SSH", "=", "OK", "GUASTO", "SOFTWARE", "CP_main non attivo ({valore})"),
    ("SAUX", "ALARM PS4GAUX STOPPED", "_SSH PS4GAUX", "!=", "active", "_SSH", "=", "OK", "GUASTO", "SOFTWARE", "PS4GAux non attivo ({valore})"),
    ("SRF", "WARNING RFID SERVICE", "_SSH RFID", "!=", "active", "_SSH", "=", "OK", "ATTENZIONE", "SOFTWARE", "Servizio RFID non attivo ({valore})"),
    ("SCM", "WARNING CM_MAIN", "_SSH CM_MAIN", "!=", "active", "_SSH", "=", "OK", "ATTENZIONE", "SOFTWARE", "CM_main non attivo ({valore})"),
    ("DSK", "WARNING DISK", "_SSH DISCO %", ">", "90", "", "", "", "ATTENZIONE", "MEMORIA", "Disco quasi pieno ({valore}%)"),
    ("LOG", "INFO CP LOG ERRORS", "_SSH ERRORI CP.LOG", ">", "20", "", "", "", "INFO", "", "{valore} righe ERROR nelle ultime 500 di CP.log"),
    ("SSHE", "INFO SSH", "_SSH", "=", "ERRORE", "_SSH NOTA", "!=", "", "INFO", "", "Controlli SSH non riusciti: {valore2}"),
]
DEFAULT_GUASTO = {r[0]: r[1] for r in DEFAULT_RULES}

PART_COLS = ["componente", "codice", "descrizione", "foto", "note"]

# Procedure di manutenzione per componente ({n} = presa). Passi Remote: WEB (rilegge la pagina di stato
# e confronta), ASK (domanda al tecnico/operatore da remoto), WAIT (secondi). On-Field: ricambi e domande.
DEFAULT_PROCS = {
    "DISPLAY": {"remote": [
        {"titolo": "Rileggi DISPLAY STATUS dalla pagina di stato", "tipo": "WEB", "variabile": "DISPLAY STATUS", "atteso": "CONNECTED"},
        {"titolo": "Verifica alimentazione pannello (AUX PANEL POWER)", "tipo": "WEB", "variabile": "AUX PANEL POWER", "atteso": "ON"},
        {"titolo": "Attendi 20 s e rileggi", "tipo": "WAIT", "secondi": 20},
        {"titolo": "Rileggi DISPLAY STATUS", "tipo": "WEB", "variabile": "DISPLAY STATUS", "atteso": "CONNECTED"}],
        "onfield": [{"componente": "CAVO DISPLAY", "domanda": "Il cavo display è inserito e integro?"},
                    {"componente": "DISPLAY", "domanda": "Dopo il riaggancio lo schermo si accende e comunica?"}]},
    "SCHEDA AUX TESTA": {"remote": [
        {"titolo": "Rileggi AUX CONNECTION", "tipo": "WEB", "variabile": "AUX CONNECTION", "atteso": "CONNECTED"},
        {"titolo": "Rileggi AUX FAULT", "tipo": "WEB", "variabile": "AUX FAULT", "atteso": "00000000"},
        {"titolo": "Controlla 12 V scheda AUX", "tipo": "WEB", "variabile": "AUX SUPPLY 12V", "atteso": "fuori:11.4|13.2"}],
        "onfield": [{"componente": "VENTOLA TESTA", "domanda": "La ventola della testa gira?"},
                    {"componente": "SCHEDA AUX TESTA", "domanda": "Sostituita la scheda AUX, AUX CONNECTION torna CONNECTED?"}]},
    "MAGNETOTERMICO PRESA {n}": {"remote": [
        {"titolo": "Rileggi SOCKET {n} POWER BREAKER", "tipo": "WEB", "variabile": "SOCKET {n} POWER BREAKER", "atteso": "CLOSE"},
        {"titolo": "Corrente a riposo nulla (METER {n} CURRENT L1)", "tipo": "WEB", "variabile": "METER {n} CURRENT L1", "atteso": "fuori:-0.5|0.5"}],
        "onfield": [{"componente": "MAGNETOTERMICO PRESA {n}", "domanda": "Il magnetotermico si richiude e resta chiuso?"},
                    {"componente": "CONTATTORE PRESA {n}", "domanda": "I contatti sono privi di segni di bruciatura?"}]},
    "DIFFERENZIALE PRESA {n}": {"remote": [
        {"titolo": "Rileggi SOCKET {n} DIFF.BREAKER", "tipo": "WEB", "variabile": "SOCKET {n} DIFF.BREAKER", "atteso": "CLOSE"}],
        "onfield": [{"componente": "DIFFERENZIALE PRESA {n}", "domanda": "Il differenziale si richiude e supera il test?"}]},
    "CONTATTORE PRESA {n}": {"remote": [
        {"titolo": "Rileggi SOCKET {n} POWER SWITCH a riposo", "tipo": "WEB", "variabile": "SOCKET {n} POWER SWITCH", "atteso": "OPEN"},
        {"titolo": "Rileggi EVSE TASK {n} STATUS", "tipo": "WEB", "variabile": "EVSE TASK {n} STATUS", "atteso": "IDLE"}],
        "onfield": [{"componente": "CONTATTORE PRESA {n}", "domanda": "Il contattore si apre correttamente senza bobina alimentata?"}]},
    "MODEM": {"remote": [
        {"titolo": "Rileggi MODEM STATUS", "tipo": "WEB", "variabile": "MODEM STATUS", "atteso": "CONNECTED"},
        {"titolo": "Attendi 60 s e rileggi", "tipo": "WAIT", "secondi": 60},
        {"titolo": "Rileggi MODEM STATUS", "tipo": "WEB", "variabile": "MODEM STATUS", "atteso": "CONNECTED"}],
        "onfield": [{"componente": "ANTENNA MODEM", "domanda": "L'antenna è collegata e integra?"},
                    {"componente": "MODEM", "domanda": "Il modem si registra in rete dopo la sostituzione?"}]},
    "LETTORE RFID": {"remote": [
        {"titolo": "Rileggi RFID CONNECTION", "tipo": "WEB", "variabile": "RFID CONNECTION", "atteso": "CONNECTED"},
        {"titolo": "Verifica alimentazione RFID (AUX RFID POWER)", "tipo": "WEB", "variabile": "AUX RFID POWER", "atteso": "ON"}],
        "onfield": [{"componente": "LETTORE RFID", "domanda": "Avvicinando la tessera di prova il lettore risponde?"}]},
    "ANTITAMPER": {"remote": [
        {"titolo": "Rileggi ANTITAMPER OK STATUS", "tipo": "WEB", "variabile": "ANTITAMPER OK STATUS", "atteso": "CLOSED"}],
        "onfield": [{"componente": "ANTITAMPER", "domanda": "Il pannello è chiuso correttamente e il microinterruttore è integro?"}]},
}

EMPTY_CFG = {"names": {}, "labels": {}, "msgs": {}, "manuals": {}, "codes": {}, "faultParts": {}}


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cols)
        w.writerows(rows)


def read_csv(path):
    if not path.exists():
        return []
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    first = raw.splitlines()[0] if raw.strip() else ""
    delim = ";" if first.count(";") >= first.count(",") else ","
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(io.StringIO(raw), delimiter=delim)]


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(default))


def save_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def expand_n(s):
    return [s.replace("{n}", n) for n in ("0", "1")] if "{n}" in s else [s]


def ensure_files():
    if not F_SETTINGS.exists():
        save_json(F_SETTINGS, DEFAULT_SETTINGS)
    if not F_RULES.exists():
        write_csv(F_RULES, RULE_COLS, DEFAULT_RULES)
    if not F_PARTS.exists():
        comps = set()
        for r in DEFAULT_RULES:
            comps.update(expand_n(r[9]) if r[9] else [])
        for p in DEFAULT_PROCS.values():
            for x in p["onfield"]:
                comps.update(expand_n(x["componente"]))
        write_csv(F_PARTS, PART_COLS, [(c, "", "", "", "") for c in sorted(comps)])
    if not F_STATIONS.exists():
        write_csv(F_STATIONS, STATION_COLS, [])
    if not F_PROCS.exists():
        save_json(F_PROCS, DEFAULT_PROCS)
    if not F_CONFIG.exists():
        save_json(F_CONFIG, EMPTY_CFG)
    if not F_LOG.exists():
        save_json(F_LOG, [])


def settings():
    return {**DEFAULT_SETTINGS, **load_json(F_SETTINGS, {})}


def config():
    return {**json.loads(json.dumps(EMPTY_CFG)), **load_json(F_CONFIG, {})}


def poledesk_settings():
    return load_json(PD_DATA / "settings.json", {})


def stations():
    out = []
    for i, r in enumerate(read_csv(F_STATIONS)):
        if r.get("ip"):
            r["id"] = f"{r['ip']}:{r.get('porta_web') or 80}#{i}"
            out.append(r)
    return out


def station(sid):
    s = next((x for x in stations() if x["id"] == sid), None)
    if not s:
        raise KeyError("colonnina non trovata")
    return s


def import_poledesk():
    pd = load_json(PD_DATA / "stations.json", None)
    if pd is None:
        raise ValueError(f"anagrafica di PoleDesk non trovata in {PD_DATA}")
    cur = read_csv(F_STATIONS)
    have = {r.get("ip") for r in cur}
    added = 0
    for s in pd:
        ip = (s.get("host") or "").strip()
        if ip and ip not in have:
            cur.append({"nome": s.get("name", ""), "ip": ip, "porta_ssh": str(s.get("port") or ""), "note": "da PoleDesk"})
            have.add(ip)
            added += 1
    write_csv(F_STATIONS, STATION_COLS, [[r.get(c, "") for c in STATION_COLS] for r in cur])
    return added


def parts():
    return {r.get("componente", "").upper(): r for r in read_csv(F_PARTS) if r.get("componente")}


def procedures():
    return load_json(F_PROCS, DEFAULT_PROCS)


def save_config(body):
    cfg = config()
    for k in ("names", "labels", "msgs", "manuals", "faultParts"):
        if isinstance(body.get(k), dict):
            cfg[k] = {str(a): str(b) for a, b in body[k].items() if str(b).strip()}
    codes = body.get("codes") if isinstance(body.get("codes"), dict) else None
    cfg["codes"] = {}
    save_json(F_CONFIG, cfg)
    if codes is not None:    # i codici ricambio vivono in ricambi.csv, modificabile anche con Excel
        rows = read_csv(F_PARTS)
        idx = {r.get("componente", "").upper(): r for r in rows}
        for comp, code in codes.items():
            comp = comp.upper().strip()
            if not comp:
                continue
            if comp in idx:
                idx[comp]["codice"] = str(code).strip()
            elif str(code).strip():
                r = {"componente": comp, "codice": str(code).strip()}
                rows.append(r)
                idx[comp] = r
        write_csv(F_PARTS, PART_COLS, [[r.get(c, "") for c in PART_COLS] for r in rows])
    return cfg


# ------------------------------------------------------------------ pagina di stato
def _cell_text(c):
    t = re.sub(r"(?is)<br\s*/?>", " ", c)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmlmod.unescape(t).replace("\xa0", " ")).strip()


def parse_page(text):
    """Coppie nome/valore dalla pagina 'Informazioni Diagnostiche' (due coppie descrizione|valore per riga).
    Restituisce anche l'ordine delle righe, per mostrare la pagina come l'originale."""
    out, layout = {}, []

    def add(k, v):
        k = k.strip().rstrip(":").strip()
        if not k or k.upper() in ("DESCRIZIONE", "VALORE") or len(k) > 80:
            return ""
        base, i = k, 2
        while k in out:
            k, i = f"{base} #{i}", i + 1
        out[k] = v.strip()
        return k

    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", text):
        cells = [_cell_text(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
        keys = [add(cells[j], cells[j + 1]) for j in range(0, len(cells) - 1, 2)]
        if any(keys):
            layout.append(keys)
    if len(out) < 5:
        out, layout = {}, []
        cells = [c for c in (_cell_text(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", text)) if c.upper() not in ("DESCRIZIONE", "VALORE")]
        keys = [add(cells[j], cells[j + 1]) for j in range(0, len(cells) - 1, 2)]
        layout = [keys[i:i + 2] for i in range(0, len(keys), 2)]
    title = re.search(r"(?is)<a[^>]*>\s*(MASTER|SLAVE[^<]*)\s*</a>", text)
    return out, (title.group(1).strip() if title else ""), layout


def num(v):
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group().replace(",", ".")) if m else None


def http_get(host, port, path="/", timeout=8):
    c = http.client.HTTPConnection(host, int(port or 80), timeout=timeout)
    c.request("GET", path, headers={"User-Agent": "Manut21"})
    r = c.getresponse()
    return r.status, r.read()


def read_page(st, cfg):
    code, body = http_get(st["ip"], st.get("porta_web") or 80, "/", int(cfg.get("timeout_http") or 8))
    text = body.decode("utf-8", "replace")
    if code != 200:
        raise OSError(f"HTTP {code}")
    vals, title, layout = parse_page(text)
    if len(vals) < 5:
        raise ValueError("la pagina non contiene la tabella attesa")
    return text, vals, title, layout


# ------------------------------------------------------------------ controlli SSH (sola lettura)
SSH_CMD = ("systemctl is-active CP_main PS4GAux rfid CM_main 2>&1; echo ---; df -P / | tail -1; echo ---; "
           "cut -d' ' -f1 /proc/uptime; echo ---; tail -n 500 /crve/log/CP.log 2>/dev/null | grep -c ERROR; echo ---; "
           "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null; echo ---; cat /sys/class/net/eth0/address")


def ssh_checks(st, cfg):
    if not paramiko:
        return {"_SSH": "NON DISPONIBILE", "_SSH NOTA": "libreria paramiko assente sul PC"}
    pd = poledesk_settings()
    pw = cfg.get("password") or pd.get("default_password") or ""
    user = cfg.get("utente") or pd.get("default_username") or "root"
    if not pw:
        return {"_SSH": "NON CONFIGURATO", "_SSH NOTA": "password SSH non impostata"}
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    t = int(cfg.get("timeout_ssh") or 12)
    try:
        c.connect(st["ip"], port=int(st.get("porta_ssh") or 22), username=user, password=pw, look_for_keys=False,
                  allow_agent=False, timeout=t, banner_timeout=t, auth_timeout=t)
        _, so, _ = c.exec_command(SSH_CMD, timeout=t + 10)
        out = so.read(200_000).decode("utf-8", "replace")
    except Exception as e:
        return {"_SSH": "ERRORE", "_SSH NOTA": str(e)[:200]}
    finally:
        try:
            c.close()
        except Exception:
            pass
    p = [x.strip() for x in out.split("---")]
    res = {"_SSH": "OK"}
    for name, val in zip(["CP_MAIN", "PS4GAUX", "RFID", "CM_MAIN"], (p[0].splitlines() + ["?"] * 4)[:4] if p else ["?"] * 4):
        res[f"_SSH {name}"] = val.strip()
    if len(p) > 1:
        m = re.search(r"(\d+)%", p[1])
        res["_SSH DISCO %"] = m.group(1) if m else ""
    if len(p) > 2 and num(p[2]) is not None:
        res["_SSH UPTIME ORE"] = f"{num(p[2]) / 3600:.1f}"
    if len(p) > 3:
        res["_SSH ERRORI CP.LOG"] = p[3].splitlines()[0] if p[3] else ""
    if len(p) > 4 and num(p[4]) is not None:
        res["_SSH CPU °C"] = f"{num(p[4]) / 1000:.1f}"
    if len(p) > 5:
        res["_SSH MAC"] = p[5].splitlines()[0] if p[5] else ""
    return res


# ------------------------------------------------------------------ regole
def rules():
    out = []
    for r in read_csv(F_RULES):
        if not r.get("variabile") or r.get("id", "").startswith("#"):
            continue
        tid = r.get("id", "")
        guasto = r.get("guasto") or DEFAULT_GUASTO.get(tid) or tid
        ns = ("0", "1") if "{n}" in json.dumps(r) else ("",)
        for n in ns:
            x = {k: v.replace("{n}", n) for k, v in r.items()}
            x["_guasto"], x["_n"], x["_tid"] = guasto, n, tid
            out.append(x)
    return out


def fault_list():
    """Un elemento per guasto (le regole con {n} valgono per entrambe le prese)."""
    seen, out = set(), []
    for r in read_csv(F_RULES):
        if not r.get("variabile") or r.get("id", "").startswith("#"):
            continue
        g = r.get("guasto") or DEFAULT_GUASTO.get(r.get("id", "")) or r.get("id", "")
        if g in seen:
            continue
        seen.add(g)
        out.append({"guasto": g, "componente": r.get("componente", ""), "messaggio": r.get("messaggio", ""),
                    "gravita": (r.get("gravita") or "").upper(), "presa": "{n}" in json.dumps(r)})
    return out


def check(op, val, ref):
    op = (op or "").strip().lower()
    if op == "manca":
        return val in (None, "")
    if val is None:
        return False
    sv, sr = str(val).strip().upper(), str(ref).strip().upper()
    if op in ("=", "=="):
        return sv == sr
    if op == "!=":
        return sv != sr
    if op == "contiene":
        return sr in sv
    if op == "noncontiene":
        return sr not in sv
    nv = num(val)
    if nv is None:
        return False
    if op == "fuori":
        lo, hi = (num(x) for x in (str(ref).split("|") + [""])[:2])
        return (lo is not None and nv < lo) or (hi is not None and nv > hi)
    nr = num(ref)
    if nr is None:
        return False
    return {"<": nv < nr, ">": nv > nr, "<=": nv <= nr, ">=": nv >= nr}.get(op, False)


def evaluate(vals):
    found, marks = [], {}
    pmap, cfg = parts(), config()
    for r in rules():
        var = r["variabile"]
        if var not in vals and r.get("condizione", "").lower() != "manca":
            continue
        v = vals.get(var)
        if not check(r.get("condizione"), v, r.get("valore", "")):
            continue
        v2 = None
        if r.get("e_se_variabile"):
            v2 = vals.get(r["e_se_variabile"])
            if not check(r.get("e_se_condizione"), v2, r.get("e_se_valore", "")):
                continue
        sev = (r.get("gravita") or "ATTENZIONE").upper()
        comp, g, n = r.get("componente", ""), r["_guasto"], r["_n"]
        msg = cfg["msgs"].get(g) or r.get("messaggio") or var
        msg = (f"Presa {n}: " if n else "") + msg.replace("{valore}", str(v)).replace("{valore2}", str(v2 or ""))
        code = cfg["faultParts"].get(g) or pmap.get(comp.upper(), {}).get("codice", "")
        p = pmap.get(comp.upper(), {})
        found.append({"id": r.get("id", ""), "guasto": g, "n": n, "gravita": sev, "componente": comp, "variabile": var, "valore": v,
                      "messaggio": msg, "ricambio": code, "descrizione_ricambio": p.get("descrizione", ""),
                      "manuale": cfg["manuals"].get(g, "")})
        if SEV_ORDER.get(sev, 1) > SEV_ORDER.get(marks.get(var, "OK"), 0):
            marks[var] = sev
    found.sort(key=lambda f: -SEV_ORDER.get(f["gravita"], 1))
    overall = found[0]["gravita"] if found and found[0]["gravita"] != "INFO" else "OK"
    return overall, found, marks


def proc_key(comp):
    pr = procedures()
    if comp in pr:
        return comp, ""
    m = re.match(r"^(.*) PRESA ([01])$", comp)
    if m and f"{m.group(1)} PRESA {{n}}" in pr:
        return f"{m.group(1)} PRESA {{n}}", m.group(2)
    return None, ""


def code_desc(code):
    for r in read_csv(F_PARTS):
        if r.get("codice") == code:
            return r.get("descrizione") or r.get("componente", "")
    return ""


def kit(results):
    """Ricambi da portare (guasti) e di scorta (attenzioni e ricambi eventuali delle procedure On-Field)."""
    need, spare = {}, {}
    pmap, pr = parts(), procedures()
    for r in results:
        who = r.get("nome") or r.get("ip")
        for a in r.get("anomalie", []):
            if a["gravita"] == "INFO":
                continue
            codes = [c.strip() for c in re.split(r"[,;]", a.get("ricambio") or "") if c.strip()]
            target = need if a["gravita"] == "GUASTO" else spare
            for c in codes:
                x = target.setdefault(c, {"codice": c, "descrizione": code_desc(c), "quantita": 0, "per": []})
                x["quantita"] += 1
                x["per"].append(f"{who}: {a['messaggio']}")
            k, n = proc_key(a.get("componente", ""))
            if k:
                for o in pr[k].get("onfield", []):
                    comp = o["componente"].replace("{n}", n or "0")
                    c = pmap.get(comp.upper(), {}).get("codice", "")
                    if c and c not in codes:
                        x = spare.setdefault(c, {"codice": c, "descrizione": code_desc(c), "quantita": 1, "per": []})
                        if f"{who}: eventuale" not in x["per"]:
                            x["per"].append(f"{who}: eventuale")
    for c in need:
        spare.pop(c, None)
    return {"necessari": list(need.values()), "scorta": list(spare.values())}


# ------------------------------------------------------------------ storico, trend, confronto con la flotta, cruscotto
TREND_VARS = ["SUPERCAP VOLTAGE", "MAIN POWER VOLTAGE", "SEC POWER VOLTAGE", "AUX SUPPLY 12V", "AUX SUPPLY 5V", "AUX SUPPLY USB",
              "TEMPERATURE", "CPU-TEMPERATURE", "AUX TEMPERATURE", "MODEM SIGNAL QUALITY", "AUX LEDS CURRENT",
              "SOCKET 0 CONTROL PILOT", "SOCKET 1 CONTROL PILOT", "_SSH DISCO %", "_SSH ERRORI CP.LOG"]
ENERGY_VARS = ["METER 0 TOTAL ACTIVE ENERGY", "METER 1 TOTAL ACTIVE ENERGY"]
# differenza minima perché un valore sia "fuori dalla flotta" (unità della pagina)
OUTLIER_VARS = {"CPU-TEMPERATURE": 8, "TEMPERATURE": 8, "AUX TEMPERATURE": 8, "MODEM SIGNAL QUALITY": 6, "MAIN POWER VOLTAGE": 0.4,
                "SEC POWER VOLTAGE": 0.4, "SUPERCAP VOLTAGE": 0.4, "AUX SUPPLY 12V": 0.4, "AUX SUPPLY 5V": 0.2, "AUX SUPPLY USB": 0.2,
                "AUX LEDS CURRENT": 0.8}
_HCACHE = {"key": None, "data": None}


def station_key(r):
    return r.get("cuid") or f"{r.get('ip')}:{r.get('porta_web') or 80}"


def run_time(name):
    try:
        return dt.datetime.strptime(name, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def history():
    """Serie per colonnina (chiave CUID) da tutti i giri salvati: valori numerici, esito, guasti."""
    runs = sorted(p for p in RUNS_DIR.iterdir() if p.is_dir() and run_time(p.name))
    key = (len(runs), runs[-1].name if runs else "", sum(1 for _ in runs[-1].glob("*.json")) if runs else 0)
    if _HCACHE["key"] == key:
        return _HCACHE["data"]
    data = {}
    for d in runs:
        t = run_time(d.name)
        for p in d.glob("*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            v = r.get("valori", {}) if r.get("valori", {}).get("_PAGINA") == "OK" else {}
            pt = {"t": t.isoformat(timespec="minutes"), "esito": r.get("esito"),
                  "guasti": sorted({f"{a.get('guasto')}|{a.get('n','')}" for a in r.get("anomalie", []) if a.get("gravita") in ("GUASTO", "ATTENZIONE")}),
                  "v": {k: tnum(k, v[k]) for k in TREND_VARS + ENERGY_VARS if k in v and tnum(k, v[k]) is not None}}
            data.setdefault(station_key(r), []).append(pt)
    _HCACHE.update(key=key, data=data)
    return data


def tnum(var, val):
    x = num(val)
    if var == "MODEM SIGNAL QUALITY" and x == 99:    # 99 = segnale non disponibile
        return None
    return x


def _limits(var):
    lo = hi = None
    for r in rules():
        if r.get("variabile") != var:
            continue
        op, ref = (r.get("condizione") or "").lower(), r.get("valore", "")
        if op == "fuori":
            a, b = (num(x) for x in (ref.split("|") + [""])[:2])
            lo, hi = a if a is not None else lo, b if b is not None else hi
        elif op in ("<", "<="):
            lo = num(ref)
        elif op in (">", ">="):
            hi = num(ref)
    return lo, hi


def _days(a, b):
    return (dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds() / 86400


def station_insights(r, pts, fleet_daily_kwh):
    out, now = [], pts[-1]["t"] if pts else None
    if not pts:
        return out
    # da quanto tempo è presente ogni anomalia attuale
    for a in r.get("anomalie", []):
        if a.get("gravita") not in ("GUASTO", "ATTENZIONE"):
            continue
        g = f"{a.get('guasto')}|{a.get('n','')}"
        first = None
        for p in reversed(pts):
            if g in p["guasti"]:
                first = p["t"]
            else:
                break
        if first:
            d = _days(first, now)
            if d >= 0.5:
                out.append({"tipo": "persistenza", "gravita": a["gravita"], "variabile": a.get("variabile", ""), "giorni": round(d, 1),
                            "messaggio": f"{a['messaggio']}: presente da {d:.0f} giorni" if d >= 1.5 else f"{a['messaggio']}: presente da {d * 24:.0f} ore"})
    # trend verso le soglie (ultimi 30 giorni)
    recent = [p for p in pts if _days(p["t"], now) <= 30]
    for var in TREND_VARS:
        xs = [(_days(recent[0]["t"], p["t"]), p["v"][var]) for p in recent if var in p["v"]]
        if len(xs) < 4 or xs[-1][0] - xs[0][0] < 2:
            continue
        n = len(xs)
        mx, my = sum(x for x, _ in xs) / n, sum(y for _, y in xs) / n
        den = sum((x - mx) ** 2 for x, _ in xs)
        if not den:
            continue
        slope = sum((x - mx) * (y - my) for x, y in xs) / den
        cur = xs[-1][1]
        lo, hi = _limits(var)
        eta = None
        if slope < 0 and lo is not None and cur > lo:
            eta, lim = (cur - lo) / -slope, lo
        elif slope > 0 and hi is not None and cur < hi:
            eta, lim = (hi - cur) / slope, hi
        if eta is not None and eta <= 45 and abs(slope) * 30 >= 0.02 * max(abs(cur), 1):
            out.append({"tipo": "trend", "gravita": "ATTENZIONE" if eta <= 15 else "INFO", "variabile": var, "giorni": round(eta),
                        "pendenza": round(slope, 4), "messaggio": f"{var} {'in calo' if slope < 0 else 'in aumento'} ({slope:+.3g}/giorno): raggiunge {lim:g} tra circa {eta:.0f} giorni"})
    # nessuna energia erogata mentre il resto della flotta lavora
    if fleet_daily_kwh > 1:
        for var in ENERGY_VARS:
            series = [(p["t"], p["v"][var]) for p in pts if var in p["v"]]
            if len(series) < 3:
                continue
            cur, since = series[-1][1], series[-1][0]
            for t, v in reversed(series):
                if v == cur:
                    since = t
                else:
                    break
            d = _days(since, now)
            if d >= 3:
                presa = var.split()[1]
                out.append({"tipo": "inattiva", "gravita": "ATTENZIONE", "variabile": var, "giorni": round(d, 1),
                            "messaggio": f"Presa {presa}: nessuna energia erogata da {d:.0f} giorni (la flotta eroga in media {fleet_daily_kwh:.0f} kWh/giorno per colonnina)"})
    return out


def fleet_outliers(results):
    out = {}
    for var, min_abs in OUTLIER_VARS.items():
        vals = [(r, tnum(var, r["valori"][var])) for r in results if var in r.get("valori", {}) and tnum(var, r["valori"][var]) is not None]
        if len(vals) < 5:
            continue
        xs = sorted(v for _, v in vals)
        med = xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
        dev = sorted(abs(v - med) for v in xs)
        mad = dev[len(dev) // 2] * 1.4826
        scale = max(mad, min_abs / 3)
        for r, v in vals:
            if abs(v - med) >= min_abs and abs(v - med) / scale >= 3.5:
                out.setdefault(r["id"], []).append({"tipo": "flotta", "gravita": "INFO", "variabile": var, "valore": v, "mediana": med,
                                                    "messaggio": f"{var} {v:g}: {v - med:+.3g} rispetto alla mediana della flotta ({med:g})"})
    return out


def latest_results():
    runs = sorted((p for p in RUNS_DIR.iterdir() if p.is_dir() and run_time(p.name)), reverse=True)
    for d in runs:
        rs = [json.loads(p.read_text(encoding="utf-8")) for p in d.glob("*.json")]
        if rs:
            return d.name, rs
    return "", []


def dashboard_data(results=None):
    stamp = ""
    if not results:
        stamp, results = latest_results()
    results = [r for r in results if not str(r.get("id", "")).startswith("file:")]
    hist = history()
    # energia della flotta: ultime 24 h e 7 giorni
    e24 = e7 = 0.0
    per_station_daily = []
    for r in results:
        pts = hist.get(station_key(r), [])
        if len(pts) < 2:
            continue
        now = pts[-1]["t"]
        tot = lambda p: sum(p["v"].get(v, 0) for v in ENERGY_VARS)
        last = tot(pts[-1])
        p24 = next((p for p in pts if _days(p["t"], now) <= 1), pts[0])
        p7 = next((p for p in pts if _days(p["t"], now) <= 7), pts[0])
        e24 += max(0, last - tot(p24)) / 1000
        span = max(_days(p7["t"], now), 0.01)
        e7 += max(0, last - tot(p7)) / 1000
        per_station_daily.append(max(0, last - tot(p7)) / 1000 / span)
    fleet_daily = sorted(per_station_daily)[len(per_station_daily) // 2] if per_station_daily else 0
    outl = fleet_outliers([r for r in results if r.get("valori", {}).get("_PAGINA") == "OK"])
    insights = {}
    for r in results:
        ins = station_insights(r, hist.get(station_key(r), []), fleet_daily) + outl.get(r["id"], [])
        if ins:
            insights[r["id"]] = sorted(ins, key=lambda x: (-SEV_ORDER.get(x["gravita"], 0), x.get("giorni") or 0))
    # disponibilità delle prese adesso e della flotta negli ultimi 7 giorni
    socks = avail = charging = 0
    blocking = ("ALIMENTAZIONE", "RETE / CPU", "SOFTWARE")
    for r in results:
        v = r.get("valori", {})
        for n in ("0", "1"):
            socks += 1
            if v.get("_PAGINA") != "OK":
                continue
            if any(a["gravita"] == "GUASTO" and (a.get("n") == n or (not a.get("n") and a.get("componente") in blocking)) for a in r.get("anomalie", [])):
                continue
            avail += 1
            charging += 1 if "CHARG" in str(v.get(f"EVSE TASK {n} STATUS", "")).upper() else 0
    ok7 = tot7 = 0
    for r in results:
        for p in hist.get(station_key(r), []):
            if _days(p["t"], dt.datetime.now().isoformat(timespec="minutes")) <= 7:
                tot7 += 1
                ok7 += 1 if p["esito"] in ("OK", "ATTENZIONE") else 0
    comps = {}
    for r in results:
        for a in r.get("anomalie", []):
            if a["gravita"] in ("GUASTO", "ATTENZIONE") and a.get("componente"):
                c = comps.setdefault(a["componente"], {"componente": a["componente"], "GUASTO": 0, "ATTENZIONE": 0})
                c[a["gravita"]] += 1
    k = kit(results)
    return {"stamp": stamp, "kpi": {
        "colonnine": len(results), "esiti": {s: sum(1 for r in results if r.get("esito") == s) for s in ("GUASTO", "ATTENZIONE", "OK")},
        "prese": socks, "prese_disponibili": avail, "in_carica": charging,
        "energia_24h_kwh": round(e24, 1), "energia_7g_kwh": round(e7, 1),
        "disponibilita_7g": round(100 * ok7 / tot7, 1) if tot7 else None, "giri_7g": tot7,
        "ricambi_necessari": sum(x["quantita"] for x in k["necessari"]),
        "segnali": sum(len(v) for v in insights.values())},
        "componenti": sorted(comps.values(), key=lambda c: (-c["GUASTO"], -c["ATTENZIONE"])), "segnali": insights}


def station_trend(sid, current=None):
    r = current
    if not r:
        _, rs = latest_results()
        r = next((x for x in rs if x.get("id") == sid), None)
    if not r:
        return {"punti": [], "segnali": [], "limiti": {}}
    pts = history().get(station_key(r), [])
    lim = {v: _limits(v) for v in TREND_VARS}
    return {"punti": pts[-400:], "limiti": lim, "chiave": station_key(r)}


# ------------------------------------------------------------------ diagnosi
def diagnose(st, cfg, rundir=None):
    t0 = time.time()
    vals, title, layout, err, text = {}, "", [], "", ""
    try:
        text, vals, title, layout = read_page(st, cfg)
        vals["_PAGINA"] = "OK"
    except Exception as e:
        err = str(e)[:200]
        vals["_PAGINA"] = "NON RAGGIUNGIBILE"
    cuid = vals.get("CUID", "")
    vals["_CUID_DIVERSO"] = "SI" if st.get("cuid_atteso") and cuid and cuid != st["cuid_atteso"] else "NO"
    if cfg.get("ssh_attivo") and vals["_PAGINA"] == "OK":
        vals.update(ssh_checks(st, cfg))
    overall, found, marks = evaluate(vals)
    for f in found:
        if f["id"] == "CUID":
            f["messaggio"] = f"All'IP risponde {cuid}, attesa {st['cuid_atteso']}"
    res = {"id": st["id"], "nome": st.get("nome", ""), "ip": st["ip"], "porta_web": st.get("porta_web", ""),
           "cuid": cuid, "titolo": title, "esito": overall, "anomalie": found, "segni": marks, "valori": vals,
           "layout": layout, "errore": err, "secondi": round(time.time() - t0, 1), "ora": dt.datetime.now().strftime("%H:%M:%S")}
    if rundir:
        key = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{cuid or 'senza-cuid'}_{st['ip']}_{st.get('porta_web') or 80}")
        (rundir / f"{key}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        if text:
            (rundir / f"{key}.html").write_text(text, encoding="utf-8")
    return res


JOBS = {}


def start_run(ids):
    cfg = settings()
    sts = [s for s in stations() if not ids or s["id"] in ids]
    if not sts:
        raise ValueError("nessuna colonnina selezionata")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    rundir = RUNS_DIR / stamp
    rundir.mkdir(parents=True, exist_ok=True)
    job = {"id": uuid.uuid4().hex[:8], "stamp": stamp, "totale": len(sts), "fatti": 0, "risultati": [], "fine": False,
           "inizio": time.time(), "in_corso": [s["id"] for s in sts]}
    JOBS[job["id"]] = job

    def work():
        with cf.ThreadPoolExecutor(max_workers=max(1, min(int(cfg.get("parallelo") or 20), len(sts)))) as ex:
            futs = {ex.submit(diagnose, s, cfg, rundir): s for s in sts}
            for f in cf.as_completed(futs):
                s = futs[f]
                try:
                    r = f.result()
                except Exception as e:
                    r = {"id": s["id"], "nome": s.get("nome", ""), "ip": s["ip"], "esito": "GUASTO", "errore": str(e), "cuid": "",
                         "anomalie": [{"gravita": "GUASTO", "messaggio": f"errore del tool: {e}", "componente": "", "guasto": "", "ricambio": ""}],
                         "valori": {}, "segni": {}, "layout": []}
                job["risultati"].append(r)
                job["fatti"] += 1
                job["in_corso"] = [i for i in job["in_corso"] if i != s["id"]]
        job["fine"] = True
        job["durata"] = round(time.time() - job["inizio"], 1)
        job["kit"] = kit(job["risultati"])
        (rundir / "riepilogo.csv").write_text(results_csv(job["risultati"]), encoding="utf-8-sig")
        (rundir / "ricambi_da_portare.csv").write_text(kit_csv(job["kit"]), encoding="utf-8-sig")
        (REPORT_DIR / f"report_{stamp}.html").write_text(report_html(job), encoding="utf-8")

    threading.Thread(target=work, daemon=True).start()
    return job


def sorted_results(rs):
    return sorted(rs, key=lambda r: (-SEV_ORDER.get(r.get("esito"), 0), r.get("nome") or r.get("ip")))


def load_run(stamp):
    d = RUNS_DIR / re.sub(r"[^0-9_]", "", stamp)
    rs = [json.loads(p.read_text(encoding="utf-8")) for p in d.glob("*.json")]
    return d, sorted_results(rs)


def results_csv(rs):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["esito", "nome", "ip", "cuid", "gravita", "componente", "anomalia", "ricambio", "manuale"])
    for r in sorted_results(rs):
        if not r.get("anomalie"):
            w.writerow([r.get("esito"), r.get("nome"), r.get("ip"), r.get("cuid"), "", "", "nessuna anomalia", "", ""])
        for a in r.get("anomalie", []):
            w.writerow([r.get("esito"), r.get("nome"), r.get("ip"), r.get("cuid"), a.get("gravita"), a.get("componente"),
                        a.get("messaggio"), a.get("ricambio"), a.get("manuale", "")])
    return buf.getvalue()


def kit_csv(k):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["tipo", "codice", "descrizione", "quantita", "per"])
    for tipo, lst in (("necessario", k["necessari"]), ("di scorta", k["scorta"])):
        for x in lst:
            w.writerow([tipo, x["codice"], x["descrizione"], x["quantita"], " | ".join(x["per"])])
    return buf.getvalue()


def report_html(job):
    e = htmlmod.escape
    rs = sorted_results(job["risultati"])
    k = job.get("kit") or kit(rs)
    cnt = {s: sum(1 for r in rs if r.get("esito") == s) for s in ("GUASTO", "ATTENZIONE", "OK")}

    def ktab(lst):
        if not lst:
            return "<p><i>nessuno</i></p>"
        return ("<table><tr><th>Preso</th><th>Codice</th><th>Descrizione</th><th>Q.tà</th><th>Per</th></tr>" + "".join(
            f"<tr><td>☐</td><td><b>{e(x['codice'])}</b></td><td>{e(x['descrizione'])}</td><td>{x['quantita']}</td><td><small>{'<br>'.join(e(p) for p in x['per'])}</small></td></tr>"
            for x in lst) + "</table>")
    rows = []
    for r in rs:
        an = "".join(
            f"<li><b class='s-{e(a['gravita'])}'>{e(a['gravita'])}</b> {e(a['messaggio'])}"
            + (f" · <span class='c'>{e(a['componente'])}</span>" if a.get("componente") else "")
            + (f" · ricambio <b>{e(a['ricambio'])}</b>" if a.get("ricambio") else "")
            + (f" · <a href='{e(a['manuale'])}'>manuale</a>" if a.get("manuale") else "") + "</li>"
            for a in r.get("anomalie", [])) or "<li>nessuna anomalia</li>"
        rows.append(f"<tr><td><span class='b b-{e(r.get('esito',''))}'>{e(r.get('esito',''))}</span></td>"
                    f"<td>{e(r.get('nome') or '')}<br><small>{e(r.get('ip',''))}</small></td><td>{e(r.get('cuid') or '-')}</td><td><ul>{an}</ul></td></tr>")
    when = dt.datetime.strptime(job["stamp"], "%Y%m%d_%H%M%S").strftime("%d/%m/%Y %H:%M")
    return f"""<!doctype html><html lang="it"><meta charset="utf-8"><title>Report flotta {when}</title>
<style>body{{font:14px/1.45 Segoe UI,system-ui,Arial;margin:24px;color:#1d2330}}h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:16px;margin:20px 0 6px}}
.k{{display:inline-block;margin:8px 16px 8px 0;font-size:15px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #e3e6ea;padding:6px 8px;vertical-align:top;text-align:left}}th{{background:#f4f5f7}}
ul{{margin:0;padding-left:18px}}.b{{padding:2px 8px;border-radius:10px;font-weight:600;font-size:12px}}
.b-GUASTO{{background:#fde2e1;color:#a4161a}}.b-ATTENZIONE{{background:#fff1cc;color:#8a5a00}}.b-OK{{background:#dff3e4;color:#1e6b35}}
.s-GUASTO{{color:#a4161a}}.s-ATTENZIONE{{color:#8a5a00}}.s-INFO{{color:#667085}}.c{{color:#475467}}small{{color:#667085}}</style>
<h1>Report di flotta · colonnine 2.1</h1><div>{when} · {len(rs)} colonnine · durata {job.get('durata','?')} s · {APP} {VERSION}</div>
<div class="k"><span class="b b-GUASTO">GUASTO</span> {cnt['GUASTO']}</div><div class="k"><span class="b b-ATTENZIONE">ATTENZIONE</span> {cnt['ATTENZIONE']}</div>
<div class="k"><span class="b b-OK">OK</span> {cnt['OK']}</div>
<h2>Ricambi da portare</h2>{ktab(k['necessari'])}<h2>Di scorta</h2>{ktab(k['scorta'])}
<h2>Dettaglio per colonnina</h2><table><tr><th>Esito</th><th>Colonnina</th><th>CUID</th><th>Anomalie, ricambio, manuale</th></tr>{''.join(rows)}</table></html>"""


def analyze_html(text, name):
    vals, title, layout = parse_page(text)
    vals["_PAGINA"] = "OK" if len(vals) >= 5 else "NON RAGGIUNGIBILE"
    vals["_CUID_DIVERSO"] = "NO"
    overall, found, marks = evaluate(vals)
    return {"id": "file:" + name, "nome": name, "ip": "(file)", "cuid": vals.get("CUID", ""), "titolo": title, "layout": layout,
            "esito": overall, "anomalie": found, "segni": marks, "valori": vals, "errore": "",
            "ora": dt.datetime.now().strftime("%H:%M:%S"), "secondi": 0}


def verify_step(body):
    """Passo WEB di una procedura Remote: rilegge ora la pagina della colonnina e confronta."""
    st = station(body.get("id", ""))
    _, vals, _, _ = read_page(st, settings())
    var, exp = body.get("variabile", ""), str(body.get("atteso", ""))
    v = vals.get(var)
    if v is None:
        return {"valore": None, "esito": "FAIL", "nota": "variabile non presente nella pagina"}
    if exp.startswith("fuori:"):
        ok = not check("fuori", v, exp[6:])
    elif exp[:1] in "<>" and exp[:2] not in ("<>",):
        op = exp[:2] if exp[1:2] == "=" else exp[:1]
        ok = check(op, v, exp[len(op):])
    else:
        ok = check("=", v, exp)
    return {"valore": v, "esito": "PASS" if ok else "FAIL"}


def add_log(body):
    with LOCK:
        log = load_json(F_LOG, [])
        ev = {k: body.get(k) for k in ("colonnina", "nome", "cuid", "analisi", "tipo", "esito", "ricambi", "passi", "note")}
        ev["data"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.insert(0, ev)
        save_json(F_LOG, log[:2000])
    return ev


def open_path(p):
    try:
        if os.name == "nt":
            os.startfile(str(p))
        else:
            webbrowser.open(Path(p).as_uri())
    except Exception:
        pass


# ------------------------------------------------------------------ HTTP
def route(method, path, body):
    if method == "GET" and path == "/api/stato":
        cfg = settings()
        return {"app": APP, "versione": VERSION, "dati": str(DATA), "colonnine": stations(),
                "impostazioni": {**cfg, "password": "", "ha_password": bool(cfg.get("password") or poledesk_settings().get("default_password"))},
                "paramiko": PARAMIKO, "poledesk": (PD_DATA / "stations.json").exists(),
                "giri": sorted([p.name for p in RUNS_DIR.iterdir() if p.is_dir()], reverse=True)[:30],
                "config": config(), "ricambi": read_csv(F_PARTS), "procedure": procedures(),
                "guasti": fault_list(),
                "registro": load_json(F_LOG, [])[:300]}
    if method == "GET" and path.startswith("/api/job/"):
        j = JOBS.get(path.rsplit("/", 1)[1])
        if not j:
            raise KeyError("giro non trovato")
        return {**{k: v for k, v in j.items() if k != "risultati"}, "risultati": sorted_results(j["risultati"])}
    if method == "GET" and path.startswith("/api/giro/"):
        d, rs = load_run(path.rsplit("/", 1)[1])
        return {"stamp": d.name, "risultati": rs, "fine": True, "totale": len(rs), "fatti": len(rs), "kit": kit(rs)}
    if method == "POST" and path == "/api/avvia":
        return {"id": start_run(body.get("ids") or [])["id"]}
    if method == "POST" and path == "/api/diagnosi_una":
        return diagnose(station(body.get("id", "")), settings())
    if method == "POST" and path == "/api/cruscotto":
        return dashboard_data(body.get("risultati") or None)
    if method == "POST" and path == "/api/andamento":
        return station_trend(body.get("id", ""), body.get("risultato"))
    if method == "POST" and path == "/api/rivaluta":
        rs = body.get("risultati") or []
        for r in rs:
            r["esito"], r["anomalie"], r["segni"] = evaluate(r.get("valori") or {})
        return {"risultati": rs, "kit": kit(rs)}
    if method == "POST" and path == "/api/verifica":
        return verify_step(body)
    if method == "POST" and path == "/api/registro":
        return add_log(body)
    if method == "POST" and path == "/api/config":
        return save_config(body)
    if method == "POST" and path == "/api/importa_poledesk":
        return {"aggiunte": import_poledesk()}
    if method == "POST" and path == "/api/colonnine":
        rows = body.get("righe") or []
        write_csv(F_STATIONS, STATION_COLS, [[str(r.get(c, "")).strip() for c in STATION_COLS] for r in rows if str(r.get("ip", "")).strip()])
        return {"ok": True}
    if method == "POST" and path == "/api/impostazioni":
        cfg = settings()
        for k in ("ssh_attivo", "utente", "parallelo", "timeout_http", "timeout_ssh", "aggiorna_s"):
            if k in body:
                cfg[k] = body[k]
        if body.get("password"):
            cfg["password"] = body["password"]
        if body.get("password_cancella"):
            cfg["password"] = ""
        save_json(F_SETTINGS, cfg)
        return {"ok": True}
    if method == "POST" and path == "/api/analizza_html":
        return analyze_html(body.get("html") or "", body.get("nome") or "pagina")
    if method == "POST" and path == "/api/apri":
        target = {"dati": DATA, "regole": F_RULES, "ricambi": F_PARTS, "colonnine": F_STATIONS, "report": REPORT_DIR,
                  "procedure": F_PROCS, "configurazione": F_CONFIG}.get(body.get("cosa"))
        if not target:
            raise ValueError("percorso sconosciuto")
        open_path(target)
        return {"ok": True, "percorso": str(target)}
    raise KeyError(f"{method} {path}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8", extra=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        if (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, {"errore": "host non consentito"})
        u = urlparse(self.path)
        if method == "GET" and u.path in ("/", "/index.html"):
            return self._send(200, (BASE / "console.html").read_bytes(), "text/html; charset=utf-8")
        if method == "GET" and u.path.startswith("/report/"):
            p = REPORT_DIR / Path(unquote(u.path)).name
            if p.exists() and p.suffix == ".html":
                return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            return self._send(404, {"errore": "report non trovato"})
        if method == "GET" and u.path.startswith("/csv/"):
            name = Path(u.path).name
            d, rs = load_run(name.replace("kit_", ""))
            if name.startswith("kit_"):
                return self._send(200, kit_csv(kit(rs)).encode("utf-8-sig"), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f"attachment; filename=ricambi_da_portare_{d.name}.csv"})
            return self._send(200, results_csv(rs).encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename=report_{d.name}.csv"})
        try:
            body = {}
            if method == "POST":
                origin = self.headers.get("Origin", "")
                if origin and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
                    return self._send(403, {"errore": "origine non consentita"})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, route(method, u.path, body))
        except KeyError as e:
            self._send(404, {"errore": str(e).strip("'\"")})
        except Exception as e:
            self._send(400, {"errore": str(e)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    ensure_files()
    port = int(os.environ.get("MANUT21_PORT") or settings().get("porta") or 8770)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"{APP} {VERSION} attivo su {url}  (Ctrl+C per chiudere)")
    print(f"Dati in {DATA}")
    print("SSH: " + (f"paramiko {PARAMIKO}" if PARAMIKO else "non disponibile (si usa solo la pagina di stato)"))
    if not os.environ.get("MANUT21_NOBROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
