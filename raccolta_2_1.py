"""
Raccolta informazioni dalle colonnine WayPole/JuicePole 2.1 per il tool di manutenzione.

Solo lettura: non scrive nulla sulla colonnina, non ferma CP_main, non manda comandi alla scheda AUX.
Per ogni colonnina salva pagina di stato, sistema, servizi, memorie condivise, dispositivi, file di
configurazione (password mascherate), log recenti e l'elenco dei nomi interni di CP_main e PS4GAux.
Alla fine crea uno zip da riportare.

Uso: doppio clic su raccogli.bat (oppure: python raccolta_2_1.py)
"""
import concurrent.futures as cf
import csv
import datetime as dt
import getpass
import http.client
import json
import os
import re
import socket
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "1.0"
HERE = Path(__file__).resolve().parent
PD_DATA = Path(os.environ.get("POLEDESK_DATA") or Path.home() / "PoleDesk" / "data")
TIMEOUT = 25
MAX_BIN = 40_000_000

try:
    import paramiko
except ImportError:
    print("Manca la libreria paramiko. Avvia con raccogli.bat, che la installa.")
    input("Invio per chiudere...")
    sys.exit(1)

# ------------------------------------------------------------------ comandi (tutti in sola lettura)
COMMANDS = [
    ("01_sistema", "uname -a; echo; cat /etc/os-release 2>/dev/null; echo; uptime; date; hostname"),
    ("02_identita", "cat /sys/class/net/eth0/address; ip addr 2>/dev/null || ifconfig -a"),
    ("03_servizi", "systemctl is-active CP_main; systemctl status CP_main --no-pager 2>&1 | head -25; "
                   "echo; systemctl list-units --type=service --no-pager 2>&1 | head -80"),
    ("04_processi", "ps aux 2>/dev/null || ps"),
    ("05_porte", "netstat -tulnp 2>/dev/null || ss -tulnp 2>/dev/null || netstat -an"),
    ("06_memorie_condivise", "ipcs -m -p 2>&1; echo; ipcs -a 2>&1; echo; ls -la /dev/shm 2>&1"),
    ("07_dispositivi", "ls -la /dev/ttymxc* /dev/ttyUSB* /dev/voltage* /dev/led* 2>&1; echo; ls /sys/class/gpio 2>&1; "
                       "echo; for z in /sys/class/thermal/thermal_zone*/temp; do echo \"$z $(cat $z)\"; done"),
    ("08_spazio", "df -h; echo; free 2>/dev/null"),
    ("09_albero_crve", "ls -laR /crve 2>/dev/null | head -600"),
    ("10_configurazioni", "for f in $(find /crve /etc -maxdepth 3 \\( -name '*.ini' -o -name '*.conf' -o -name '*.cfg' "
                          "-o -name '*.json' -o -name 'displayip.txt' \\) -size -64k 2>/dev/null | grep -v '^/etc/ssl' | head -60); "
                          "do echo \"===== $f\"; cat \"$f\"; echo; done"),
    ("11_elenco_log", "ls -la /crve/log /crve/log/* /var/log 2>&1 | head -200"),
    ("12_log_recenti", "for f in $(ls -t /crve/log/*.log /crve/log/*/*.log 2>/dev/null | head -12); "
                       "do echo \"===== $f\"; tail -n 300 \"$f\"; echo; done"),
    ("13_journal_cp_main", "journalctl -u CP_main -n 300 --no-pager 2>&1 | tail -300"),
    ("14_pagina_dall_interno", "(curl -s --max-time 8 http://127.0.0.1/ || wget -qO- -T 8 http://127.0.0.1/) 2>/dev/null | head -c 200000"),
    ("15_eseguibili", "for n in CP_main PS4GAux rmaevse; do p=$(pidof $n 2>/dev/null | cut -d' ' -f1); "
                      "if [ -n \"$p\" ]; then echo \"$n $(readlink /proc/$p/exe) pid=$p\"; cat /proc/$p/cmdline | tr '\\0' ' '; echo; "
                      "ls -la /proc/$p/fd 2>/dev/null | grep -vE 'socket:|pipe:|anon_inode' | head -40; fi; done; "
                      "systemctl show -p ExecStart CP_main 2>/dev/null"),
]

INTERESTING = re.compile(r"(?i)temp|fan|heat|led|lux|volt|curr|param|shm|shared|fault|alarm|alm|err|status|state|"
                         r"socket|sck|plug|lock|contact|rcm|rccb|mcb|break|meter|rfid|modem|display|tamper|cuid|"
                         r"/dev/|/crve|/sys/|\.ini|\.log|ttymx|ttyusb|cunet|aux|ps4g|cmd|command|http|port|json")
SECRET = re.compile(r"(?i)^(\s*[\w.\-]*(pass|pwd|secret|token|apikey|api_key|private)[\w.\-]*\s*[=:]\s*)(.+)$")


def mask(text, secrets):
    out = []
    for line in text.splitlines():
        m = SECRET.match(line)
        if m:
            line = m.group(1) + "***"
        out.append(line)
    t = "\n".join(out)
    for s in secrets:
        if s and len(s) >= 4:
            t = t.replace(s, "***")
    return t


def clean_html_cells(html):
    cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", html)
    return [re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", c)).replace("&nbsp;", " ").strip() for c in cells]


def find_cuid(html):
    cells = clean_html_cells(html)
    for i, c in enumerate(cells):
        if c.rstrip(":").upper() == "CUID" and i + 1 < len(cells):
            return cells[i + 1].split()[0] if cells[i + 1] else ""
    m = re.search(r"CUID\W{0,10}(N\.[A-Z0-9]+)", re.sub(r"(?s)<[^>]+>", " ", html))
    return m.group(1) if m else ""


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:60] or "colonnina"


# ------------------------------------------------------------------ colonnine e credenziali
def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def choose_stations():
    pd_st = load_json(PD_DATA / "stations.json", [])
    pd_cfg = load_json(PD_DATA / "settings.json", {})
    aux = load_json(PD_DATA / "aux.json", {})
    web_url = aux.get("web_url") or "http://{{host}}/"
    chosen = []
    if pd_st:
        print(f"Colonnine nell'anagrafica di PoleDesk ({PD_DATA}):")
        for i, s in enumerate(pd_st, 1):
            print(f"  {i:2d}. {s.get('name','')}  {s.get('host','')}" + (f"  (scheda {s['source_ip']})" if s.get("source_ip") else ""))
        sel = input("Numeri da usare separati da virgola (Invio = tutte): ").strip()
        idx = range(1, len(pd_st) + 1) if not sel else [int(x) for x in re.findall(r"\d+", sel)]
        for i in idx:
            if 1 <= i <= len(pd_st):
                chosen.append(dict(pd_st[i - 1]))
    extra = input("Altri IP da aggiungere, separati da virgola (Invio = nessuno): ").strip()
    for ip in re.findall(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", extra):
        chosen.append({"name": ip, "host": ip})
    if not chosen:
        print("Nessuna colonnina scelta.")
        return [], None, web_url
    user = pd_cfg.get("default_username") or "root"
    pw = pd_cfg.get("default_password") or ""
    key = pd_cfg.get("default_key_path") or ""
    if not pw and not key and not all(s.get("password") or s.get("key_path") for s in chosen):
        u = input(f"Utente SSH (Invio = {user}): ").strip()
        user = u or user
        pw = getpass.getpass("Password SSH (non compare mentre scrivi): ")
    for s in chosen:
        s.setdefault("username", user)
        s["username"] = s.get("username") or user
        s["password"] = s.get("password") or pw
        s["key_path"] = s.get("key_path") or key
    return chosen, pw, web_url


# ------------------------------------------------------------------ lettura
def http_get(url, src=""):
    p = urlsplit(url)
    headers = {"User-Agent": "raccolta-2.1"}
    if p.username:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    kw = {"timeout": 10}
    if src:
        kw["source_address"] = (src, 0)
    if p.scheme == "https":
        import ssl
        kw["context"] = ssl._create_unverified_context()
    c = cls(p.hostname, p.port, **kw)
    c.request("GET", (p.path or "/") + (f"?{p.query}" if p.query else ""), headers=headers)
    r = c.getresponse()
    return r.status, r.read()


def ssh_connect(st):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sock = None
    port = int(st.get("port") or 22)
    if (st.get("source_ip") or "").strip():
        sock = socket.create_connection((st["host"], port), timeout=10, source_address=(st["source_ip"].strip(), 0))
    pw, key = st.get("password") or None, st.get("key_path") or None
    c.connect(st["host"], port=port, sock=sock, username=st.get("username") or "root", password=pw,
              key_filename=key, look_for_keys=False, allow_agent=False, timeout=10, banner_timeout=15, auth_timeout=15)
    return c


def run(c, cmd, binary=False):
    _, out, err = c.exec_command(cmd, timeout=TIMEOUT)
    data = out.read(MAX_BIN if binary else 5_000_000)
    e = err.read(200_000)
    return data if binary else data.decode("utf-8", "replace"), e.decode("utf-8", "replace")


def strings_of(blob, minlen=4):
    seen, out = set(), []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, blob):
        s = m.group().decode("ascii")
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def collect(st, web_url, outroot, secrets):
    t0 = time.time()
    host = st["host"]
    tmp = outroot / safe_name(f"{st.get('name') or host}_{host}")
    tmp.mkdir(parents=True, exist_ok=True)
    row = {"nome": st.get("name", ""), "ip": host, "cuid": "", "mac": "", "pagina_http": "", "ssh": "",
           "cp_main": "", "memorie_condivise": "", "errori": "", "secondi": ""}
    errors = []

    # pagina di stato dal PC
    url = web_url.replace("{{host}}", host)
    html = ""
    try:
        code, body = http_get(url, st.get("source_ip", ""))
        html = body.decode("utf-8", "replace")
        (tmp / "00_pagina_stato.html").write_bytes(body)
        row["pagina_http"] = str(code)
        row["cuid"] = find_cuid(html)
    except Exception as e:
        row["pagina_http"] = "ERRORE"
        errors.append(f"pagina: {e}")

    # SSH
    try:
        c = ssh_connect(st)
        row["ssh"] = "OK"
        hk = c.get_transport().get_remote_server_key()
        (tmp / "00_ssh.txt").write_text(f"host key: {hk.get_name()} {hk.get_fingerprint().hex()}\n"
                                        f"server: {c.get_transport().remote_version}\n", encoding="utf-8")
        for name, cmd in COMMANDS:
            try:
                out, err = run(c, cmd)
                text = mask(out + (f"\n----- stderr\n{err}" if err.strip() else ""), secrets)
                (tmp / f"{name}.txt").write_text(f"$ {cmd}\n\n{text}", encoding="utf-8")
                if name == "02_identita":
                    row["mac"] = out.splitlines()[0].strip() if out.strip() else ""
                if name == "03_servizi":
                    row["cp_main"] = out.splitlines()[0].strip() if out.strip() else ""
                if name == "06_memorie_condivise":
                    row["memorie_condivise"] = str(len(re.findall(r"^0x[0-9a-f]+\s", out, re.M)))
                if name == "14_pagina_dall_interno" and not row["cuid"]:
                    row["cuid"] = find_cuid(out)
            except Exception as e:
                errors.append(f"{name}: {e}")
        # nomi interni degli eseguibili (solo l'elenco filtrato, non i file)
        try:
            out, _ = run(c, COMMANDS[-1][1])
            for line in out.splitlines():
                m = re.match(r"^(CP_main|PS4GAux|rmaevse) (/\S+) pid=", line)
                if not m:
                    continue
                blob, _ = run(c, f"cat '{m.group(2)}'", binary=True)
                ss = strings_of(blob)
                keep = [s for s in ss if INTERESTING.search(s)][:6000]
                (tmp / f"16_nomi_{m.group(1)}.txt").write_text(
                    f"# {m.group(2)}  {len(blob)} byte, {len(ss)} stringhe, {len(keep)} filtrate\n" + "\n".join(keep),
                    encoding="utf-8")
        except Exception as e:
            errors.append(f"eseguibili: {e}")
        c.close()
    except Exception as e:
        row["ssh"] = "ERRORE"
        errors.append(f"ssh: {e}")

    row["errori"] = " | ".join(errors)[:500]
    row["secondi"] = f"{time.time() - t0:.0f}"
    (tmp / "00_errori.txt").write_text("\n".join(errors) or "nessuno", encoding="utf-8")
    final = outroot / safe_name(f"{row['cuid'] or row['mac'].replace(':', '') or 'senza-id'}_{st.get('name') or host}_{host}")
    if final != tmp and not final.exists():
        tmp.rename(final)
    return row


def main():
    print(f"Raccolta informazioni colonnine 2.1 · v{VERSION} · sola lettura\n")
    stations, pw, web_url = choose_stations()
    if not stations:
        input("Invio per chiudere...")
        return
    note = input("Note facoltative (es. 'A sana, B schermo nero'; Invio per saltare): ").strip()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    outroot = HERE / f"raccolta_{stamp}"
    outroot.mkdir(parents=True, exist_ok=True)
    secrets = {s.get("password") for s in stations} | {pw}
    print(f"\nRaccolgo da {len(stations)} colonnine in parallelo, circa 1 minuto ciascuna...\n")
    rows = []
    with cf.ThreadPoolExecutor(max_workers=min(10, len(stations))) as ex:
        futs = {ex.submit(collect, s, web_url, outroot, secrets): s for s in stations}
        for f in cf.as_completed(futs):
            s = futs[f]
            try:
                r = f.result()
            except Exception as e:
                r = {"nome": s.get("name", ""), "ip": s["host"], "errori": str(e)}
            rows.append(r)
            print(f"  {r.get('ip',''):15s} CUID {r.get('cuid') or '-':22s} pagina {r.get('pagina_http','-'):6s} "
                  f"SSH {r.get('ssh','-'):6s} CP_main {r.get('cp_main') or '-':8s} {('ERRORI: ' + r['errori'][:80]) if r.get('errori') else ''}")
    fields = ["nome", "ip", "cuid", "mac", "pagina_http", "ssh", "cp_main", "memorie_condivise", "secondi", "errori"]
    with open(outroot / "riepilogo.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    (outroot / "note.txt").write_text(f"raccolta v{VERSION} {stamp}\nPoleDesk dati: {PD_DATA}\nURL pagina: {web_url}\n\n{note}\n",
                                      encoding="utf-8")
    zpath = HERE / f"raccolta_{stamp}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in outroot.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(HERE))
    print(f"\nFatto. File da riportare:\n  {zpath}\n")
    if os.name == "nt":
        os.startfile(str(HERE))
    try:
        input("Invio per chiudere...")
    except EOFError:
        pass


if __name__ == "__main__":
    main()
