"""PoleDesk: server locale che fa da ponte SSH per l'interfaccia HTML.

Avvio:  python server.py   (poi apri http://127.0.0.1:8765)
Dati:   cartella ./data (anagrafica, azioni, sonde, impostazioni, registro, serie)
"""
import base64
import csv
import hashlib
import os
import io
import json
import re
import shlex
import shutil
import socket
import stat
import sys
import tarfile
import threading
import time
import uuid
import math
import urllib.request
import webbrowser
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import logging
import paramiko

PARAMIKO_VER = getattr(paramiko, "__version__", "?")

APP_NAME = "PoleDesk"
VERSION = "1.30.0"

FROZEN = getattr(sys, "frozen", False)          # True quando gira come PoleDesk.exe
UPDATED = os.environ.get("POLEDESK_UPDATED") == "1"


def _vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


# Aggiornamento senza ricreare l'eseguibile: se accanto a PoleDesk.exe c'è un server.py più recente
# (con il suo console.html), PoleDesk.exe esegue quello, con le librerie già contenute nell'eseguibile.
if FROZEN and not UPDATED:
    _ext = Path(sys.executable).resolve().parent / "server.py"
    _m = re.search(r'^VERSION = "([\d.]+)"', _ext.read_text(encoding="utf-8", errors="ignore"), re.M) if _ext.exists() else None
    if _m and _vt(_m.group(1)) > _vt(VERSION):
        print(f"Trovata accanto all'eseguibile la versione {_m.group(1)}: la avvio (eseguibile {VERSION}).")
        os.environ["POLEDESK_UPDATED"] = "1"
        import runpy
        runpy.run_path(str(_ext), run_name="__main__")
        sys.exit(0)
# interfaccia: accanto a server.py, oppure dentro l'eseguibile
BASE = Path(__file__).resolve().parent if UPDATED or not FROZEN else Path(getattr(sys, "_MEIPASS", ""))
# dati: variabile d'ambiente (avvia.bat), altrimenti %USERPROFILE%\PoleDesk\data per l'eseguibile,
# altrimenti la cartella data accanto a server.py
DATA = Path(os.environ.get("POLEDESK_DATA") or (Path.home() / "PoleDesk" / "data" if FROZEN else BASE / "data"))
SERIES_DIR = DATA / "series"
for d in (DATA, SERIES_DIR):
    d.mkdir(parents=True, exist_ok=True)

F_STATIONS, F_ACTIONS, F_PROBES = DATA / "stations.json", DATA / "actions.json", DATA / "probes.json"
F_SETTINGS, F_AUDIT, F_KNOWN = DATA / "settings.json", DATA / "audit.log", DATA / "known_hosts"

_ssh_log = logging.FileHandler(DATA / "ssh.log", encoding="utf-8")
_ssh_log.setLevel(logging.WARNING)
_ssh_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.getLogger("paramiko").addHandler(_ssh_log)    # niente raffiche di errori nella finestra nera
logging.getLogger("paramiko").setLevel(logging.WARNING)

MAX_OUTPUT = 1_000_000   # byte per comando, protegge la memoria sulle flotte
MAX_READ = 2_000_000     # byte massimi aperti nell'editor
LOCK = threading.RLock()

DEFAULT_SETTINGS = {
    "default_jump": "",
    "default_username": "root", "default_password": "", "default_key_path": "",
    "max_parallel": 20, "connect_timeout": 10, "command_timeout": 30, "port": 8765,
}

DEFAULT_ACTIONS = [
    {"id": "a1", "name": "Ping schermo", "kind": "read", "command": "ping -c 2 -W 2 {{display_ip}}"},
    {"id": "a2", "name": "Pagina web interna", "kind": "read", "command": "curl -s --max-time 5 http://127.0.0.1/ || wget -qO- -T 5 http://127.0.0.1/"},
    {"id": "a3", "name": "Temperature di sistema", "kind": "read", "command": "for z in /sys/class/thermal/thermal_zone*; do echo \"$(cat $z/type): $(cat $z/temp)\"; done"},
    {"id": "a4", "name": "Vicini di rete (schermo visto?)", "kind": "read", "command": "ip neigh 2>/dev/null || arp -a"},
    {"id": "a5", "name": "Interfacce di rete", "kind": "read", "command": "ip -br addr 2>/dev/null || ifconfig"},
    {"id": "a6", "name": "Dispositivi USB", "kind": "read", "command": "lsusb 2>/dev/null || ls /sys/bus/usb/devices"},
    {"id": "a7", "name": "Ultimi messaggi kernel", "kind": "read", "command": "dmesg | tail -n 60"},
    {"id": "a8", "name": "Uptime e carico", "kind": "read", "command": "uptime; free 2>/dev/null; df -h / 2>/dev/null"},
]

DEFAULT_PROBES = [
    {"id": "p1", "name": "Schermo raggiungibile", "mode": "exit", "command": "ping -c 1 -W 2 {{display_ip}}", "regex": "", "scale": 1, "unit": "1=sì"},
    {"id": "p2", "name": "Temperatura CPU", "mode": "number", "command": "cat /sys/class/thermal/thermal_zone0/temp", "regex": "", "scale": 0.001, "unit": "°C"},
    {"id": "p3", "name": "Carico 1 min", "mode": "number", "command": "cat /proc/loadavg", "regex": "", "scale": 1, "unit": ""},
]


# ---------------------------------------------------------------- archivio

def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


_SAVE_LOCK = threading.Lock()


def save(path, obj):
    # nome temporaneo unico e scrittura serializzata: più prove in parallelo scrivono gli stessi file
    with _SAVE_LOCK:
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


F_JUMPS = DATA / "jumps.json"


class JumpError(OSError):
    """Errore sul jump host: il messaggio è già leggibile."""


def jumps():
    return load(F_JUMPS, [])


def _jump_by_id(jid):
    return next((j for j in jumps() if j["id"] == jid), None)


def _jump_for(st):
    """Jump host da usare per una colonnina: il suo, quello predefinito, oppure nessuno ('direct')."""
    jid = (st.get("jump") or "").strip()
    if jid == "direct":
        return None
    if not jid:
        jid = (settings().get("default_jump") or "").strip()
    if not jid:
        return None
    j = _jump_by_id(jid)
    if not j:
        raise JumpError(f"jump host non trovato in anagrafica ({jid})")
    return j


def settings():
    return {**DEFAULT_SETTINGS, **load(F_SETTINGS, {})}


def stations():
    return load(F_STATIONS, [])


def station_by_id(sid):
    for s in stations():
        if s["id"] == sid:
            return s
    raise KeyError(f"colonnina {sid} non trovata")


def audit(station, op, detail, ok, extra=""):
    line = json.dumps({"t": datetime.now().isoformat(timespec="seconds"),
                       "station": station.get("name", "") if station else "",
                       "host": station.get("host", "") if station else "",
                       "op": op, "detail": detail[:500], "ok": ok, "extra": str(extra)[:300]},
                      ensure_ascii=False)
    with LOCK, F_AUDIT.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def render(cmd, st):
    """Sostituisce {{variabile}} con i valori della colonnina (name, host e variabili custom)."""
    values = {"name": st.get("name", ""), "host": st.get("host", ""), **(st.get("vars") or {})}

    def sub(m):
        k = m.group(1)
        if k not in values or values[k] in ("", None):
            raise ValueError(f"variabile mancante sulla colonnina: {k}")
        return str(values[k])
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, cmd)


# ---------------------------------------------------------------- SSH

class CommandTimeout(TimeoutError):
    """Tempo scaduto: conserva l'output ricevuto fino a quel momento."""

    def __init__(self, msg, out="", err=""):
        super().__init__(msg)
        self.out, self.err = out, err


class _Grab(logging.Handler):
    """Raccoglie i messaggi di paramiko durante la connessione, per spiegare gli errori di negoziazione."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


class Pool:
    """Connessioni riusate per colonnina, con riconnessione automatica e chiusura per inattività."""

    def __init__(self):
        self.conns, self.used, self.locks = {}, {}, {}
        self.ident, self.fail = {}, {}
        self.guard = threading.Lock()

    @staticmethod
    def _read_ident(c):
        """Legge MAC e nome della macchina: con IP uguali per tutte, è ciò che distingue le colonnine."""
        try:
            chan = c.get_transport().open_session(timeout=5)
            chan.settimeout(5)
            chan.exec_command("cat /sys/class/net/eth0/address 2>/dev/null || cat /sys/class/net/*/address 2>/dev/null | head -1; hostname")
            out, t0 = b"", time.time()
            while not chan.exit_status_ready() and time.time() - t0 < 5:
                if chan.recv_ready():
                    out += chan.recv(4096)
                else:
                    time.sleep(0.05)
            while chan.recv_ready():
                out += chan.recv(4096)
            chan.close()
            lines = [l.strip() for l in out.decode("utf-8", "replace").splitlines() if l.strip()]
            mac = next((l.lower() for l in lines if re.fullmatch(r"[0-9a-fA-F:]{17}", l)), "")
            host = next((l for l in lines if not re.fullmatch(r"[0-9a-fA-F:]{17}", l)), "")
            return {"mac": mac, "hostname": host}
        except Exception:
            return {"mac": "", "hostname": ""}

    def _lock(self, sid):
        with self.guard:
            return self.locks.setdefault(sid, threading.Lock())

    def get(self, st):
        sid = st["id"]
        with self._lock(sid):
            c = self.conns.get(sid)
            if c is not None:
                t = c.get_transport()
                if t is not None and t.is_active():
                    self.used[sid] = time.time()
                    return c
                self._close(sid)
            last = self.fail.get(sid)
            if last and time.time() - last[0] < 4:
                raise ConnectionError(f"{last[1]} (nuovo tentativo tra qualche secondo)")
            cfg = settings()
            c = paramiko.SSHClient()
            if F_KNOWN.exists():
                c.load_host_keys(str(F_KNOWN))
                c.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            grab = _Grab()
            plog = logging.getLogger("paramiko")
            old_level = plog.level
            plog.addHandler(grab)
            plog.setLevel(logging.DEBUG)
            try:
                self._connect(c, st, cfg)
            except (OSError, EOFError) as e:
                self.fail[sid] = (time.time(), describe_error(e))
                raise
            except paramiko.SSHException as e:
                self.fail[sid] = (time.time(), describe_error(e))
                offer = [l for l in grab.lines if "server key" in l.lower()]
                if "no acceptable" in str(e) and offer:
                    raise paramiko.SSHException(f"{e} | la colonnina offre: {offer[0][-160:]}") from e
                raise
            finally:
                plog.removeHandler(grab)
                plog.setLevel(old_level)
            c.get_transport().set_keepalive(30)
            self.fail.pop(sid, None)
            ident = self._read_ident(c)
            prev = self.ident.get(sid)
            changed = bool(prev and ident["mac"] and prev.get("mac") and prev["mac"] != ident["mac"])
            self.ident[sid] = {**ident, "since": time.time(),
                               "changed_at": time.time() if changed else (prev or {}).get("changed_at"),
                               "previous": prev.get("mac") if changed else (prev or {}).get("previous")}
            if changed:
                audit(st, "colonnina cambiata", f"stesso IP, MAC {prev['mac']} -> {ident['mac']}", True)
            self.conns[sid], self.used[sid] = c, time.time()
            return c

    def _new_client(self):
        c = paramiko.SSHClient()
        if F_KNOWN.exists():
            c.load_host_keys(str(F_KNOWN))
            c.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        return c

    def jump_client(self, j, depth=0):
        """Connessione (riutilizzata) a un jump host, eventualmente passando da un altro jump host."""
        if depth > 3:
            raise JumpError("catena di jump host troppo lunga o circolare")
        key = "jump:" + j["id"]
        with self._lock(key):
            c = self.conns.get(key)
            if c is not None and c.get_transport() is not None and c.get_transport().is_active():
                self.used[key] = time.time()
                return c
            self._close(key)
            cfg = settings()
            c = self._new_client()
            port = int(j.get("port") or 22)
            try:
                parent = _jump_by_id(j["via"]) if j.get("via") else None
                if j.get("via") and not parent:
                    raise JumpError(f"il jump host intermedio di {j['name']} non esiste più")
                sock = self.channel(parent, j["host"], port, depth + 1) if parent else None
                if sock is None and (j.get("source_ip") or "").strip():
                    sock = socket.create_connection((j["host"], port), timeout=cfg["connect_timeout"],
                                                    source_address=(j["source_ip"].strip(), 0))
                pw, kf = j.get("password") or None, j.get("key_path") or None
                user = j.get("username") or cfg["default_username"]
                if not (pw or kf) and "@" in (user or ""):
                    # stringa di connessione di un PAM (es. One Identity Safeguard: vaultaddress=…@token=…@account@dominio@destinazione):
                    # l'autenticazione è nel nome utente, il server non chiede password
                    _token_connect(c, j["host"], port, user, sock, cfg["connect_timeout"])
                else:
                    c.connect(j["host"], port=port, username=user,
                              password=pw, key_filename=kf, look_for_keys=not (pw or kf), allow_agent=not (pw or kf),
                              sock=sock, timeout=cfg["connect_timeout"], banner_timeout=cfg["connect_timeout"],
                              auth_timeout=cfg["connect_timeout"])
            except JumpError:
                raise
            except Exception as e:
                raise JumpError(f"jump host {j['name']} ({j['host']}): {describe_error(e)}") from e
            c.get_transport().set_keepalive(30)
            self.conns[key], self.used[key] = c, time.time()
            return c

    def channel(self, j, host, port, depth=0):
        """Canale TCP aperto dal jump host verso host:port (come ssh -J / -L). Se il jump host o il PAM non permettono
        l'inoltro di porte (direct-tcpip), si apre il collegamento con un comando sul jump host (nc o /dev/tcp di bash)."""
        tr = self.jump_client(j, depth).get_transport()
        to = settings()["connect_timeout"]
        err1 = None
        if j.get("tunnel", "auto") in ("auto", "tcpip"):
            try:
                return tr.open_channel("direct-tcpip", (host, int(port)), ("127.0.0.1", 0), timeout=to)
            except Exception as e:
                err1 = e
                if j.get("tunnel", "auto") == "tcpip" or not tr.is_active():
                    self.drop("jump:" + j["id"])
                    raise JumpError(f"il jump host {j['name']} non riesce a raggiungere {host}:{port} ({describe_error(e)})") from e
        h, pt = shlex.quote(str(host)), int(port)
        cmd = (f"if command -v nc >/dev/null 2>&1; then exec nc {h} {pt}; "
               f"else exec bash -c 'exec 3<>/dev/tcp/'{h}'/{pt}; cat <&3 & cat >&3'; fi")
        try:
            ch = tr.open_session(timeout=to)
            ch.exec_command(cmd)
            return ch
        except Exception as e:
            self.drop("jump:" + j["id"])
            raise JumpError(f"il jump host {j['name']} non riesce a raggiungere {host}:{port}: inoltro di porte "
                            f"{'rifiutato (' + describe_error(err1) + ')' if err1 else 'non usato'} e comando nc non riuscito ({describe_error(e)})") from e

    def _connect(self, c, st, cfg):
        pw = st.get("password") or cfg["default_password"] or None
        key = st.get("key_path") or cfg["default_key_path"] or None
        sock = None
        src = (st.get("source_ip") or "").strip()
        j = _jump_for(st)
        if j:
            sock = self.channel(j, st["host"], int(st.get("port") or 22))
            src = ""
        if src:
            try:
                sock = socket.create_connection((st["host"], int(st.get("port") or 22)),
                                                timeout=cfg["connect_timeout"], source_address=(src, 0))
            except OSError as e:
                if getattr(e, "winerror", None) == 10049 or getattr(e, "errno", None) in (99, 10049):
                    raise OSError(f"l'IP locale {src} non è presente su questo PC: controlla la scheda di rete") from e
                raise
        c.connect(st["host"], port=int(st.get("port") or 22), sock=sock,
                  username=st.get("username") or cfg["default_username"],
                  password=pw, key_filename=key,
                  look_for_keys=not (pw or key), allow_agent=not (pw or key),
                  timeout=cfg["connect_timeout"], banner_timeout=cfg["connect_timeout"],
                  auth_timeout=cfg["connect_timeout"])

    def _close(self, sid):
        c = self.conns.pop(sid, None)
        self.used.pop(sid, None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def drop(self, sid):
        with self._lock(sid):
            self._close(sid)

    def drop_all(self):
        for sid in list(self.conns):
            self.drop(sid)

    def reap(self, idle=300):
        for sid, t in list(self.used.items()):
            if time.time() - t > idle:
                self.drop(sid)

    def count(self):
        return len(self.conns)


POOL = Pool()


def _token_connect(c, host, port, user, sock, timeout):
    """Connessione a un PAM con il token nel nome utente: autenticazione "none" o keyboard-interactive senza risposte."""
    if sock is None:
        sock = socket.create_connection((host, port), timeout=timeout)
    t = paramiko.Transport(sock)
    t.banner_timeout = timeout
    t.start_client(timeout=timeout)
    allowed = []
    try:
        t.auth_none(user)
    except paramiko.BadAuthenticationType as e:
        allowed = list(e.allowed_types or [])
    except paramiko.AuthenticationException:
        pass
    if not t.is_authenticated() and "keyboard-interactive" in (allowed or ["keyboard-interactive"]):
        try:
            t.auth_interactive(user, lambda title, instr, prompts: ["" for _ in prompts])
        except paramiko.AuthenticationException:
            pass
    if not t.is_authenticated():
        t.close()
        raise paramiko.AuthenticationException(
            "il PAM ha rifiutato la stringa di connessione (token scaduto o già usato? chiedine una nuova e incollala di nuovo)"
            + (f"; metodi accettati: {', '.join(allowed)}" if allowed else ""))
    c._transport = t


def ssh_exec(st, cmd, timeout=None, stdin_data=None):
    """Esegue un comando e restituisce (exit_code, stdout, stderr). Riprova una volta se la connessione è caduta."""
    timeout = timeout or settings()["command_timeout"]
    for attempt in (0, 1):
        try:
            c = POOL.get(st)
            chan = c.get_transport().open_session(timeout=10)
            chan.settimeout(1.0)
            chan.exec_command(cmd)
            if stdin_data is not None:
                chan.sendall(stdin_data)
                chan.shutdown_write()
            out, err, deadline = bytearray(), bytearray(), time.time() + timeout
            while True:
                if chan.recv_ready():
                    out += chan.recv(65536)
                elif chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                elif chan.exit_status_ready():
                    break
                elif time.time() > deadline:
                    chan.close()
                    raise CommandTimeout(f"il comando non è terminato entro {timeout} s (aumenta il tempo massimo)",
                                         out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
                else:
                    time.sleep(0.02)
                if len(out) > MAX_OUTPUT:
                    chan.close()
                    out = out[:MAX_OUTPUT] + b"\n[output troncato]"
                    return -1, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
            while chan.recv_ready():
                out += chan.recv(65536)
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            code = chan.recv_exit_status()
            chan.close()
            return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        except TimeoutError:
            raise
        except (paramiko.SSHException, OSError, EOFError) as e:
            POOL.drop(st["id"])
            if attempt or isinstance(e, ConnectionError) and "nuovo tentativo" in str(e):
                raise


def describe_error(e):
    msg = str(e) or type(e).__name__
    if type(e).__name__ == "IncompatiblePeer" or "no acceptable" in msg:
        return ("SSH non compatibile con la colonnina (" + msg + "). La colonnina usa algoritmi datati: "
                f"serve paramiko 3.x, installata ora è la {PARAMIKO_VER}. Riavvia con avvia.bat della versione 1.3.2 o successiva.")
    if isinstance(e, paramiko.AuthenticationException):
        return "Autenticazione rifiutata: controlla utente, password o chiave"
    if isinstance(e, TimeoutError) or "timed out" in msg:
        return f"Tempo scaduto: {msg}"
    if isinstance(e, ConnectionError) and "nuovo tentativo" in msg:
        return msg
    if type(e).__name__ == "JumpError":
        return msg
    if msg.startswith("l'IP locale"):
        return msg
    if getattr(e, "errno", None) in (99, 10049) or getattr(e, "winerror", None) == 10049:
        return "L'IP locale indicato nella colonnina non è presente su questo PC: controlla il campo Scheda di rete"
    if isinstance(e, (RuntimeError, ValueError, FileExistsError)) and str(e):
        return str(e)
    if isinstance(e, ConnectionResetError) or "10054" in msg or "Connection reset" in msg:
        return ("Connessione chiusa dalla colonnina: succede quando sullo stesso IP è stata collegata un'altra colonnina. "
                "PoleDesk si riconnette da solo; se persiste, usa Riconnetti")
    if isinstance(e, ConnectionRefusedError):
        return "Connessione rifiutata: porta SSH chiusa o servizio fermo"
    return f"{type(e).__name__}: {msg}"


# ---------------------------------------------------------------- file (SFTP con ripiego su shell)

def _sftp(st):
    try:
        return POOL.get(st).open_sftp()
    except Exception:
        return None   # molte colonnine embedded (dropbear) non hanno il sottosistema SFTP


def fs_list(st, path):
    path = path or "/"
    sftp = _sftp(st)
    if sftp:
        try:
            items = [{"name": a.filename, "dir": stat.S_ISDIR(a.st_mode or 0), "size": a.st_size,
                      "mtime": a.st_mtime} for a in sftp.listdir_attr(path)]
        finally:
            sftp.close()
    else:
        code, out, err = ssh_exec(st, f"ls -1Ap -- {shlex.quote(path)}")
        if code:
            raise OSError(err.strip() or "impossibile leggere la cartella")
        items = [{"name": n.rstrip("/"), "dir": n.endswith("/"), "size": None, "mtime": None}
                 for n in out.splitlines() if n]
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return items


def fs_read(st, path):
    sftp = _sftp(st)
    if sftp:
        try:
            size = sftp.stat(path).st_size or 0
            if size > MAX_READ:
                raise OSError(f"file troppo grande per l'editor ({size} byte): usa la scheda Log")
            with sftp.open(path, "rb") as f:
                data = f.read()
        finally:
            sftp.close()
        return data.decode("utf-8", "replace")
    code, out, err = ssh_exec(st, f"head -c {MAX_READ} -- {shlex.quote(path)}")
    if code:
        raise OSError(err.strip() or "impossibile leggere il file")
    return out


def fs_exists(st, path):
    code, _, _ = ssh_exec(st, f"test -e {shlex.quote(path)}")
    return code == 0


def fs_write(st, path, content, backup=True, create_only=False):
    q = shlex.quote(path)
    exists = fs_exists(st, path)
    if create_only and exists:
        raise FileExistsError("esiste già un file con questo nome")
    bak = ""
    if backup and exists:
        bak = f"{path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        code, _, err = ssh_exec(st, f"cp -p -- {q} {shlex.quote(bak)}")
        if code:
            raise OSError(f"copia di sicurezza non riuscita: {err.strip()}")
    tmp = shlex.quote(path + ".tmp-console")
    code, _, err = ssh_exec(st, f"cat > {tmp} && mv -f -- {tmp} {q}", stdin_data=content.encode("utf-8"))
    if code:
        raise OSError(err.strip() or "scrittura non riuscita")
    return bak


def fs_simple(st, op, path):
    q = shlex.quote(path)
    cmd = {"mkdir": f"mkdir -p -- {q}", "delete": f"rm -f -- {q}"}[op]
    code, _, err = ssh_exec(st, cmd)
    if code:
        raise OSError(err.strip() or f"{op} non riuscito")


# ---------------------------------------------------------------- job paralleli

JOBS = {}
EXEC = ThreadPoolExecutor(max_workers=settings()["max_parallel"])


def start_job(body):
    ids = body.get("station_ids") or []
    sts = [s for s in stations() if s["id"] in ids]
    if not sts:
        raise ValueError("nessuna colonnina selezionata")
    kind = body.get("kind", "exec")
    quiet = bool(body.get("quiet"))   # letture di servizio (log, prove sonda) non finiscono nel registro
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "label": body.get("command") or body.get("path", ""),
           "total": len(sts), "results": {}, "started": time.time(), "done": False}
    with LOCK:
        JOBS[jid] = job
        for old in sorted(JOBS, key=lambda k: JOBS[k]["started"])[:-50]:
            JOBS.pop(old, None)

    def one(st):
        t0 = time.time()
        try:
            if kind == "write":
                bak = fs_write(st, body["path"], body.get("content", ""), body.get("backup", True))
                res = {"ok": True, "exit": 0, "stdout": f"scritto {body['path']}" + (f"\ncopia: {bak}" if bak else ""), "stderr": ""}
                audit(st, "scrittura file", body["path"], True, bak)
            else:
                cmd = render(body["command"], st)
                code, out, err = ssh_exec(st, cmd, timeout=body.get("timeout"))
                res = {"ok": code == 0, "exit": code, "stdout": out, "stderr": err}
                if not quiet:
                    audit(st, "comando", cmd, code == 0, f"exit {code}")
        except CommandTimeout as e:
            res = {"ok": False, "exit": None, "timeout": True, "stdout": e.out,
                   "stderr": (e.err + "\n" if e.err else "") + describe_error(e)}
            if not quiet:
                audit(st, "comando", body.get("command", ""), False, describe_error(e))
        except Exception as e:
            res = {"ok": False, "exit": None, "stdout": "", "stderr": describe_error(e)}
            if not quiet:
                audit(st, "scrittura file" if kind == "write" else "comando", body.get("command") or body.get("path", ""), False, res["stderr"])
        res["ms"] = int((time.time() - t0) * 1000)
        with LOCK:
            job["results"][st["id"]] = res
            if len(job["results"]) == job["total"]:
                job["done"] = True

    for st in sts:
        EXEC.submit(one, st)
    return jid


# ---------------------------------------------------------------- monitor

MON = {"running": False, "station_ids": [], "probe_ids": [], "interval": 10,
       "series": {}, "last": {}, "errors": {}, "started": None}
MON_STOP = threading.Event()


def parse_value(p, code, out):
    if p.get("mode") == "exit":
        return 1.0 if code == 0 else 0.0
    if code != 0:
        raise ValueError(f"il comando è terminato con codice {code}")
    rx = p.get("regex") or r"-?\d+(?:[.,]\d+)?"
    m = re.search(rx, out)
    if not m:
        raise ValueError("nessun numero trovato nell'output")
    raw = m.group(1) if m.groups() else m.group(0)
    return float(raw.replace(",", ".")) * float(p.get("scale") or 1)


def mon_loop():
    csv_path = SERIES_DIR / f"monitor-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter=";").writerow(["timestamp", "colonnina", "host", "sonda", "valore", "errore"])
    while not MON_STOP.is_set():
        t0 = time.time()
        sts = [s for s in stations() if s["id"] in MON["station_ids"]]
        prs = [p for p in load(F_PROBES, DEFAULT_PROBES) if p["id"] in MON["probe_ids"]]

        def sample(st, p):
            key, ts = f"{st['id']}|{p['id']}", time.time()
            try:
                code, out, err = ssh_exec(st, render(p["command"], st), timeout=max(5, MON["interval"]))
                v, e = parse_value(p, code, out), ""
            except Exception as ex:
                v, e = None, describe_error(ex)
            with LOCK:
                if v is not None:
                    MON["series"].setdefault(key, deque(maxlen=20000)).append((ts, v))
                    MON["last"][key] = v
                MON["errors"][key] = e
            return [datetime.fromtimestamp(ts).isoformat(timespec="seconds"), st["name"], st["host"],
                    p["name"], "" if v is None else v, e]

        rows = [f.result() for f in [EXEC.submit(sample, s, p) for s in sts for p in prs]]
        with csv_path.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f, delimiter=";").writerows(rows)
        MON_STOP.wait(max(0.5, MON["interval"] - (time.time() - t0)))
    MON["running"] = False


def mon_start(body):
    if MON["running"]:
        mon_stop()
    MON.update(station_ids=body.get("station_ids", []), probe_ids=body.get("probe_ids", []),
               interval=max(2, float(body.get("interval", 10))), series={}, last={}, errors={},
               started=time.time(), running=True)
    if not MON["station_ids"] or not MON["probe_ids"]:
        MON["running"] = False
        raise ValueError("scegli almeno una colonnina e una sonda")
    MON_STOP.clear()
    threading.Thread(target=mon_loop, daemon=True).start()


def mon_stop():
    MON_STOP.set()
    MON["running"] = False


def mon_data(since):
    with LOCK:
        pts = {k: [p for p in v if p[0] > since] for k, v in MON["series"].items()}
        return {k: MON[k] for k in ("running", "station_ids", "probe_ids", "interval", "started", "last", "errors")} | {"points": pts}


# ---------------------------------------------------------------- procedure guidate

F_PROCS = DATA / "procedures.json"
REPORTS_DIR = DATA / "reports"
REPORTS_DIR.mkdir(exist_ok=True)


def _step(sid, name, content, expect="result=0", question="", wait=0):
    return {"id": sid, "name": name, "content": content, "expect": expect, "fail": r"result=[1-9]",
            "question": question, "wait": wait, "timeout": 20}


# Contenuti dei file "command" della cartella "Collaudo PS4GAUX" (manuale Functional Test PS4GAUX PCBA JP2.1).
# 05 = scrittura parametro, 07 = scrittura stato uscite (ventola;riscaldatore;...), 04 = lettura parametro.
CMD_DISABLE_THERMAL = "05;00;TempContEn;0\n"
CMD_ENABLE_THERMAL = "05;00;TempContEn;1\n"
CMD_CHECK_THERMAL = "04;00;TempContEn\n"
CMD_FAN_ON = "07;01;00;01;00;01\n"
CMD_HEATER_ON = "07;00;01;01;00;01\n"
CMD_ALL_OFF = "07;00;00;01;00;01\n"     # identico per "fan off" e "heater off"


DEFAULT_PROCS = [{
    "id": "fan-heater", "name": "Collaudo fan e heater (PS4GAUX)",
    "log_path": "/crve/log/PS4GAux.log", "command_path": "/crve/command",
    "steps": [
        _step("s1", "Disabilita controllo termico", CMD_DISABLE_THERMAL),
        _step("s2", "Ventola accesa", CMD_FAN_ON, question="La ventola gira?"),
        _step("s3", "Ventola spenta", CMD_ALL_OFF, question="La ventola si è fermata?"),
        _step("s4", "Riscaldatore acceso", CMD_HEATER_ON, question="Il riscaldatore scalda? Attenzione: se resta acceso a lungo scotta."),
        _step("s5", "Riscaldatore spento", CMD_ALL_OFF, question="Il riscaldatore si sta raffreddando?", wait=10),
        _step("s6", "Riabilita controllo termico", CMD_ENABLE_THERMAL),
        _step("s7", "Verifica controllo termico", CMD_CHECK_THERMAL, expect=r"Value=1\b", question=""),
    ],
    "on_fail": ["s5", "s3", "s6"],
    "confirm_timeout": 180,
}]

RUNS, RUN_CTRL = {}, {}


class Aborted(Exception):
    pass


def procedures():
    procs = load(F_PROCS, DEFAULT_PROCS)
    # completa i file comando mancanti della procedura predefinita salvata con versioni precedenti
    defaults = {p["id"]: {s["id"]: s for s in p["steps"]} for p in DEFAULT_PROCS}
    for p in procs:
        for st in p.get("steps", []):
            d = defaults.get(p.get("id"), {}).get(st.get("id"))
            if d and not (st.get("content") or "").strip():
                st["content"] = d["content"]
    return procs


def _log_read(st, path, offset):
    """Restituisce (dimensione attuale, testo nuovo dall'offset) con un solo comando."""
    q = shlex.quote(path)
    code, out, err = ssh_exec(st, f"wc -c < {q} && tail -c +{offset + 1} {q}", timeout=15)
    if code:
        raise RuntimeError(f"log non leggibile ({path}): {err.strip()}")
    first, _, rest = out.partition("\n")
    return int(first.strip() or 0), rest


def _wait(ctrl, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if ctrl["abort"]:
            raise Aborted()
        time.sleep(0.2)


def _exec_step(run, ctrl, proc, st, step, rec, interactive):
    rec.update(status="running", started=time.time(), log="", error="")
    lost = None
    while True:
        try:
            _exec_step_once(run, ctrl, proc, st, step, rec, interactive)
            rec.update(status="ok", ms=int((time.time() - rec["started"]) * 1000))
            return True
        except Aborted:
            rec.update(status="aborted", error="interrotto")
            raise
        except Exception as e:
            if _is_conn_error(e) and not ctrl["abort"]:
                lost = lost or time.time()
                if time.time() - lost < RECONNECT_MAX:
                    # connessione persa: si aspetta che torni e si rifà lo stesso passo
                    rec.update(status="reconnect", error=f"connessione persa, riprovo: {describe_error(e)}")
                    POOL.drop(st["id"])
                    _wait(ctrl, 3)
                    rec.update(status="running", error="")
                    continue
            rec.update(status="ko", error=describe_error(e), ms=int((time.time() - rec["started"]) * 1000))
            return False


def _exec_step_once(run, ctrl, proc, st, step, rec, interactive):
    content = step.get("content") or ""
    if not content.strip():
        raise ValueError("contenuto del file comando mancante: caricalo nella procedura")
    size, _ = _log_read(st, proc["log_path"], 10**12)   # solo la dimensione
    offset = size
    fs_write(st, proc["command_path"], content, backup=False)
    web_event(st, "procedura", f"{proc['name']}: {step['name']}", content.strip(), "inviato")
    t0, timeout = time.time(), float(step.get("timeout") or 20)
    exp, fail = step.get("expect") or "", step.get("fail") or ""
    new = ""
    while True:
        if ctrl["abort"]:
            raise Aborted()
        size, chunk = _log_read(st, proc["log_path"], offset)
        if size < offset:          # log ruotato
            offset, new = 0, ""
            continue
        new += chunk
        offset += len(chunk.encode("utf-8"))
        rec["log"] = new[-6000:]
        if fail and re.search(fail, new):
            raise RuntimeError(f"il log segnala un errore ({re.search(fail, new).group(0)})")
        if exp and re.search(exp, new):
            break
        if not exp and time.time() - t0 >= 3:
            break
        if time.time() - t0 > timeout:
            raise RuntimeError(f"nessuna conferma nel log entro {timeout:.0f} s (atteso: {exp})")
        time.sleep(0.7)
    var = (step.get("check_var") or "").strip()
    if var:                        # verifica dell'attuazione sulla pagina web della colonnina
        want, deadline, got = str(step.get("check_value", "")).strip(), time.time() + float(step.get("check_timeout") or 10), None
        while True:
            if ctrl["abort"]:
                raise Aborted()
            got = _web_value(st, var)
            if _same(got, want):
                rec["log"] = (rec["log"] + f"\n[pagina web] {var} = {got} ✓").strip()
                break
            if time.time() > deadline:
                raise RuntimeError(f"attuazione non confermata dalla pagina web: {var} = {got}, atteso {want}")
            time.sleep(1)
    if step.get("question") and interactive:
        _wait(ctrl, float(step.get("wait") or 0))
        rec["status"], ctrl["answer"] = "waiting", None
        run["question"] = {"step": step["id"], "text": step["question"]}
        ctrl["evt"].clear()
        limit = float(proc.get("confirm_timeout") or 180)
        if not ctrl["evt"].wait(limit) and not ctrl["abort"]:
            run["question"] = None
            raise RuntimeError(f"nessuna risposta dell'operatore entro {limit:.0f} s")
        run["question"] = None
        if ctrl["abort"]:
            raise Aborted()
        if ctrl["answer"] != "ok":
            raise RuntimeError("verifica visiva negativa (KO indicato dall'operatore)")


def _proc_thread(rid):
    run, ctrl = RUNS[rid], RUN_CTRL[rid]
    proc, st = ctrl["proc"], ctrl["station"]
    by_id = {s["id"]: s for s in proc["steps"]}
    try:
        for step, rec in zip(proc["steps"], run["steps"]):
            run["current"] = step["id"]
            if not _exec_step(run, ctrl, proc, st, step, rec, True):
                run["result"] = "KO"
                break
        else:
            run["result"] = "OK"
    except Aborted:
        run["result"] = "INTERROTTO"
    except Exception as e:
        run["result"], run["error"] = "ERRORE", describe_error(e)
    run["question"] = None
    if run["result"] != "OK":
        ctrl["abort"] = False                     # la messa in sicurezza deve poter girare
        run["phase"] = "messa in sicurezza"
        for sid in proc.get("on_fail", []):
            if sid in by_id:
                rec = {"id": sid, "name": by_id[sid]["name"], "status": "pending"}
                run["cleanup"].append(rec)
                try:
                    _exec_step(run, ctrl, proc, st, by_id[sid], rec, False)
                except Aborted:
                    pass
    run.update(done=True, current=None, phase="", ended=time.time())
    audit(st, "procedura", proc["name"], run["result"] == "OK", run["result"])
    _save_report(rid)


def _save_report(rid):
    run = RUNS[rid]
    name = f"{datetime.fromtimestamp(run['started']):%Y%m%d-%H%M%S}_{re.sub(r'[^A-Za-z0-9_-]+', '-', run['station'])}_{run['procedure_id']}"
    idn = POOL.ident.get(run["station_id"], {})
    lines = [f"{run['procedure']}", f"Colonnina: {run['station']} ({run['host']})"
             + (f", MAC {idn['mac']}" if idn.get("mac") else "") + (f", {idn['hostname']}" if idn.get("hostname") else ""),
             f"Inizio: {datetime.fromtimestamp(run['started']):%d/%m/%Y %H:%M:%S}",
             f"Esito: {run['result']}" + (f" - {run['error']}" if run.get("error") else ""), ""]
    for i, r in enumerate(run["steps"], 1):
        lines.append(f"{i}. {r['name']}: {r['status'].upper()}" + (f" - {r['error']}" if r.get("error") else ""))
    if run["cleanup"]:
        lines += ["", "Messa in sicurezza:"] + [f"- {r['name']}: {r['status'].upper()}" + (f" - {r.get('error')}" if r.get("error") else "") for r in run["cleanup"]]
    lines += ["", "Log per passo:"]
    for r in run["steps"] + run["cleanup"]:
        if r.get("log"):
            lines += [f"--- {r['name']}", r["log"].rstrip()]
    run["report_file"] = name + ".txt"
    (REPORTS_DIR / run["report_file"]).write_text("\n".join(lines) + "\n", encoding="utf-8")


def proc_start(body):
    proc = next((p for p in procedures() if p["id"] == body.get("procedure_id")), None)
    if not proc:
        raise KeyError("procedura non trovata")
    st = station_by_id(body.get("station_id", ""))
    missing = [s["name"] for s in proc["steps"] if not (s.get("content") or "").strip()]
    if missing:
        raise ValueError("manca il contenuto del file comando per: " + ", ".join(missing))
    if any(not r["done"] and r["station_id"] == st["id"] for r in RUNS.values()):
        raise ValueError("su questa colonnina c'è già una procedura in corso")
    rid = uuid.uuid4().hex[:10]
    RUNS[rid] = {"id": rid, "procedure_id": proc["id"], "procedure": proc["name"], "station_id": st["id"],
                 "station": st["name"], "host": st["host"], "started": time.time(), "done": False,
                 "result": None, "error": "", "question": None, "current": None, "phase": "",
                 "steps": [{"id": s["id"], "name": s["name"], "status": "pending"} for s in proc["steps"]],
                 "cleanup": []}
    RUN_CTRL[rid] = {"evt": threading.Event(), "answer": None, "abort": False, "proc": proc, "station": st}
    audit(st, "procedura", proc["name"], True, "avviata")
    threading.Thread(target=_proc_thread, args=(rid,), daemon=True).start()
    return rid


def proc_answer(body):
    ctrl = RUN_CTRL.get(body.get("id"))
    if not ctrl:
        raise KeyError("esecuzione non trovata")
    ctrl["answer"] = "ok" if body.get("answer") == "ok" else "ko"
    ctrl["evt"].set()


def proc_abort(body):
    ctrl = RUN_CTRL.get(body.get("id"))
    if not ctrl:
        raise KeyError("esecuzione non trovata")
    ctrl["abort"] = True
    ctrl["evt"].set()


# ---------------------------------------------------------------- console AUX e pagina web

F_AUX = DATA / "aux.json"
AUX_LIBRARY = [
    {"name": "Disabilita controllo termico", "line": "05;00;TempContEn;0"},
    {"name": "Abilita controllo termico", "line": "05;00;TempContEn;1"},
    {"name": "Leggi controllo termico", "line": "04;00;TempContEn"},
    {"name": "Ventola accesa", "line": "07;01;00;01;00;01"},
    {"name": "Riscaldatore acceso", "line": "07;00;01;01;00;01"},
    {"name": "Ventola e riscaldatore spenti", "line": "07;00;00;01;00;01"},
]
DEFAULT_AUX = {"command_path": "/crve/command", "log_path": "/crve/log/PS4GAux.log",
               "web_mode": "pc", "web_url": "http://{{host}}/", "interval": 2, "library": AUX_LIBRARY}
LINE_RX = re.compile(r"^\d{2}(;[^;\r\n]*)*$")


def aux_cfg():
    return {**DEFAULT_AUX, **load(F_AUX, {})}


def _num(v):
    """Numero solo se il valore è un numero, eventualmente con unità breve (es. "24,8 °C"); no IP o date."""
    m = re.fullmatch(r"\s*(-?\d+(?:[.,]\d+)?)\s*[^\d\s.,:;/-]{0,5}\s*", str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def _clean(html):
    t = re.sub(r"(?is)<br\s*/?>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&deg;", "°")
    return re.sub(r"\s+", " ", t).strip()


def parse_vars(text, hints=None):
    """Estrae coppie nome/valore da JSON, tabelle HTML, elementi con id o righe 'nome: valore'."""
    out = {}

    def add(k, v):
        k, v = str(k).strip().rstrip(":"), str(v).strip()
        if not k or len(k) > 80 or len(v) > 200:
            return
        base, i = k, 2
        while k in out:
            k, i = f"{base} #{i}", i + 1
        out[k] = v

    s = text.strip()
    if s[:1] in "{[":
        try:
            def flat(o, p):
                if isinstance(o, dict):
                    for k, v in o.items():
                        flat(v, f"{p}.{k}" if p else str(k))
                elif isinstance(o, list):
                    for i, v in enumerate(o):
                        flat(v, f"{p}[{i}]")
                else:
                    add(p, o)
            flat(json.loads(s), "")
            return out, "JSON"
        except ValueError:
            pass
    if re.search(r"(?i)<(html|body|table|div|span|td)\b", s):
        body = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", "", s)
        for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", body):
            raw_cells = [_clean(x) for x in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
            cells = [c for c in raw_cells if c]
            if len(raw_cells) >= 4 and len(raw_cells) % 2 == 0:
                # più coppie nome | valore sulla stessa riga: | CUID | N.21… | ACTIVE FLAG | ACTIVE |
                for i in range(0, len(raw_cells), 2):
                    if raw_cells[i]:
                        add(raw_cells[i], raw_cells[i + 1])
            elif len(cells) >= 2:
                add(cells[0], " ".join(cells[1:]))
        for m in re.finditer(r'(?is)<(\w+)[^>]*\bid="([^"]+)"[^>]*>([^<]{1,80})</\1>', body):
            if m.group(3).strip():
                add(m.group(2), _clean(m.group(3)))
        for m in re.finditer(r'(?is)<input[^>]*?\b(?:name|id)="([^"]+)"[^>]*?\bvalue="([^"]*)"', body):
            add(m.group(1), m.group(2))
        flat_text = re.sub(r"(?i)</?(p|div|li|tr|br|h\d|table)[^>]*>", "\n", re.sub(r"(?i)</t[dh]>", " </td>", body))
        lines, fmt = [_clean(l) for l in flat_text.split("\n")], "HTML"
    else:
        lines, fmt = s.splitlines(), "testo"
    known = set(out)
    for line in lines:
        for k, v in _split_pairs(line or ""):
            if k not in known:
                add(k, v)
                known.add(k)
    _apply_hints("\n".join(l or "" for l in lines), out, hints)
    return out, fmt


# inizio di un nuovo nome dentro una riga: parole maiuscole (la prima solo lettere) seguite da ':' o '='
_NEXT_KEY = re.compile(r"\s(?=[A-Z][A-Z_/\-]*(?:\s+[A-Z0-9][A-Z0-9_/\-.]*)*\s*[:=])")


def _split_pairs(line):
    """Separa più coppie 'NOME: valore' sulla stessa riga (es. 'AUX TEMPERATURE: 30.49 C AUX LEDS CURRENT: 0.10 A')."""
    out, rest = [], line.strip()
    while rest:
        m = re.match(r"^([^:=<>]{1,60}?)\s*[:=]\s*", rest)
        if not m:
            break
        key, tail = m.group(1).strip(), rest[m.end():]
        nxt = _NEXT_KEY.search(tail)
        val, rest = (tail[:nxt.start()], tail[nxt.start():].strip()) if nxt else (tail, "")
        out.append((key, val.strip()))
    return out


def _hint_names():
    try:
        return [v for v in profile_cfg()["vars"].values() if v]
    except Exception:
        return []


def _apply_hints(text, out, hints):
    """Per i nomi attesi (quelli del profilo) cerca 'NOME: valore' nel testo e ne corregge il valore."""
    names = sorted({h for h in (hints if hints is not None else _hint_names()) if h}, key=len, reverse=True)
    if not names:
        return
    rx = re.compile(r"(?<![A-Za-z0-9\-])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9])\s*([:=])?\s*", re.I)
    ms = list(rx.finditer(text))
    lower = [n.lower() for n in names]
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        seg = text[m.end():end].split("\n")[0]
        nxt = _NEXT_KEY.search(seg)
        val = (seg[:nxt.start()] if nxt else seg).strip()
        name = names[lower.index(m.group(1).lower())]
        cur = next((k for k in out if k.lower() == name.lower()), None)
        merged = cur is not None and any(n in out[cur].lower() for n in lower if n != name.lower())
        # senza i due punti si corregge solo un valore mancante o "incollato" ad altre variabili
        if val and (m.group(2) or cur is None or merged):
            if cur:
                del out[cur]
            out[name] = val


def web_fetch(st, cfg):
    url = render(cfg.get("web_url") or "", st)
    if not url:
        raise ValueError("indirizzo della pagina web non impostato")
    t0 = time.time()
    if cfg.get("web_mode") == "ssh":
        q = shlex.quote(url)
        code, out, err = ssh_exec(st, f"curl -s -k --max-time 6 {q} 2>/dev/null || wget -qO- -T 6 {q}", timeout=12)
        if not out.strip():
            raise OSError(err.strip() or "la pagina è vuota o non raggiungibile dalla colonnina")
        text = out
    else:
        import base64, ssl, urllib.request
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(url)
        headers = {"User-Agent": "PoleDesk"}
        if p.username:
            tok = base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
            headers["Authorization"] = "Basic " + tok
            url = urlunsplit((p.scheme, p.hostname + (f":{p.port}" if p.port else ""), p.path, p.query, p.fragment))
        src = (st.get("source_ip") or "").strip()
        jh = _jump_for(st)
        if jh:
            import http.client
            q = urlsplit(url)
            if q.scheme == "https":
                raise JumpError("pagina HTTPS tramite jump host non supportata: in Fonte dati scegli la lettura dalla colonnina via SSH")
            port = q.port or 80
            conn = http.client.HTTPConnection(q.hostname, port, timeout=6)
            conn.sock = POOL.channel(jh, q.hostname, port)
            try:
                conn.request("GET", (q.path or "/") + (f"?{q.query}" if q.query else ""), headers=headers)
                text = conn.getresponse().read(2_000_000).decode("utf-8", "replace")
            finally:
                conn.close()
        elif src:
            import http.client
            q = urlsplit(url)
            kw = {"timeout": 6, "source_address": (src, 0)}
            conn = (http.client.HTTPSConnection(q.hostname, q.port or 443, context=ssl._create_unverified_context(), **kw)
                    if q.scheme == "https" else http.client.HTTPConnection(q.hostname, q.port or 80, **kw))
            try:
                conn.request("GET", (q.path or "/") + (f"?{q.query}" if q.query else ""), headers=headers)
                text = conn.getresponse().read(2_000_000).decode("utf-8", "replace")
            finally:
                conn.close()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=6, context=ssl._create_unverified_context()) as r:
                text = r.read(2_000_000).decode("utf-8", "replace")
    return text, int((time.time() - t0) * 1000)


WEB = {"running": False, "station_id": None, "interval": 2, "vars": {}, "series": {}, "events": [], "rows": deque(maxlen=50000), "csv": None,
       "log": deque(maxlen=3000), "error": "", "log_error": "", "fmt": "", "ms": 0, "samples": 0, "started": None}
WEB_STOP = threading.Event()


def web_loop():
    st, cfg = station_by_id(WEB["station_id"]), aux_cfg()
    log_off = None
    csv_path = SERIES_DIR / f"sessione-{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9_-]+', '-', st['name'])}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerow(["timestamp", "tipo", "nome", "valore", "dettaglio"])
    WEB["csv"] = csv_path
    while not WEB_STOP.is_set():
        t0 = time.time()
        rows = []
        try:
            text, ms = web_fetch(st, cfg)
            vars_, fmt = parse_vars(text)
            err = "" if vars_ else "nessuna variabile riconosciuta nella pagina"
        except Exception as e:
            vars_, fmt, ms, err = {}, WEB["fmt"], 0, describe_error(e)
        now = time.time()
        with LOCK:
            for k, v in vars_.items():
                prev = WEB["vars"].get(k)
                changed = prev is not None and prev["v"] != v
                n = _num(v)
                WEB["vars"][k] = {"v": v, "n": n, "t": now,
                                  "changed": now if changed else (prev or {}).get("changed", 0)}
                if n is not None:
                    WEB["series"].setdefault(k, deque(maxlen=5000)).append((now, n))
                rows.append([datetime.fromtimestamp(now).isoformat(timespec="milliseconds"), "variabile", k,
                             v.replace(".", ",") if n is not None else v, ""])
            if vars_:
                WEB["rows"].append((now, dict(vars_)))
            WEB.update(error=err, fmt=fmt, ms=ms, samples=WEB["samples"] + (1 if vars_ else 0))
        try:
            if log_off is None:
                log_off, _ = _log_read(st, cfg["log_path"], 10**12)
            else:
                size, chunk = _log_read(st, cfg["log_path"], log_off)
                if size < log_off:
                    log_off = 0
                else:
                    log_off += len(chunk.encode("utf-8"))
                    with LOCK:
                        for line in chunk.splitlines():
                            if line.strip():
                                WEB["log"].append((time.time(), line))
            WEB["log_error"] = ""
        except Exception as e:
            WEB["log_error"] = describe_error(e)
        if rows:
            with csv_path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f, delimiter=";").writerows(rows)
        WEB_STOP.wait(max(0.3, WEB["interval"] - (time.time() - t0)))
    WEB["running"] = False


def web_start(body):
    web_stop()
    time.sleep(0.1)
    st = station_by_id(body.get("station_id", ""))
    WEB.update(running=True, station_id=st["id"], interval=max(1.0, float(body.get("interval") or aux_cfg()["interval"])),
               vars={}, series={}, events=[], rows=deque(maxlen=50000), csv=None,
               log=deque(maxlen=3000), error="", log_error="", fmt="", ms=0,
               samples=0, started=time.time())
    WEB_STOP.clear()
    threading.Thread(target=web_loop, daemon=True).start()


def web_stop():
    WEB_STOP.set()
    WEB["running"] = False


def web_event(st, kind, label, line, status="", value=None, ev=None):
    """Registra un'attuazione nella sessione in corso (grafici e CSV), da qualunque scheda provenga."""
    if not st or WEB["station_id"] != st["id"] or not WEB["running"]:
        return
    ev = ev if ev is not None else {"t": time.time(), "label": label, "line": line, "kind": kind}
    ev.update(status=status, kind=kind, **({"value": value} if value is not None else {}))
    with LOCK:
        if not any(e is ev for e in WEB["events"]):
            WEB["events"].append(ev)
        path = WEB.get("csv")
    if path:
        with Path(path).open("a", newline="", encoding="utf-8") as f:
            csv.writer(f, delimiter=";").writerow([datetime.fromtimestamp(ev["t"]).isoformat(timespec="milliseconds"),
                                                  "attuazione", label, line, f"{kind}; {status}" + (f"; Value={value}" if value is not None else "")])


def web_export():
    """CSV 'largo' per Excel: una riga per campione, una colonna per variabile, colonna attuazioni."""
    with LOCK:
        rows, events = list(WEB["rows"]), list(WEB["events"])
        st = next((s for s in stations() if s["id"] == WEB["station_id"]), {"name": "colonnina"})
    keys = sorted({k for _, r in rows for k in r}, key=lambda k: k.lower())
    items = [(t, "v", r) for t, r in rows] + [(e["t"], "e", e) for e in events]
    items.sort(key=lambda x: x[0])
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["timestamp", *keys, "attuazione"])
    for t, kind, r in items:
        ts = datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M:%S.%f")[:-3]
        if kind == "v":
            w.writerow([ts, *[(r.get(k, "").replace(".", ",") if _num(r.get(k, "")) is not None else r.get(k, "")) for k in keys], ""])
        else:
            w.writerow([ts, *[""] * len(keys), f"{r.get('label', '')} | {r.get('line', '')} | {r.get('status', '')}"
                        + (f" | Value={r['value']}" if r.get("value") is not None else "")])
    name = f"sessione-{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9_-]+', '-', st['name'])}.csv"
    return buf.getvalue(), name


def aux_shell(body):
    st = station_by_id(body.get("station_id", ""))
    cmd = (body.get("command") or "").strip()
    if not cmd:
        raise ValueError("scrivi un comando")
    ev = {"t": time.time(), "label": body.get("label") or cmd[:60], "line": cmd, "kind": "shell"}
    code, out, err = ssh_exec(st, render(cmd, st), timeout=float(body.get("timeout") or 30))
    status = "ok" if code == 0 else "ko"
    audit(st, "comando shell", cmd, code == 0, f"exit {code}")
    web_event(st, "shell", ev["label"], cmd, status, ev=ev)
    return {**ev, "status": status, "exit": code, "stdout": out[-20000:], "stderr": err[-5000:]}


def web_data(since):
    with LOCK:
        return {"running": WEB["running"], "station_id": WEB["station_id"], "interval": WEB["interval"],
                "fmt": WEB["fmt"], "ms": WEB["ms"], "samples": WEB["samples"], "error": WEB["error"],
                "log_error": WEB["log_error"], "started": WEB["started"], "now": time.time(),
                "vars": WEB["vars"], "events": WEB["events"],
                "points": {k: [p for p in v if p[0] > since] for k, v in WEB["series"].items()},
                "log": [l for l in WEB["log"] if l[0] > since]}


def aux_send(body):
    st, cfg = station_by_id(body.get("station_id", "")), aux_cfg()
    line = (body.get("line") or "").strip()
    if not LINE_RX.match(line):
        raise ValueError("formato non valido: atteso NN;campo;campo… (es. 04;00;TempContEn)")
    off, _ = _log_read(st, cfg["log_path"], 10**12)
    q = shlex.quote(cfg["command_path"])
    # scrittura diretta del file, come fa WinSCP nel manuale
    code, _, err = ssh_exec(st, f"cat > {q}", stdin_data=(line + "\n").encode("utf-8"))
    if code:
        audit(st, "comando AUX", line, False, err.strip())
        raise RuntimeError(err.strip() or "scrittura del file comando non riuscita")
    ev = {"t": time.time(), "label": body.get("label") or line, "line": line, "status": "in attesa", "log": "", "kind": "AUX"}
    if WEB["station_id"] == st["id"] and WEB["running"]:
        with LOCK:
            WEB["events"].append(ev)
    wait = body.get("wait")
    new, deadline = "", time.time() + (8.0 if wait is None else float(wait))
    while time.time() < deadline:
        size, chunk = _log_read(st, cfg["log_path"], off)
        if size < off:
            off = 0
            continue
        off += len(chunk.encode("utf-8"))
        new += chunk
        if re.search(r"result=\d+|Value=", new):
            time.sleep(0.4)
            _, more = _log_read(st, cfg["log_path"], off)
            new += more
            break
        time.sleep(0.4)
    m = re.search(r"result=(\d+)", new)
    status = ("ok" if m.group(1) == "0" else "ko") if m else ("ok" if "Value=" in new else
                                                              ("inviato" if wait is not None and float(wait) == 0 else "nessuna risposta"))
    val = re.search(r"Value=([^\s,;]+)", new)
    ev.update(status=status, log=new[-3000:], value=val.group(1) if val else None)
    audit(st, "comando AUX", line, status == "ok", status)
    web_event(st, "AUX", ev["label"], line, status, ev["value"], ev=ev)
    return ev


# ---------------------------------------------------------------- sequenze temporizzate

F_SEQS = DATA / "sequences.json"
F_SEQSTATE = DATA / "sequence_state.json"
RECONNECT_MAX = 600        # secondi di attesa massima per il ritorno della connessione


def _ss(kind, label, value="", after=0, wait=True, **kw):
    return {"type": kind, "label": label, "value": value, "after": after, "wait": wait, "advance": "time", **kw}


DEFAULT_SEQS = [{
    "id": "ciclo-ventola", "name": "Ciclo ventola (esempio)", "repeat": 3, "stop_on_error": True,
    "before": [_ss("aux", "Disabilita controllo termico", "05;00;TempContEn;0", 2)],
    "cycle": [_ss("aux", "Ventola accesa", "07;01;00;01;00;01", 30),
              _ss("aux", "Ventola spenta", "07;00;00;01;00;01", 30)],
    "finally": [_ss("aux", "Ventola e riscaldatore spenti", "07;00;00;01;00;01", 1),
                _ss("aux", "Abilita controllo termico", "05;00;TempContEn;1", 0)],
}]
SEQRUNS, SEQCTRL = {}, {}
_STATE_LOCK = threading.Lock()


def sequences():
    return load(F_SEQS, DEFAULT_SEQS)


def _is_conn_error(e):
    if isinstance(e, (ValueError, KeyError, RuntimeError, CommandTimeout)):
        return False
    return isinstance(e, (OSError, EOFError, TimeoutError, paramiko.SSHException))


def _state_save(rid):
    """Memorizza su disco dove si trova l'esecuzione, per poterla riprendere dopo una chiusura."""
    with _STATE_LOCK:
        st = load(F_SEQSTATE, {})
        r = SEQRUNS.get(rid)
        if r and not r["done"]:
            st[rid] = {k: r[k] for k in ("sequence_id", "name", "station_id", "station", "host", "started", "cycle")} | \
                      {"pos": r.get("pos"), "saved": time.time()}
        else:
            st.pop(rid, None)
        save(F_SEQSTATE, st)


class SkipLate(Exception):
    """In tempo reale: la finestra del passo è scaduta mentre si aspettava la connessione."""


def _retry_conn(run, ctrl, fn):
    """Esegue fn; se la connessione cade aspetta che torni e riprova, invece di dare errore."""
    first = None
    while True:
        try:
            out = fn()
            if first:
                run["waiting"] = None
                run["reconnects"] = run.get("reconnects", 0) + 1
            return out
        except Exception as e:
            if not _is_conn_error(e) or ctrl["stop"].is_set():
                raise
            first = first or time.time()
            if time.time() - first > RECONNECT_MAX:
                raise
            run["waiting"] = {"mode": "reconnect", "since": first, "error": describe_error(e)}
            POOL.drop(ctrl["station"]["id"])
            ctrl["stop"].wait(3)
            if ctrl["stop"].is_set():
                raise
            if ctrl.get("win_end") and time.time() > ctrl["win_end"]:
                run["waiting"] = None
                raise SkipLate()


def _web_value(st, var):
    text, _ = web_fetch(st, aux_cfg())
    vars_, _ = parse_vars(text)
    if var not in vars_:
        raise KeyError(f"variabile \"{var}\" non trovata nella pagina")
    return vars_[var]


def _same(a, b):
    a, b = str(a).strip().lower(), str(b).strip().lower()
    na, nb = _num(a), _num(b)
    return (na is not None and nb is not None and abs(na - nb) < 1e-9) or a == b


def _check_web(run, ctrl, st, chk):
    """Verifica sulla pagina web che l'attuazione sia avvenuta (es. flag ventola = 1)."""
    var, want = (chk.get("var") or "").strip(), str(chk.get("value", "")).strip()
    deadline, got = time.time() + float(chk.get("timeout") or 10), None
    while True:
        run["waiting"] = {"mode": "check", "var": var, "want": want, "got": got, "until": deadline}
        got = _retry_conn(run, ctrl, lambda: _web_value(st, var))
        if _same(got, want):
            run["waiting"] = None
            return True, f"pagina: {var} = {got}"
        if time.time() > deadline or ctrl["stop"].is_set():
            run["waiting"] = None
            return False, f"pagina: {var} = {got}, atteso {want}"
        ctrl["stop"].wait(1)


def _advance(run, ctrl, st, step):
    """Attende il momento di passare al passo successivo: a tempo, con il tasto Avanti o a valore stabile."""
    mode = step.get("advance") or "time"
    ctrl["skip"].clear()
    if mode == "key":
        run["waiting"] = {"mode": "key"}
        while not ctrl["skip"].is_set() and not ctrl["stop"].is_set():
            ctrl["skip"].wait(0.5)
        run["waiting"] = None
        return "avanti premuto" if ctrl["skip"].is_set() else ""
    if mode == "stable":
        cfg = step.get("stable") or {}
        var = (cfg.get("var") or "").strip()
        win, tol = float(cfg.get("window") or 60), float(cfg.get("tol") or 0.3)
        tmax = float(cfg.get("max") or 1800)
        t0, pts, note = time.time(), deque(), ""
        while not ctrl["stop"].is_set():
            if ctrl["skip"].is_set():
                note = "avanti premuto"
                break
            v = _num(_retry_conn(run, ctrl, lambda: _web_value(st, var)))
            now = time.time()
            if v is not None:
                pts.append((now, v))
            while pts and pts[0][0] < now - win:
                pts.popleft()
            rng = (max(p[1] for p in pts) - min(p[1] for p in pts)) if pts else None
            covered = now - t0 >= win and len(pts) >= 3
            run["waiting"] = {"mode": "stable", "var": var, "value": v, "range": rng, "tol": tol, "window": win,
                              "elapsed": now - t0, "max": tmax, "stable": bool(covered and rng is not None and rng <= tol * 2)}
            if covered and rng is not None and rng <= tol * 2:
                note = f"{var} stabile a {v} (±{rng / 2:.2f} in {win:.0f} s)"
                break
            if now - t0 > tmax:
                note = f"{var} non stabile entro {tmax / 60:.0f} min, proseguo"
                break
            ctrl["skip"].wait(min(2.0, max(0.5, win / 30)))
        run["waiting"] = None
        return note
    after = float(step.get("after") or 0)
    if after > 0:
        run["waiting"] = {"mode": "time", "until": time.time() + after, "secs": after}
        while not ctrl["skip"].is_set() and not ctrl["stop"].is_set() and time.time() < run["waiting"]["until"]:
            ctrl["skip"].wait(0.3)
        run["waiting"] = None
        return "avanti premuto" if ctrl["skip"].is_set() else ""
    return ""


def _seq_step(run, ctrl, st, step, block, cycle, final=False):
    rt = ctrl.get("realtime") and not final
    ctrl["win_end"] = None
    if rt and (step.get("advance") or "time") == "time":
        start, end = ctrl["sched"], ctrl["sched"] + float(step.get("after") or 0)
        late = time.time() - start
        margin = min(1.0, (end - start) * 0.25)
        ctrl["win_end"] = end - margin
        if time.time() > end - margin and end > start:
            # finestra del passo già passata (es. per una riconnessione): si salta per restare allineati
            with LOCK:
                run["history"].append({"t": time.time(), "block": block, "cycle": cycle,
                                       "label": step.get("label") or step.get("value", ""), "value": step.get("value", ""),
                                       "status": "saltato", "detail": f"in ritardo di {late:.0f} s rispetto all'orario"})
                del run["history"][:-400]
            ctrl["sched"] = end
            run["late"] = round(late)
            return True
        run["late"] = round(max(0.0, late))
    rec = {"t": time.time(), "block": block, "cycle": cycle, "label": step.get("label") or step.get("value", ""),
           "value": step.get("value", ""), "status": "in corso", "detail": ""}
    with LOCK:
        run["history"].append(rec)
        del run["history"][:-400]
        run["current"] = {"block": block, "label": rec["label"]}
    kind = step.get("type", "aux")
    try:
        if kind == "aux":
            def send():
                return aux_send({"station_id": st["id"], "line": step["value"], "label": rec["label"],
                                 "wait": 8 if step.get("wait", True) else 0})
            ev = _retry_conn(run, ctrl, send)
            rec["status"] = {"ok": "ok", "ko": "ko"}.get(ev["status"], "inviato" if not step.get("wait", True) else "nessuna risposta")
            last = [l for l in (ev.get("log") or "").splitlines() if l.strip()]
            rec["detail"] = (f"Value={ev['value']}" if ev.get("value") is not None else
                             (re.sub(r"^[\d:.\-T ]+\s*(\[\w+\]\s*)?", "", last[-1])[:90] if last else ""))
            chk = step.get("check") or {}
            if chk.get("var") and rec["status"] != "ko":
                ok, msg = _check_web(run, ctrl, st, chk)
                rec["detail"] = (rec["detail"] + " | " if rec["detail"] else "") + msg + (" ✓" if ok else " ✗")
                if not ok:
                    rec["status"] = "ko"
        elif kind == "shell":
            code, out, err = _retry_conn(run, ctrl, lambda: ssh_exec(st, render(step["value"], st),
                                                                     timeout=float(step.get("timeout") or 30)))
            rec["status"] = "ok" if code == 0 else "ko"
            first = (out or err).strip().splitlines()
            rec["detail"] = (first[-1][:120] if first else f"exit {code}")
            audit(st, "sequenza shell", step["value"], code == 0, f"exit {code}")
            web_event(st, "shell", rec["label"], step["value"], rec["status"])
        else:
            rec["status"] = "ok"
    except SkipLate:
        rec.update(status="saltato", detail="finestra scaduta durante la riconnessione")
    except Exception as e:
        rec.update(status="errore", detail=describe_error(e))
    ctrl["win_end"] = None
    if final:                                   # nei passi finali niente attese lunghe
        step = dict(step, advance="time", after=min(float(step.get("after") or 0), 10))
    if rt and (step.get("advance") or "time") == "time":
        # tempo reale: si attende fino all'orario previsto di fine passo, non "after" secondi da adesso
        ctrl["sched"] += float(step.get("after") or 0)
        step = dict(step, after=max(0.0, ctrl["sched"] - time.time()))
    try:
        note = _advance(run, ctrl, st, step)
        if note:
            rec["detail"] = (rec["detail"] + " | " if rec["detail"] else "") + note
    except Exception as e:
        rec["detail"] = (rec["detail"] + " | " if rec["detail"] else "") + describe_error(e)
    if rt and (step.get("advance") or "time") != "time" or (rt and ctrl["skip"].is_set()):
        ctrl["sched"] = time.time()             # dopo Avanti o attese non a tempo l'orologio riparte da adesso
    return rec["status"] in ("ok", "inviato", "nessuna risposta", "saltato")


def _seq_thread(rid, start=None):
    run, ctrl = SEQRUNS[rid], SEQCTRL[rid]
    if ctrl.get("realtime"):
        ctrl["stop"].wait(max(0.0, ctrl["sched"] - time.time()))
        if start:
            ctrl["sched"] = time.time()           # ripresa: l'orologio riparte da adesso
    seq, st = ctrl["seq"], ctrl["station"]
    blk0, i0, c0 = (start or {}).get("block", "iniziali"), int((start or {}).get("index", 0)), int((start or {}).get("cycle", 1) or 1)
    ok = True

    def step_at(block, i, c, s, final=False):
        run["pos"] = {"block": block, "index": i, "cycle": c}
        _state_save(rid)
        return _seq_step(run, ctrl, st, s, block, c, final)
    try:
        if blk0 == "iniziali":
            run["phase"] = "iniziali"
            for i, s in enumerate(seq.get("before", [])):
                if i < i0:
                    continue
                if ctrl["stop"].is_set():
                    break
                if not step_at("iniziali", i, 0, s) and seq.get("stop_on_error", True):
                    ok = False
                    break
        if blk0 in ("iniziali", "ciclo"):
            n = int(seq.get("repeat") or 0)
            c = c0 - 1 if blk0 == "ciclo" else 0
            first_i = i0 if blk0 == "ciclo" else 0
            while ok and not ctrl["stop"].is_set() and (n == 0 or c < n) and seq.get("cycle"):
                c += 1
                run.update(phase="ciclo", cycle=c)
                for i, s in enumerate(seq["cycle"]):
                    if i < first_i:
                        continue
                    if ctrl["stop"].is_set():
                        break
                    if not step_at("ciclo", i, c, s) and seq.get("stop_on_error", True):
                        ok = False
                        break
                first_i = 0
    except Exception as e:
        ok, run["error"] = False, describe_error(e)
    stopped = ctrl["stop"].is_set()
    ctrl["stop"] = threading.Event()          # i comandi finali girano sempre
    run["phase"] = "finali"
    for i, s in enumerate(seq.get("finally", [])):
        if blk0 == "finali" and i < i0:
            continue
        step_at("finali", i, 0, s, final=True)
    run.update(done=True, phase="", current=None, ended=time.time(), waiting=None,
               mac=POOL.ident.get(st["id"], {}).get("mac", ""),
               result="FERMATA" if stopped and ok else ("OK" if ok else "KO"))
    _state_save(rid)
    audit(st, "sequenza", seq["name"], ok, run["result"])


def _new_run(seq, st, extra=None, t0=None):
    rid = uuid.uuid4().hex[:10]
    SEQRUNS[rid] = {"id": rid, "sequence_id": seq["id"], "name": seq["name"], "station_id": st["id"],
                    "station": st["name"], "host": st["host"], "started": time.time(), "done": False,
                    "result": None, "phase": "", "cycle": 0, "cycles": int(seq.get("repeat") or 0),
                    "current": None, "waiting": None, "history": [], "error": "", "pos": None, "reconnects": 0,
                    **(extra or {})}
    rt = seq.get("timing") == "reale"
    SEQCTRL[rid] = {"stop": threading.Event(), "skip": threading.Event(), "seq": seq, "station": st,
                    "realtime": rt, "sched": t0 or time.time()}
    SEQRUNS[rid].update(realtime=rt, t0=t0 or time.time(), late=0)
    return rid


def _validate_seq(seq):
    for s in seq.get("before", []) + seq.get("cycle", []) + seq.get("finally", []):
        if s.get("type") == "aux" and not LINE_RX.match((s.get("value") or "").strip()):
            raise ValueError(f"comando AUX non valido nel passo \"{s.get('label')}\": {s.get('value')}")
        if s.get("advance") == "stable" and not (s.get("stable") or {}).get("var"):
            raise ValueError(f"nel passo \"{s.get('label')}\" manca la variabile da attendere stabile")


def seq_start(body):
    seq = next((s for s in sequences() if s["id"] == body.get("sequence_id")), None)
    if not seq:
        raise KeyError("sequenza non trovata")
    sts = [s for s in stations() if s["id"] in (body.get("station_ids") or [])]
    if not sts:
        raise ValueError("seleziona almeno una colonnina")
    busy = [s["name"] for s in sts if any(not r["done"] and r["station_id"] == s["id"] for r in SEQRUNS.values())]
    if busy:
        raise ValueError("sequenza già in corso su: " + ", ".join(busy))
    _validate_seq(seq)
    ids = []
    t0 = time.time() + 1.5                    # stesso istante di partenza per tutte: in tempo reale restano allineate
    for st in sts:
        rid = _new_run(seq, st, t0=t0)
        audit(st, "sequenza", seq["name"], True, "avviata")
        threading.Thread(target=_seq_thread, args=(rid,), daemon=True).start()
        ids.append(rid)
    for old in sorted(SEQRUNS, key=lambda k: SEQRUNS[k]["started"])[:-30]:
        if SEQRUNS[old]["done"] and not SEQRUNS[old].get("resumable"):
            SEQRUNS.pop(old, None)
            SEQCTRL.pop(old, None)
    return ids


def seq_resume(body):
    """Riprende un'esecuzione interrotta dalla chiusura di PoleDesk, dallo stesso passo o solo con i finali."""
    old = SEQRUNS.get(body.get("id") or "")
    if not old or not old.get("resumable"):
        raise KeyError("esecuzione da riprendere non trovata")
    seq = next((s for s in sequences() if s["id"] == old["sequence_id"]), None)
    if not seq:
        raise KeyError("la sequenza non esiste più")
    st = station_by_id(old["station_id"])
    if any(not r["done"] and r["station_id"] == st["id"] for r in SEQRUNS.values()):
        raise ValueError("sulla colonnina c'è già una sequenza in corso")
    pos = old.get("pos") or {"block": "iniziali", "index": 0, "cycle": 0}
    start = {"block": "finali", "index": 0} if body.get("finals_only") else pos
    old.update(resumable=False, result="RIPRESA", error="ripresa in una nuova esecuzione")
    rid = _new_run(seq, st, {"resumed_from": old["id"]})
    audit(st, "sequenza", seq["name"], True, "ripresa" + (" (solo finali)" if body.get("finals_only") else ""))
    threading.Thread(target=_seq_thread, args=(rid, start), daemon=True).start()
    with _STATE_LOCK:
        stt = load(F_SEQSTATE, {})
        stt.pop(old["id"], None)
        save(F_SEQSTATE, stt)
    return rid


def seq_restore():
    """All'avvio: le esecuzioni rimaste a metà diventano 'interrotte' e si possono riprendere."""
    for rid, r in load(F_SEQSTATE, {}).items():
        seq = next((s for s in sequences() if s["id"] == r["sequence_id"]), None)
        SEQRUNS[rid] = {"id": rid, "sequence_id": r["sequence_id"], "name": r["name"], "station_id": r["station_id"],
                        "station": r["station"], "host": r["host"], "started": r["started"], "done": True,
                        "result": "INTERROTTA", "phase": "", "cycle": r.get("cycle", 0),
                        "cycles": int((seq or {}).get("repeat") or 0), "current": None, "waiting": None,
                        "history": [], "error": "PoleDesk è stato chiuso durante l'esecuzione", "pos": r.get("pos"),
                        "resumable": True, "saved": r.get("saved")}


def seq_stop(body):
    targets = [body["id"]] if body.get("id") else [k for k, r in SEQRUNS.items() if not r["done"]]
    for k in targets:
        if k in SEQCTRL:
            SEQCTRL[k]["stop"].set()
            SEQCTRL[k]["skip"].set()


def seq_next(body):
    ctrl = SEQCTRL.get(body.get("id") or "")
    if not ctrl:
        raise KeyError("esecuzione non trovata")
    ctrl["skip"].set()


def seq_dismiss(body):
    r = SEQRUNS.get(body.get("id") or "")
    if r and r.get("resumable"):
        SEQRUNS.pop(r["id"], None)
        with _STATE_LOCK:
            stt = load(F_SEQSTATE, {})
            stt.pop(r["id"], None)
            save(F_SEQSTATE, stt)


# ---------------------------------------------------------------- schede di rete del PC (Windows)

_PO = {0: "Other", 1: "Manual", 2: "WellKnown", 3: "Dhcp", 4: "RouterAdvertisement"}


def _ps(cmd, timeout=30):
    import subprocess
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                       capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, r.stdout, r.stderr


def _as_list(x):
    return [] if x is None else (x if isinstance(x, list) else [x])


def net_parse(data):
    """Unisce schede, indirizzi e gateway (JSON di PowerShell) in un elenco leggibile."""
    gw = {g.get("ifIndex") for g in _as_list(data.get("g")) if g.get("NextHop") not in (None, "", "0.0.0.0")}
    ips = {}
    for i in _as_list(data.get("i")):
        o = i.get("PrefixOrigin")
        o = _PO.get(o, o) if isinstance(o, int) else str(o)
        ips.setdefault(i.get("InterfaceIndex"), []).append({"ip": i.get("IPAddress"), "prefix": i.get("PrefixLength"), "origin": o})
    out = []
    for a in _as_list(data.get("a")):
        idx = a.get("ifIndex")
        lst = [x for x in ips.get(idx, []) if not str(x["ip"]).startswith("169.254.")]
        out.append({"name": a.get("Name"), "desc": a.get("InterfaceDescription"), "status": str(a.get("Status")),
                    "mac": (a.get("MacAddress") or "").replace("-", ":").lower(), "index": idx, "ips": lst,
                    "dhcp": any(x["origin"] == "Dhcp" for x in lst), "gateway": idx in gw})
    out.sort(key=lambda x: (x["status"] != "Up", x["name"] or ""))
    return out


def net_list():
    if os.name != "nt":
        raise RuntimeError("la gestione delle schede di rete è disponibile solo su Windows")
    code, out, err = _ps("$a=@(Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,MacAddress,ifIndex);"
                         "$i=@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object InterfaceIndex,IPAddress,PrefixLength,PrefixOrigin);"
                         "$g=@(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object ifIndex,NextHop);"
                         "@{a=$a;i=$i;g=$g} | ConvertTo-Json -Depth 4 -Compress")
    if code or not out.strip():
        raise RuntimeError("impossibile leggere le schede di rete: " + (err.strip() or "PowerShell non ha risposto"))
    return net_parse(json.loads(out))


def _mask(prefix):
    p = int(prefix)
    m = (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF if p else 0
    return ".".join(str((m >> s) & 255) for s in (24, 16, 8, 0))


def net_set(body):
    """Imposta IP statico o DHCP su una scheda. Richiede conferma da amministratore (UAC)."""
    adapters = net_list()
    name = body.get("name", "")
    ad = next((a for a in adapters if a["name"] == name), None)
    if not ad:
        raise ValueError("scheda di rete non trovata")
    if ad["gateway"] and not body.get("confirm_gateway"):
        raise ValueError("questa scheda porta alla rete aziendale (ha il gateway): conferma esplicitamente la modifica")
    q = name.replace('"', "")
    if body.get("mode") == "dhcp":
        args = f'interface ipv4 set address name="{q}" source=dhcp'
    else:
        ip = (body.get("ip") or "").strip()
        if not re.fullmatch(r"(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}", ip):
            raise ValueError("indirizzo IP non valido")
        prefix = int(body.get("prefix") or 24)
        if not 8 <= prefix <= 30:
            raise ValueError("maschera non valida")
        args = f'interface ipv4 set address name="{q}" static {ip} {_mask(prefix)}'
    ps_args = args.replace("'", "''")
    code, out, err = _ps(f"try {{ $p = Start-Process -FilePath netsh -ArgumentList '{ps_args}' -Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode }} "
                         f"catch {{ Write-Error $_.Exception.Message; exit 1223 }}", timeout=120)
    if code == 1223 or "annullat" in err.lower() or "cancel" in err.lower():
        raise RuntimeError("operazione annullata: serve la conferma da amministratore di Windows")
    if code:
        raise RuntimeError(f"Windows non ha applicato la modifica (codice {code}) {err.strip()[:200]}")
    time.sleep(2)
    now = next((a for a in net_list() if a["name"] == name), ad)
    audit(None, "rete PC", f"{name}: {args.split(name + chr(34))[-1].strip()}", True)
    return now


# ---------------------------------------------------------------- profilo termico v3 (indice di distacco schermo)

F_PROFILE = DATA / "profile.json"
PROF_DIR = DATA / "profiles"
PROF_DIR.mkdir(exist_ok=True)
F_PROF_SUMMARY = PROF_DIR / "riepilogo_v3.csv"
F_PROF_REF = PROF_DIR / "riferimenti.json"
F_PROF_PENDING = PROF_DIR / "ripristino.json"

PROFILE_DEFAULT = {
    "spec": "v3",
    "vars": {"cuid": "CUID", "aux": "AUX TEMPERATURE", "temp": "TEMPERATURE", "cpu": "CPU-TEMPERATURE",
             "led": "AUX LEDS CURRENT", "lux": "AUX LUX", "vmain": "MAIN POWER VOLTAGE", "v12": "AUX SUPPLY 12V",
             "user": "USER STATUS", "auxfault": "AUX FAULT", "tamper": "ANTITAMPER",
             "heater_state": "AUX HEATER POWER", "fan_state": "AUX FAN POWER", "charge": ""},
    "phases": [
        {"role": "rif", "name": "Riferimento", "heater": 0, "fan": 0, "s": 300},
        {"role": "carica", "name": "Carica 1", "heater": 1, "fan": 0, "s": 180},
        {"role": "misura", "name": "Misura 1", "heater": 0, "fan": 1, "s": 300},
        {"role": "recupero", "name": "Recupero", "heater": 0, "fan": 0, "s": 600},
        {"role": "carica", "name": "Carica 2", "heater": 1, "fan": 0, "s": 180},
        {"role": "misura", "name": "Misura 2", "heater": 0, "fan": 1, "s": 300},
    ],
    "rules_ver": 16,
    "sample_s": 5,
    "pre": {"uptime_h": 3, "stable_min": 30, "stable_tol": 0.3, "led_tol_pct": 10, "latency_ms": 3000,
            "max_wait_h": 3, "not_before": "", "rest_min": 60, "rif_min_s": 240,
            "night_required": True, "night_from": "19:00", "night_to": "06:00", "lux_max": None},
    "valid": {"heater_s": 180, "heater_tol_s": 10, "pause_max_s": 10, "gap_max_s": 15, "sample_max_s": 6, "user_ok": "IDLE", "lux_sun": None,
              "drift_max": 0.03, "drift_correct": True},
    "pair": {"wait_min": 20},
    "watchdog": {"on": True, "margin_min": 15},
    "meteo": {"on": False, "lat": "", "lon": ""},
    # metodo assoluto: regole per modello (CUID che contiene "match"); sospetto se almeno sus_min condizioni vere (1 = OR),
    # attaccato se almeno att_min condizioni vere (0 = tutte). Soglie provvisorie, da validare con le prove.
    "absolute": {"models": [
        {"name": "T3QQ (096, 097)", "match": "T3QQ", "sus_min": 1, "att_min": 0,
         "sus": [{"ind": "F_medio", "op": ">=", "thr": 0.6}, {"ind": "tau_medio", "op": "<=", "thr": 60}],
         "att": [{"ind": "F_medio", "op": "<=", "thr": 0.35}, {"ind": "tau_medio", "op": ">=", "thr": 150}]},
        {"name": "T5PQ (004)", "match": "T5PQ", "sus_min": 1, "att_min": 0,
         "sus": [{"ind": "F_medio", "op": ">=", "thr": 0.6}, {"ind": "tau_medio", "op": "<=", "thr": 60}],
         "att": [{"ind": "F_medio", "op": "<=", "thr": 0.35}, {"ind": "tau_medio", "op": ">=", "thr": 150}]},
        {"name": "altri modelli", "match": "", "sus_min": 1, "att_min": 0,
         "sus": [{"ind": "F_medio", "op": ">=", "thr": 0.6}], "att": [{"ind": "F_medio", "op": "<=", "thr": 0.35}]}]},
    "index": {"win_s": 15, "after_on_s": 45, "c_max": 0.30, "a_low": 1.1, "a_high": 1.4,
              "q_low": 1.3, "q_high": 1.8, "ref_min": 3, "dt_ref": None,
              "p_att_low": 0.7, "p_att_high": 1.4, "p_sus_low": 0.4, "p_sus_high": 2.0, "combine": "I", "k_min_dt": 0.5},
    "safety": {"aux_max": None, "temp_max": None, "v12_min": None, "v12_max": None, "vmain_min": None,
               "vmain_max": None, "comm_loss_s": 30, "user_fault": "FAULT", "tamper_open": "", "charge_on": ""},
}
PROF_ROLES = {"cuid": "CUID", "aux": "AUX TEMPERATURE", "temp": "TEMPERATURE", "cpu": "CPU-TEMPERATURE",
              "led": "AUX LEDS CURRENT", "lux": "AUX LUX", "vmain": "MAIN POWER VOLTAGE", "v12": "AUX SUPPLY 12V",
              "user": "USER STATUS", "auxfault": "AUX FAULT", "tamper": "ANTITAMPER",
              "heater_state": "stato letto del riscaldatore *", "fan_state": "stato letto della ventola *",
              "charge": "stato ricarica"}
CSV_COLS = ["timestamp", "cuid", "passo", "nome_passo", "cmd_riscaldatore", "cmd_ventola", "stato_riscaldatore",
            "stato_ventola", "aux_temperature", "temperature", "cpu_temperature", "aux_leds_current", "aux_lux",
            "main_power_voltage", "aux_supply_12v", "user_status", "aux_fault", "antitamper", "ok", "latency_ms"]
F_PROF_LAST = None   # impostato sotto, dopo PROF_DIR
SUMMARY_COLS = ["file", "cuid", "colonnina", "data_ora", "tipo", "configurazione", "ripetizione", "riferimento", "rif_cuid",
                "luogo", "temp_esterna", "livello_led", "valida", "motivo", "I1", "I2", "I_medio", "C", "I0", "Q", "esito_I",
                "S1", "S2", "S_medio", "S0", "P", "esito_S", "combinazione", "n_rif", "metodo", "esito",
                "delta", "R", "calo_aux_60s", "pendenza_carica", "T_aux_on", "I_n", "campioni", "note", "forzata",
                "T_base", "k1", "k2", "k_medio", "tau1", "tau2", "tau_medio", "lux_medio",
                "meteo_temp", "meteo_vento", "meteo_nuvole", "meteo_irr", "campagna",
                "coppia", "ruolo_coppia", "file_coppia", "Q_coppia", "P_coppia", "esito_coppia", "fuori_rif", "origine",
                "modello", "T_aria", "F1", "F2", "F_medio", "Tinf1", "Tinf2", "Tinf_medio", "fit_r2", "k_aria_medio", "dT_riposo",
                "esito_abs", "regole_abs", "deriva", "notte"]
COMBINE_LBL = {"I": "solo I", "S": "solo S", "IS": "I+S", "ABS": "assoluto", "ABS_OR": "assoluto O relativo"}
ABS_IND = {"F_medio": "F (frazione recuperata in 120 s)", "tau_medio": "τ (s)", "k_aria_medio": "k rispetto all'aria (1/min)",
           "dT_riposo": "AUX − T aria a riposo (°C)", "Tinf_aria": "T∞ − T aria (°C)", "I_medio": "Ī (°C/min)", "S_medio": "S̄ (°C/min)",
           "k_medio": "k (1/min)", "delta_medio": "Δ = TEMP − AUX (°C)", "calo60_medio": "calo a 60 s (°C)"}
PROFRUNS, PROFCTRL = {}, {}


class ProfAbort(Exception):
    pass


def profile_cfg():
    saved = load(F_PROFILE, {})
    if saved.get("spec") != "v3":                 # configurazione della v2: si tengono variabili e soglie, si adotta la sequenza v3
        saved = {k: v for k, v in saved.items() if k in ("vars", "safety", "pre", "sample_s")}
        if (saved.get("safety") or {}).get("comm_loss_s") in (None, 60):
            saved.setdefault("safety", {})["comm_loss_s"] = 30
    out = json.loads(json.dumps(PROFILE_DEFAULT))
    for k, v in saved.items():
        out[k] = {**out[k], **v} if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    out["spec"] = "v3"
    for role in ("heater_state", "fan_state"):          # 1.11: stato letto predefinito dalla pagina della colonnina
        if not out["vars"].get(role):
            out["vars"][role] = PROFILE_DEFAULT["vars"][role]
    if out["index"].get("combine") not in COMBINE_LBL:
        out["index"]["combine"] = "I"
    if int(saved.get("rules_ver") or 0) < 16:          # 1.16: tempi dalle prove del 5 e 9/11 (una volta, poi si salva)
        out["phases"] = json.loads(json.dumps(PROFILE_DEFAULT["phases"]))
        pr = out["pre"]
        pr["uptime_h"] = max(float(pr.get("uptime_h") or 0), 3)
        pr["stable_min"] = max(float(pr.get("stable_min") or 0), 30)
        pr["stable_tol"] = min(float(pr.get("stable_tol") or 0.3), 0.3)
        out["rules_ver"] = 16
        out["_migrated"] = "16"
    return out


def _prof_alarm(s, cfg, run):
    """Condizioni per interrompere subito (§4): soglie, ricarica, FAULT, comunicazione, cambio colonnina."""
    sf, n, raw = cfg["safety"], s["num"], s["raw"]
    if s["ok"]:
        run["_last_ok"] = s["t"]
    elif s["t"] - run.get("_last_ok", s["t"]) > float(sf.get("comm_loss_s") or 30):
        return f"comunicazione persa da più di {sf.get('comm_loss_s') or 30} s"
    if not s["ok"]:
        return None
    for role, key, label in (("aux", "aux_max", "AUX TEMPERATURE"), ("temp", "temp_max", "TEMPERATURE")):
        lim = _flt(sf.get(key))
        if lim is not None and n.get(role) is not None and n[role] > lim:
            return f"{label} {n[role]} oltre la soglia di sicurezza {lim}"
    for role, lo, hi, label in (("v12", "v12_min", "v12_max", "AUX SUPPLY 12V"), ("vmain", "vmain_min", "vmain_max", "MAIN POWER VOLTAGE")):
        v = n.get(role)
        if v is not None and ((_flt(sf.get(lo)) is not None and v < _flt(sf.get(lo))) or (_flt(sf.get(hi)) is not None and v > _flt(sf.get(hi)))):
            return f"{label} {v} fuori dai limiti"
    if _rx(sf.get("user_fault"), raw.get("user")):
        return f"FAULT: USER STATUS = {raw.get('user')}"
    if _rx(sf.get("charge_on"), raw.get("charge")):
        return f"inizio di una ricarica ({raw.get('charge')})"
    if run.get("cuid0") and raw.get("cuid") and raw["cuid"] != run["cuid0"]:
        return f"colonnina cambiata: CUID {raw['cuid']} invece di {run['cuid0']}"
    return None


def _resolve(vars_, name):
    """Trova la variabile della pagina: nome esatto, poi senza maiuscole, poi come parte finale del nome."""
    if not name:
        return None
    if name in vars_:
        return vars_[name]
    low = {k.lower(): v for k, v in vars_.items()}
    if name.lower() in low:
        return low[name.lower()]
    cand = [v for k, v in low.items() if k.endswith(name.lower()) or k.endswith("." + name.lower())]
    return cand[0] if len(cand) == 1 else None

def _prof_sample(st, cfg):
    t = time.time()
    try:
        text, ms = web_fetch(st, aux_cfg())
        vars_, _ = parse_vars(text)
        ok, err = (1 if vars_ else 0), ""
    except Exception as e:
        vars_, ms, ok, err = {}, None, 0, describe_error(e)
    raw = {r: _resolve(vars_, n) for r, n in cfg["vars"].items() if n}
    if raw.get("cuid"):
        raw["cuid"] = str(raw["cuid"]).split()[0]        # il CUID non contiene spazi
    return {"t": t, "ok": ok, "ms": ms, "err": err, "raw": raw, "num": {r: _num(v) if v is not None else None for r, v in raw.items()}}

def _rx(pattern, value):
    return bool(pattern) and value is not None and re.search(pattern, str(value), re.I) is not None

def _flt(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None

def _hm(x, dflt):
    try:
        h, m = str(x or dflt).split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        h, m = dflt.split(":")
        return int(h) * 60 + int(m)


def _in_night(ts, pre):
    """True se l'ora è nella finestra notturna (es. 19:00–06:00, anche a cavallo della mezzanotte)."""
    d = datetime.fromtimestamp(ts)
    t, a, b = d.hour * 60 + d.minute, _hm(pre.get("night_from"), "19:00"), _hm(pre.get("night_to"), "06:00")
    return (a <= t < b) if a < b else (t >= a or t < b)


def _prof_conditions(st, cfg, hist, run, ctrl):
    """Condizioni per partire (§4): ognuna con esito e spiegazione."""
    pre, sf, out = cfg["pre"], cfg["safety"], []
    last = hist[-1]
    nb = run.get("_not_before")
    if nb:
        out.append({"name": "Orario di partenza", "ok": time.time() >= nb, "detail": f"non prima delle {datetime.fromtimestamp(nb):%H:%M}"})
    need = float(pre.get("uptime_h") or 0)
    if need > 0:
        if time.time() - run.get("_upt_t", 0) > 60:
            try:
                _, o, _ = ssh_exec(st, "cat /proc/uptime", timeout=10)
                run["_upt"], run["_upt_t"] = float(o.split()[0]) / 3600, time.time()
            except Exception as e:
                run["_upt"], run["_upt_t"] = None, time.time()
                run["_upt_err"] = describe_error(e)
        u = run.get("_upt")
        out.append({"name": f"Accesa da almeno {need:g} h", "ok": u is not None and u >= need,
                    "detail": f"accesa da {u:.1f} h" if u is not None else f"non verificabile: {run.get('_upt_err', '')}"})
    win = float(pre.get("stable_min") or 30) * 60
    span = hist[-1]["t"] - hist[0]["t"] if len(hist) > 1 else 0
    tol = float(pre.get("stable_tol") or 0.5)
    for role, label in (("aux", "AUX TEMPERATURE"), ("temp", "TEMPERATURE")):
        vals = [h["num"].get(role) for h in hist if h["num"].get(role) is not None]
        rng = (max(vals) - min(vals)) if vals else None
        out.append({"name": f"{label} stabile ({tol:g} °C in {win / 60:g} min)",
                    "ok": bool(vals) and span >= win * 0.98 and rng < tol,
                    "detail": "variabile non trovata nella pagina" if not vals else f"variazione {rng:.2f} °C su {span / 60:.0f}/{win / 60:g} min"})
    if cfg["vars"].get("led"):
        recent = [h["num"].get("led") for h in hist if h["t"] >= last["t"] - 300 and h["num"].get("led") is not None]
        lt = float(pre.get("led_tol_pct") or 10) / 100
        if recent:
            m = sum(recent) / len(recent)
            okl = all(abs(v - m) <= lt * abs(m) for v in recent) if m else max(recent) - min(recent) < 0.05
            out.append({"name": f"Corrente LED stabile (±{lt * 100:g}%)", "ok": okl, "detail": f"{min(recent):g}–{max(recent):g}"})
        else:
            out.append({"name": "Corrente LED stabile", "ok": False, "detail": "variabile non trovata nella pagina"})
    rest = float(pre.get("rest_min") or 0)
    if rest > 0:
        lt = load(F_PROF_LAST, {}).get(st["id"])
        ago = (time.time() - lt) / 60 if lt else None
        out.append({"name": f"Almeno {rest:g} min dalla prova precedente", "ok": ago is None or ago >= rest,
                    "detail": "nessuna prova recente" if ago is None else f"ultima prova finita {ago:.0f} min fa"})
    if pre.get("night_required"):
        inw = _in_night(time.time(), pre)
        lm, lx = _flt(pre.get("lux_max")), last["num"].get("lux")
        lux_ok = lm is None or lx is None or lx <= lm
        nm = f"Al buio ({pre.get('night_from') or '19:00'}–{pre.get('night_to') or '06:00'}{f', LUX ≤ {lm:g}' if lm is not None else ''})"
        if run.get("pair"):
            out.append({"name": nm, "ok": True, "warn": not (inw and lux_ok),
                        "detail": "di notte" if inw and lux_ok else "di giorno: vale solo il confronto in coppia"})
        else:
            out.append({"name": nm, "ok": inw and lux_ok,
                        "detail": ("fuori dalla finestra notturna: aspetto (o usa la prova in coppia)" if not inw else
                                   f"troppa luce: LUX {lx:g}" if not lux_ok else "ok")})
    if cfg["vars"].get("charge") and sf.get("charge_on"):
        seen = [h["t"] for h in hist if _rx(sf["charge_on"], h["raw"].get("charge"))]
        run["_charge_seen"] = max(seen + [run.get("_charge_seen", 0)])
        free = (time.time() - run["_charge_seen"]) / 60 if run["_charge_seen"] else span / 60
        out.append({"name": "Nessuna ricarica", "ok": not _rx(sf["charge_on"], last["raw"].get("charge")) and (not run["_charge_seen"] or free >= 120),
                    "detail": f"nessuna ricarica osservata da {free:.0f} min (le ultime 2 h sono verificabili solo se osservate)"})
    else:
        out.append({"name": "Nessuna ricarica", "ok": True, "detail": "non verificata: imposta variabile e valore di ricarica", "warn": True})
    out.append({"name": "Comunicazione OK", "ok": bool(last["ok"]) and (last["ms"] or 0) <= float(pre.get("latency_ms") or 3000),
                "detail": f"latenza {last['ms']} ms" if last["ok"] else (last["err"] or "pagina non letta")})
    faults = [r for r in (_prof_alarm(dict(last), cfg, {}),) if r]
    out.append({"name": "Nessun allarme (fault, antitamper, soglie)", "ok": not faults, "detail": faults[0] if faults else "ok"})
    return out

def _pending_set(st, on, wd=""):
    with LOCK:
        _pending_set_unlocked(st, on, wd)


def _pending_set_unlocked(st, on, wd=""):
    p = load(F_PROF_PENDING, {})
    if on:
        p[st["id"]] = {"station": st["name"], "host": st["host"], "since": time.time(), "wd": wd or p.get(st["id"], {}).get("wd", "")}
    else:
        p.pop(st["id"], None)
    save(F_PROF_PENDING, p)

def profile_restore(body):
    """Spegne riscaldatore e ventola e riattiva il controllo termico su una colonnina rimasta a metà prova."""
    st = station_by_id(body.get("station_id", ""))
    res = []
    for line, label in (("07;00;00;01;00;01", "Riscaldatore e ventola spenti"), ("05;00;TempContEn;1", "Controllo termico riattivato")):
        ev = aux_send({"station_id": st["id"], "line": line, "label": label})
        res.append({"label": label, "status": ev["status"]})
    val = _tempconten(st)
    res.append({"label": "Verifica TempContEn", "status": "ok" if val == "1" else f"letto {val}" if val else "nessuna risposta"})
    ok = all(r["status"] == "ok" for r in res[:2]) and val != "0"
    if ok:
        _wd_stop(st, load(F_PROF_PENDING, {}).get(st["id"], {}).get("wd"))
        _pending_set(st, False)
    audit(st, "ripristino controllo termico", "", ok, "; ".join(f"{r['label']}: {r['status']}" for r in res))
    return {"ok": ok, "steps": res}

def _prof_last(s):
    n = s["num"]
    d = (n["temp"] - n["aux"]) if n.get("temp") is not None and n.get("aux") is not None else None
    return {"aux": n.get("aux"), "temp": n.get("temp"), "led": n.get("led"), "vmain": n.get("vmain"),
            "cuid": s["raw"].get("cuid"), "delta": round(d, 2) if d is not None else None}

def _aux_line(heater, fan):
    return f"07;{int(bool(fan)):02d};{int(bool(heater)):02d};01;00;01"

def _onoff(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    return s in ("1", "on", "true", "acceso", "attivo", "yes", "si", "sì") or (_num(s) is not None and _num(s) > 0)

def _slope(pts):
    """Pendenza (°C al minuto) con regressione lineare."""
    pts = [(t, v) for t, v in pts if v is not None]
    if len(pts) < 3:
        return None
    n = len(pts)
    mt, mv = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    den = sum((p[0] - mt) ** 2 for p in pts)
    return (sum((p[0] - mt) * (p[1] - mv) for p in pts) / den * 60) if den else None

def _safe_name(x):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(x or "")).strip("-") or "na"

def _deriv_at(rows, ti, win):
    """Dᵢ: pendenza della regressione di AUX sui campioni con |tₖ − tᵢ| ≤ win, in °C/min (§5)."""
    return _slope([(r["t"], r["aux"]) for r in rows if abs(r["t"] - ti) <= win])


def _expfit(pts):
    """Adatta y = T∞ + A·e^(−t/τ) (t in s dall'accensione della ventola) con τ libero.
    Per ogni τ la retta in e^(−t/τ) si risolve esatta; τ si cerca su griglia logaritmica e poi si affina.
    Restituisce τ (s), T∞, A, R²; τ None se la curva non si stabilizza nella finestra."""
    pts = [(t, y) for t, y in pts if y is not None]
    if len(pts) < 8:
        return None
    n = len(pts)
    my = sum(y for _, y in pts) / n
    sst = sum((y - my) ** 2 for _, y in pts) or 1e-12

    def sse(tau):
        xs = [math.exp(-t / tau) for t, _ in pts]
        mx = sum(xs) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx < 1e-12:
            return float("inf"), None, None
        A = sum((x - mx) * (y - my) for x, (_, y) in zip(xs, pts)) / sxx
        T = my - A * mx
        return sum((y - T - A * x) ** 2 for x, (_, y) in zip(xs, pts)), T, A
    grid = [3 * (1.12 ** i) for i in range(int(math.log(2000 / 3) / math.log(1.12)) + 1)]
    vals = [sse(g)[0] for g in grid]
    j = min(range(len(grid)), key=lambda k: vals[k])
    lo, hi = math.log(grid[max(0, j - 1)]), math.log(grid[min(len(grid) - 1, j + 1)])
    for _ in range(40):                                  # sezione aurea in log τ
        a, b = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
        if sse(math.exp(a))[0] < sse(math.exp(b))[0]:
            hi = b
        else:
            lo = a
    tau = math.exp((lo + hi) / 2)
    e, T, A = sse(tau)
    span = pts[-1][0] - pts[0][0]
    if T is None or A is None or A <= 0:
        return {"tau": None, "Tinf": None, "A": A, "r2": None}
    # τ molto più lungo della finestra: la stima è approssimata (ma sicuramente lunga)
    return {"tau": tau, "Tinf": T, "A": A, "r2": 1 - e / sst, "lungo": j >= len(grid) - 2 or tau > 3 * span}


def _tau(rm, t_on, T_base):
    """Costante di tempo (s) del raffreddamento: regressione di ln(AUX − T_base) nella misura."""
    if T_base is None:
        return None
    pts = [(r["t"], math.log(r["aux"] - T_base)) for r in rm if r["t"] >= t_on and r["aux"] - T_base > 0.3]
    sl = _slope(pts) if len(pts) >= 5 else None
    return -60.0 / sl if sl and sl < 0 else None


def _nearest(rows, t, key):
    best = min(rows, key=lambda r: abs(r["t"] - t), default=None)
    return best[key] if best else None


def _prof_evaluate(rows, phase_t, run, cfg):
    """Validità (§4), indice I₁ I₂ Ī C (§5), grandezze di analisi e classificazione (§6)."""
    va, ix, pre = cfg["valid"], cfg["index"], cfg["pre"]
    reasons, notes = [], []
    good = [r for r in rows if r["ok"] and r["aux"] not in (None, 0.0)]
    dropped = len(rows) - len(good)
    if dropped:
        notes.append(f"{dropped} campioni scartati (OK = 0 o valori impossibili)")
    if len(good) < 5:
        return {"valid": False, "reasons": ["troppo pochi campioni validi"], "notes": notes}
    dts = sorted(b["t"] - a["t"] for a, b in zip(good, good[1:]))
    med = dts[len(dts) // 2] if dts else 0
    if med > float(va.get("sample_max_s") or 6):
        reasons.append(f"intervallo di campionamento {med:.1f} s oltre {va.get('sample_max_s')} s")
    ref, tol = run.get("led_ref"), float(pre.get("led_tol_pct") or 10) / 100
    leds = [r["led"] for r in good if r["led"] is not None]
    if ref and leds and any(abs(v - ref) > tol * abs(ref) for v in leds):
        reasons.append(f"AUX LEDS CURRENT fuori ±{tol * 100:.0f}% (riferimento {ref:.2f}, valori {min(leds):g}–{max(leds):g})")
    bad_user = [r["user"] for r in good if va.get("user_ok") and r["user"] is not None and not _rx(va["user_ok"], r["user"])]
    if bad_user:
        reasons.append(f"USER STATUS diverso da {va['user_ok']} ({bad_user[0]})")
    if any(r["auxfault"] not in (None, 0.0) for r in good):
        reasons.append(f"AUX FAULT diverso da 0 ({next(r['auxfault_raw'] for r in good if r['auxfault'] not in (None, 0.0))})")
    if cfg["safety"].get("tamper_open") and any(_rx(cfg["safety"]["tamper_open"], r["tamper"]) for r in good):
        reasons.append("ANTITAMPER aperto durante la prova")
    has_h, has_f = bool(cfg["vars"].get("heater_state")), bool(cfg["vars"].get("fan_state"))
    if not (has_h and has_f):
        reasons.append("stato letto di riscaldatore e ventola non configurato: durata e t_on non verificabili")
    phases = cfg["phases"]
    # temperatura di partenza: AUX medio negli ultimi 60 s del riferimento (o prima della prima carica)
    irif = next((i for i, ph in enumerate(phases) if ph.get("role") == "rif" and i in phase_t), None)
    icar = next((i for i, ph in enumerate(phases) if ph.get("role") == "carica" and i in phase_t), None)
    t_end = phase_t.get(irif + 1) if irif is not None else (phase_t.get(icar) if icar is not None else None)
    base = [r["aux"] for r in good if t_end is not None and t_end - 60 <= r["t"] < t_end]
    if not base and icar is not None:                    # senza riferimento: primi campioni della carica
        base = [r["aux"] for r in good if phase_t[icar] <= r["t"] < phase_t[icar] + 10]
    T_base = sum(base) / len(base) if base else None
    T_air = _flt(run.get("t_air"))
    # deriva della base: pendenza di AUX negli ultimi 180 s del riferimento (°C/min)
    drift = None
    if irif is not None and t_end is not None:
        drift = _slope([(r["t"], r["aux"]) for r in good if t_end - 180 <= r["t"] < t_end])
    dmax = _flt(va.get("drift_max"))
    if drift is None:
        notes.append("passo di riferimento assente o troppo corto: deriva della base non misurata")
    elif dmax is not None and abs(drift) > dmax:
        reasons.append(f"base in deriva: AUX cambia di {drift:+.3f} °C/min prima della carica (massimo ±{dmax:g}); colonnina non in equilibrio")
    dcor = drift if drift is not None and va.get("drift_correct", True) else 0.0
    if dcor:
        notes.append(f"indici corretti per la deriva della base ({drift:+.3f} °C/min)")
    t0 = good[0]["t"]
    pre_ = cfg.get("pre") or {}
    lux_rif = [r.get("lux") for r in good if r.get("lux") is not None and (t_end is None or r["t"] < t_end)]
    lm = _flt(pre_.get("lux_max"))
    notte = _in_night(t0, pre_) and (lm is None or not lux_rif or sum(lux_rif) / len(lux_rif) <= lm)
    if pre_.get("night_required") and not notte and not run.get("in_coppia"):
        reasons.append(f"prova di giorno (fuori {pre_.get('night_from') or '19:00'}–{pre_.get('night_to') or '06:00'} o troppa luce): "
                       "di giorno vale solo la prova in coppia")
    meas = []
    for i, ph in enumerate(phases):
        if ph.get("role") != "misura" or i == 0 or phases[i - 1].get("role") != "carica" or i not in phase_t or i - 1 not in phase_t:
            continue
        t_c0, t_m0 = phase_t[i - 1], phase_t[i]
        t_m1 = phase_t.get(i + 1, rows[-1]["t"])
        rc = [r for r in good if t_c0 <= r["t"] < t_m0]
        rm = [r for r in good if t_m0 <= r["t"] <= t_m1]
        name = ph.get("name", f"misura {len(meas) + 1}")
        h_on = next((r["t"] for r in rc if r["heater"] is True), None) if has_h else None
        h_off = next((r["t"] for r in rc + rm if h_on is not None and r["t"] > h_on and r["heater"] is False), None) if has_h else None
        f_on = next((r["t"] for r in rm if r["fan"] is True), None) if has_f else None
        if has_h and has_f:
            if h_on is None or h_off is None:
                reasons.append(f"{name}: stato del riscaldatore non letto acceso/spento")
            else:
                dur = h_off - h_on
                if abs(dur - float(va.get("heater_s") or 180)) > float(va.get("heater_tol_s") or 10):
                    reasons.append(f"{name}: riscaldatore acceso {dur:.0f} s invece di {va.get('heater_s')} ± {va.get('heater_tol_s')} s")
            if f_on is None:
                reasons.append(f"{name}: la ventola non risulta accesa")
            elif h_off is not None and f_on - h_off > float(va.get("pause_max_s") or 10):
                reasons.append(f"{name}: pausa riscaldatore→ventola {f_on - h_off:.0f} s oltre {va.get('pause_max_s')} s")
        t_on = f_on if f_on is not None else t_m0
        first60 = [t_on] + [r["t"] for r in rm if t_on < r["t"] <= t_on + 60] + [min(t_on + 60, t_m1)]
        gap = max((b - a for a, b in zip(first60, first60[1:])), default=0)
        if gap > float(va.get("gap_max_s") or 15):
            reasons.append(f"{name}: buco di {gap:.0f} s nei primi 60 s di ventola")
        ds = [d for d in (_deriv_at(good, r["t"], float(ix.get("win_s") or 15)) for r in rm
                          if t_on <= r["t"] <= t_on + float(ix.get("after_on_s") or 45)) if d is not None]
        I = -min(ds) if ds else None
        if I is not None:
            I += dcor                      # una base che sale frena in apparenza la discesa
        a0, a60 = _nearest(rm, t_on, "aux"), _nearest(rm, t_on + 60, "aux")
        if a60 is not None:
            a60 -= dcor
        tail = [r for r in rm if r["t"] >= t_m1 - 15 and r["temp"] is not None]
        heat_slope = _slope([(r["t"], r["aux"]) for r in rc])
        if heat_slope is not None:
            heat_slope -= dcor
        mindt = float(ix.get("k_min_dt") or 0.5)
        k = I / (a0 - T_base) if I is not None and a0 is not None and T_base is not None and a0 - T_base > mindt else None
        fit = _expfit([(r["t"] - t_on, r["aux"] - dcor * (r["t"] - t_on) / 60) for r in rm if r["t"] >= t_on]) or {}
        a120 = _nearest(rm, t_on + 120, "aux") if rm and rm[-1]["t"] >= t_on + 110 else None
        if a120 is not None:
            a120 -= 2 * dcor
        okair = T_air is not None and a0 is not None and a0 - T_air > mindt
        if fit.get("lungo"):
            notes.append(f"{name}: τ molto più lungo della misura, stima approssimata (T∞ incerto): per stimarlo meglio usa misure di 5 min")
        meas.append({"I": I, "k": k, "tau": fit.get("tau"), "Tinf": None if fit.get("lungo") else fit.get("Tinf"), "fit_A": fit.get("A"), "fit_r2": fit.get("r2"),
                     "F": (a0 - a120) / (a0 - T_air) if okair and a120 is not None else None,
                     "k_aria": I / (a0 - T_air) if okair and I is not None else None,
                     "t_on": t_on, "T_aux_on": a0, "calo60": (a0 - a60) if a0 is not None and a60 is not None else None,
                     "delta": (sum(r["temp"] - r["aux"] for r in tail) / len(tail)) if tail else None,
                     "pend_carica": heat_slope, "R": (I / heat_slope) if I is not None and heat_slope else None,
                     "h_dur": (h_off - h_on) if h_on is not None and h_off is not None else None,
                     "pausa": (f_on - h_off) if f_on is not None and h_off is not None else None})
    if len(meas) < 2 or any(m["I"] is None for m in meas[:2]):
        reasons.append("indice non calcolabile su entrambe le misure")
    luxs = [r.get("lux") for r in good if r.get("lux") is not None]
    res = {"valid": not reasons, "reasons": reasons, "notes": notes, "misure": meas,
           "led": (sum(leds) / len(leds)) if leds else None, "campioni": len(good), "T_base": T_base,
           "lux_medio": sum(luxs) / len(luxs) if luxs else None, "deriva": drift, "notte": notte}
    if _flt(va.get("lux_sun")) is not None and res["lux_medio"] is not None and res["lux_medio"] >= _flt(va["lux_sun"]):
        notes.append(f"probabile sole diretto: AUX LUX medio {res['lux_medio']:.0f}")
    for key in ("k", "tau", "F", "Tinf", "k_aria", "delta", "calo60"):
        v = [m.get(key) for m in meas[:2]]
        res[key + "1"], res[key + "2"] = (v + [None, None])[:2]
        res[key + "_medio"] = sum(v) / 2 if len(v) == 2 and None not in v else None
    r2 = [m.get("fit_r2") for m in meas[:2] if m.get("fit_r2") is not None]
    res["fit_r2"] = min(r2) if r2 else None
    res["T_aria"] = T_air
    res["dT_riposo"] = T_base - T_air if T_base is not None and T_air is not None else None
    res["Tinf_aria"] = res["Tinf_medio"] - T_air if res.get("Tinf_medio") is not None and T_air is not None else None
    if T_air is None:
        notes.append("temperatura dell'aria non indicata: F, k rispetto all'aria e AUX − T aria non calcolati")
    if res["fit_r2"] is not None and res["fit_r2"] < 0.95:
        notes.append(f"curva di raffreddamento poco esponenziale (R² {res['fit_r2']:.2f}): τ meno affidabile")
    if len(meas) >= 2 and meas[0]["I"] is not None and meas[1]["I"] is not None:
        I1, I2 = meas[0]["I"], meas[1]["I"]
        Im = (I1 + I2) / 2
        res.update(I1=I1, I2=I2, I_medio=Im, C=(abs(I1 - I2) / Im) if Im else None)
    # secondo indicatore S: pendenza di AUX durante la carica (°C/min)
    ss = [m.get("pend_carica") for m in meas[:2]]
    res.update(S1=ss[0] if ss else None, S2=ss[1] if len(ss) > 1 else None)
    sv = [x for x in ss if x is not None]
    res["S_medio"] = sum(sv) / len(sv) if len(sv) == 2 else None
    return res


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    n = len(vals)
    if not n:
        return None
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _ref_of(refs, cuid, ref_min):
    """Riferimento di una colonnina: I₀ e S₀ (mediane delle tarature valide), usabili con almeno ref_min prove."""
    r = refs.get(cuid or "", {})
    tests = r.get("tests", [])
    iv = [t.get("I") for t in tests if t.get("I") is not None]
    sv = [t.get("S") for t in tests if t.get("S") is not None]
    return {"cuid": cuid, "n": len(iv), "nS": len(sv), "ref_min": ref_min,
            "I0_all": _median(iv), "S0_all": _median(sv),
            "I0": _median(iv) if len(iv) >= ref_min else None, "S0": _median(sv) if len(sv) >= ref_min else None}


SCORE = {"attaccato": -1, "incerto": 0, "sospetto distacco": 1}


def _abs_model(cfg, cuid):
    for m in ((cfg.get("absolute") or {}).get("models") or []):
        if not m.get("match") or str(m["match"]).lower() in str(cuid or "").lower():
            return m
    return None


def _abs_eval(res, cuid, cfg):
    """Metodo assoluto: regole sugli indicatori della prova, senza riferimento. Sospetto se almeno sus_min condizioni
    di sospetto sono vere (1 = basta una, OR); attaccato se almeno att_min condizioni di attaccato sono vere (0 = tutte)."""
    m = _abs_model(cfg, cuid)
    if not m:
        return "", "nessun modello impostato per questo CUID", ""
    def check(rules):
        out = []
        for r in rules or []:
            v, thr = res.get(r.get("ind")), _flt(r.get("thr"))
            if v is None or thr is None:
                out.append((r, None, v))
            else:
                out.append((r, v >= thr if r.get("op") == ">=" else v <= thr, v))
        return out
    sus, att = check(m.get("sus")), check(m.get("att"))
    lab = lambda k: ABS_IND.get(k, k).split(" (")[0]
    fm = lambda v: "–" if v is None else (f"{v:.0f}" if abs(v) >= 100 else f"{v:.2f}".rstrip("0").rstrip(".")).replace(".", ",")
    txt = "; ".join(f"{lab(r['ind'])} {fm(v)} {r['op']} {fm(_flt(r['thr']))} {'✓' if ok else '✗' if ok is False else '?'}"
                    for r, ok, v in sus + att)
    n_s = sum(1 for _, ok, _ in sus if ok)
    need_s = max(1, int(m.get("sus_min") or 1))
    att_ev = [ok for _, ok, _ in att]
    need_a = int(m.get("att_min") or 0) or len(att)
    if n_s >= need_s:
        return "sospetto distacco", txt, m.get("name", "")
    if att and sum(1 for ok in att_ev if ok) >= need_a and (int(m.get("att_min") or 0) or None not in att_ev):
        return "attaccato", txt, m.get("name", "")
    if all(ok is None for _, ok, _ in sus + att):
        return "", "indicatori assoluti non calcolabili (manca la temperatura dell'aria o la curva)", m.get("name", "")
    return "incerto", txt, m.get("name", "")


def _combine_abs(rel, ea, mode):
    """ABS: solo il metodo assoluto (se non valutabile, il relativo). ABS_OR: sospetto se uno dei due è sospetto;
    attaccato se tutti quelli disponibili dicono attaccato; altrimenti incerto."""
    if mode == "ABS":
        return ea or rel
    if mode == "ABS_OR":
        av = [e for e in (rel, ea) if e]
        if "sospetto distacco" in av:
            return "sospetto distacco"
        return "attaccato" if av and all(e == "attaccato" for e in av) else "incerto"
    return rel


def _esito_P(P, ix):
    if P is None:
        return ""
    if float(ix.get("p_att_low") or 0.7) <= P <= float(ix.get("p_att_high") or 1.4):
        return "attaccato"
    if P <= float(ix.get("p_sus_low") or 0.4) or P >= float(ix.get("p_sus_high") or 2.0):
        return "sospetto distacco"
    return "incerto"


def _classify(res, cuid, cfg, info, refs=None):
    """Esito (§6). Taratura: nessun esito di rilevamento, solo coerenza con le tarature precedenti.
    Rilevamento: indice I (B con I₀ se esiste, altrimenti soglie A), indicatore S rispetto a S₀, combinazione."""
    ix = cfg["index"]
    ref_min = int(ix.get("ref_min") or 3)
    refs = load(F_PROF_REF, {}) if refs is None else refs
    Im, C, Sm = res.get("I_medio"), res.get("C"), res.get("S_medio")
    t_est, dtr = _flt(info.get("ambient")), _flt(ix.get("dt_ref"))
    if res.get("valid") and t_est is not None and dtr and res["misure"] and res["misure"][0].get("T_aux_on") is not None:
        res["I_n"] = Im * dtr / max(res["misure"][0]["T_aux_on"] - t_est, 3.0)
    m = _abs_model(cfg, cuid)
    res["modello"] = m.get("name", "") if m else ""
    if res.get("valid") and res.get("notte") is False:
        res["esito_abs"], res["regole_abs"] = "", "prova di giorno: il metodo assoluto vale solo al buio"
    elif res.get("valid"):
        res["esito_abs"], res["regole_abs"], _ = _abs_eval(res, cuid, cfg)
    if info.get("tipo") == "taratura":
        prev = _ref_of(refs, cuid, ref_min)
        res.update(tipo="taratura", metodo="", rif_cuid="", I0_prev=prev["I0_all"], S0_prev=prev["S0_all"], n_prev=prev["n"])
        if not res["valid"]:
            res["esito"] = "non valida"
            return res
        res["esito"] = "taratura"
        res["rI"] = Im / prev["I0_all"] if prev["I0_all"] else None
        res["rS"] = Sm / prev["S0_all"] if prev["S0_all"] and Sm is not None else None
        ql = float(ix.get("q_low") or 1.3)
        bad = [f"{k} = {v:.2f}" for k, v in (("Ī/I₀ prec.", res["rI"]), ("S̄/S₀ prec.", res["rS"])) if v is not None and not (1 / ql <= v <= ql)]
        res["coerente"] = None if res["rI"] is None else not bad
        if bad:
            res["notes"].append("taratura poco coerente con le precedenti (" + ", ".join(bad) + "): verificare che lo schermo sia attaccato")
        return res
    rc = (info.get("ref_cuid") or "").strip()
    rc = rc if rc and rc != cuid else ""
    ref = _ref_of(refs, rc or cuid, ref_min)
    res.update(tipo="rilevamento", rif_cuid=rc, I0=ref["I0"], S0=ref["S0"], n_rif=ref["n"])
    if rc and ref["I0"] is None:
        res["notes"].append(f"la colonnina {rc} non ha ancora un riferimento completo ({ref['n']}/{ref_min}): soglie fisse")
    if not res["valid"]:
        res.update(esito="non valida", metodo="")
        return res
    I0, S0 = ref["I0"], ref["S0"]
    if I0:
        Q = Im / I0
        res.update(Q=Q, metodo="B")
        eI = "attaccato" if Q <= float(ix["q_low"]) else "sospetto distacco" if Q >= float(ix["q_high"]) else "incerto"
    else:
        res.update(Q=None, metodo="A")
        eI = "attaccato" if Im <= float(ix["a_low"]) else "sospetto distacco" if Im >= float(ix["a_high"]) else "incerto"
    if C is not None and C > float(ix.get("c_max") or 0.3):
        eI = "incerto"
        res["notes"].append(f"misure poco coerenti: C = {C:.2f} > {ix.get('c_max')}")
    res["esito_I"] = eI
    P = (Sm / S0) if S0 and Sm is not None else None
    res["P"], res["esito_S"] = P, _esito_P(P, ix)
    mode = ix.get("combine") or "I"
    rmode = mode if mode in ("I", "S", "IS") else ("IS" if res["esito_S"] else "I")
    if rmode != "I" and not res["esito_S"]:
        if mode in ("S", "IS"):
            res["notes"].append("S non usato: manca S₀ (servono tarature con S) o S̄")
        rmode = "I"
    if rmode == "S":
        rel = res["esito_S"]
    elif rmode == "IS":
        sc = SCORE[eI] + SCORE[res["esito_S"]]
        rel = "sospetto distacco" if sc >= 1 else "attaccato" if sc <= -1 else "incerto"
    else:
        rel = eI
    res["esito_rel"] = rel
    res["combinazione"] = mode if mode in ("ABS", "ABS_OR") else rmode
    if mode in ("ABS", "ABS_OR") and not res.get("esito_abs"):
        res["notes"].append("metodo assoluto non valutabile: esito dal metodo relativo")
    res["esito"] = _combine_abs(rel, res.get("esito_abs", ""), mode)
    return res


def _ref_add(cuid, ev, info, file="", refs=None):
    keep = refs is not None
    refs = refs if keep else load(F_PROF_REF, {})
    r = refs.setdefault(cuid, {"tests": []})
    r["tests"].append({"I": round(ev["I_medio"], 4), "S": round(ev["S_medio"], 4) if ev.get("S_medio") is not None else None,
                       "data": datetime.now().isoformat(timespec="seconds"), "led": ev.get("led"),
                       "temp_esterna": info.get("ambient", ""), "file": file})
    r["I0"] = round(_median(t.get("I") for t in r["tests"]), 4)
    s0 = _median(t.get("S") for t in r["tests"])
    r["S0"] = round(s0, 4) if s0 is not None else None
    r["led"], r["temp_esterna"] = ev.get("led"), info.get("ambient", "")
    if not keep:
        save(F_PROF_REF, refs)
    return r


def profile_status():
    """Decisione nel tempo per CUID (§7)."""
    rows = profile_history()["rows"][::-1]          # cronologico
    refs, ref_min = load(F_PROF_REF, {}), int(profile_cfg()["index"].get("ref_min") or 3)
    out = {}
    for r in rows:
        out.setdefault(r["cuid"], []).append(r)
    res = []
    for cuid, rs in out.items():
        mon = [r for r in rs if r.get("riferimento") != "si" and r.get("tipo") != "taratura"]
        valid = [r for r in mon if r.get("valida") == "si"]
        last5 = mon[-5:]
        # decisione dal motore unico: «staccato» comprende già il sospetto confermato in più notti
        if mon and mon[-1]["esito"] == "staccato (diagnostica)":
            dec = "ALLARME (diagnostica)"
        elif valid and valid[-1]["esito"] == "staccato":
            dec = "ALLARME"
        elif len(last5) == 5 and all(r["esito"] in ("incerto", "non valida", "interrotta", "non partita") for r in last5):
            dec = "misura impossibile"
        elif valid and valid[-1]["esito"] == "sospetto":
            dec = "sospetto: ripetere la notte successiva"
        elif valid:
            dec = valid[-1]["esito"]
        else:
            dec = "nessuna prova valida"
        ref = _ref_of(refs, cuid, ref_min)
        res.append({"cuid": cuid, "decisione": dec, "I0": ref["I0"], "S0": ref["S0"], "rif_prove": ref["n"], "rif_prove_S": ref["nS"],
                    "I0_parz": ref["I0_all"], "S0_parz": ref["S0_all"], "ref_min": ref_min,
                    "ultime": [{"esito": r["esito"], "data": r["data_ora"], "I": r["I_medio"], "Q": r.get("Q", ""), "P": r.get("P", ""),
                                "rif_cuid": r.get("rif_cuid", "")} for r in mon[-6:]]})
    for cuid in refs:                                  # colonnine con sole tarature non ancora nel riepilogo
        if cuid not in out:
            ref = _ref_of(refs, cuid, ref_min)
            res.append({"cuid": cuid, "decisione": "nessuna prova valida", "I0": ref["I0"], "S0": ref["S0"], "rif_prove": ref["n"],
                        "rif_prove_S": ref["nS"], "I0_parz": ref["I0_all"], "S0_parz": ref["S0_all"], "ref_min": ref_min, "ultime": []})
    return res


def _next_night(nbs):
    h, m = map(int, nbs.split(":"))
    tgt = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    return tgt.timestamp() if tgt.timestamp() > time.time() else tgt.timestamp() + 86400


def _prof_one(rid, rep):
    """Una prova: attesa condizioni, sequenza, chiusura, validità, indice, classificazione."""
    run, ctrl = PROFRUNS[rid], PROFCTRL[rid]
    st, cfg, info = ctrl["station"], ctrl["cfg"], run["info"]
    phases, dt = cfg["phases"], max(1.0, float(cfg.get("sample_s") or 5))
    run.update(rep=rep, state="attesa", phase=None, conditions=[], result=None, eval=None, file=None, reason="",
               samples=[], led_ref=None, cuid0=None, log=[])
    nbs = (cfg["pre"].get("not_before") or "").strip()
    run["_not_before"] = _next_night(nbs) if nbs else None
    hist, win, t_wait = deque(), float(cfg["pre"].get("stable_min") or 30) * 60, time.time()
    rows, phase_t, fh, wr = [], {}, None, None
    try:
        if run["_not_before"]:
            run["state"] = "attesa orario"
            while time.time() < run["_not_before"]:
                if ctrl["stop"].is_set():
                    raise ProfAbort("fermata dall'operatore prima dell'inizio")
                ctrl["stop"].wait(min(30, run["_not_before"] - time.time()))
            run["state"], t_wait = "attesa", time.time()
        while True:                                        # condizioni per partire (§4)
            if ctrl["stop"].is_set():
                raise ProfAbort("fermata dall'operatore prima dell'inizio")
            s = _prof_sample(st, cfg)
            hist.append(s)
            while hist and hist[0]["t"] < s["t"] - win - dt:
                hist.popleft()
            run["conditions"] = _prof_conditions(st, cfg, list(hist), run, ctrl)
            run["last"] = _prof_last(s)
            if all(c["ok"] for c in run["conditions"]):
                break
            if time.time() - t_wait > float(cfg["pre"].get("max_wait_h") or 3) * 3600:
                raise ProfAbort("condizioni di partenza non raggiunte: " + "; ".join(c["name"] for c in run["conditions"] if not c["ok"]))
            ctrl["stop"].wait(dt)
        _pair_wait(run, ctrl, rep, cfg)
        recent = [h["num"].get("led") for h in hist if h["t"] >= hist[-1]["t"] - 300 and h["num"].get("led") is not None]
        run["led_ref"] = sum(recent) / len(recent) if recent else None
        if (cfg.get("meteo") or {}).get("on"):
            try:
                info["meteo"] = _meteo(cfg)
                if info["meteo"].get("temp") is not None and (not str(info.get("ambient") or "").strip() or info.get("ambient_src") == "meteo"):
                    info["ambient"], info["ambient_src"] = str(info["meteo"]["temp"]), "meteo"
            except Exception as e:
                info["meteo"] = {}
                run["log"].append(f"{datetime.now():%H:%M:%S} meteo: {e}")
        run["cuid0"] = hist[-1]["raw"].get("cuid") or POOL.ident.get(st["id"], {}).get("mac", "") or st["name"]
        stamp = datetime.now()
        run["file"] = f"{_safe_name(run['cuid0'])}_{_safe_name(info.get('config'))}_{rep}_{stamp:%Y%m%d_%H%M}.csv"
        fh = (PROF_DIR / run["file"]).open("w", newline="", encoding="utf-8-sig")
        wr = csv.writer(fh, delimiter=";")
        wr.writerow(CSV_COLS)
        save(PROF_DIR / (run["file"][:-4] + ".json"), {"spec": "v3", "cuid": run["cuid0"], "colonnina": st["name"], "host": st["host"],
                                                       "mac": POOL.ident.get(st["id"], {}).get("mac", ""), "info": info, "ripetizione": rep,
                                                       "inizio": stamp.astimezone().isoformat(timespec="seconds"),
                                                       "led_rif": run["led_ref"], "parametri": cfg})
        run["state"] = "fase"
        _pending_set(st, True)
        wdc = cfg.get("watchdog") or {}
        if wdc.get("on", True):
            secs = sum(float(ph.get("s") or 60) for ph in phases) + float(wdc.get("margin_min") or 15) * 60
            try:
                run["_wd"] = _wd_start(st, secs)
                _pending_set(st, True, run["_wd"])
                run["log"].append(f"{datetime.now():%H:%M:%S} rete di sicurezza sulla colonnina: ripristino automatico tra {secs / 60:.0f} min (PID {run['_wd'] or '?'})")
            except Exception as e:
                run["log"].append(f"{datetime.now():%H:%M:%S} rete di sicurezza non avviata: {describe_error(e)}")
        _prof_cmd(run, ctrl, st, "05;00;TempContEn;0", "Disabilita controllo termico")
        tz = datetime.now().astimezone().tzinfo
        t_next = time.time()
        for i, ph in enumerate(phases):
            dur = float(ph.get("s") or 60)
            t_ph = time.time()
            run.update(phase=i, phase_start=t_ph, phase_end=t_ph + dur)
            # un solo comando: riscaldatore spento e ventola accesa nello stesso istante (§3)
            _prof_cmd(run, ctrl, st, _aux_line(ph.get("heater"), ph.get("fan")), f"Passo {i + 1} {ph.get('name')}")
            phase_t[i] = t_ph
            k = 0
            while time.time() < run["phase_end"]:
                if ctrl["stop"].is_set():
                    raise ProfAbort("fermata dall'operatore")
                s = _prof_sample(st, cfg)
                reason = _prof_alarm(s, cfg, run)
                n, raw = s["num"], s["raw"]
                rows.append({"t": s["t"], "i": i, "ok": s["ok"], "aux": n.get("aux"), "temp": n.get("temp"), "led": n.get("led"), "lux": n.get("lux"),
                             "user": raw.get("user"), "auxfault": n.get("auxfault"), "auxfault_raw": raw.get("auxfault"),
                             "tamper": raw.get("tamper"), "heater": _onoff(raw.get("heater_state")), "fan": _onoff(raw.get("fan_state"))})
                wr.writerow([datetime.fromtimestamp(s["t"], tz).isoformat(timespec="seconds"), raw.get("cuid") or run["cuid0"],
                             i + 1, ph.get("name"), int(bool(ph.get("heater"))), int(bool(ph.get("fan"))),
                             raw.get("heater_state", ""), raw.get("fan_state", ""),
                             *[(str(raw.get(r)) if raw.get(r) is not None else "").replace(".", ",") if n.get(r) is not None else (raw.get(r) or "")
                               for r in ("aux", "temp", "cpu", "led", "lux", "vmain", "v12", "user", "auxfault", "tamper")],
                             s["ok"], s["ms"] if s["ms"] is not None else ""])
                fh.flush()
                run["samples"].append([s["t"], n.get("aux"), n.get("temp"), i])
                del run["samples"][:-1500]
                run["last"] = _prof_last(s)
                if reason:
                    raise ProfAbort(reason)
                k += 1
                ctrl["stop"].wait(max(0.0, min(t_ph + k * dt, run["phase_end"]) - time.time()))
        run["result"] = "completata"
    except ProfAbort as e:
        run.update(result="interrotta", reason=str(e))
    except Exception as e:
        run.update(result="interrotta", reason=describe_error(e))
    finally:
        run["state"] = "chiusura"
        stop_was = ctrl["stop"].is_set()
        if run.get("pair") and not run.get("cuid0") and run["pair"]["id"] in PAIRS:
            P = PAIRS[run["pair"]["id"]]
            with P["cond"]:
                P["finished"][run["id"]] = rep
                P["cond"].notify_all()
        ctrl["stop"] = threading.Event()
        if run.get("cuid0"):
            if _prof_close(run, ctrl, st):
                _pending_set(st, False)
        if fh:
            fh.close()
        if run.get("cuid0"):
            if run["result"] == "completata":
                run["t_air"] = _t_air(info)
                run["in_coppia"] = bool(run.get("pair")) and not run.get("_alone")
                ev = _classify(_prof_evaluate(rows, phase_t, run, cfg), run["cuid0"], cfg, info)
                if run.get("pair") and not run.get("_alone"):
                    ev["notes"].append(f"prova in coppia ({run['pair']['role']}) con {run['pair']['partner_name']}")
            else:
                ev = {"valid": False, "reasons": [run["reason"]], "notes": [], "esito": "interrotta", "misure": []}
            run["eval"] = ev
            ev.setdefault("tipo", info.get("tipo", "rilevamento"))
            if info.get("tipo") == "taratura" and ev.get("valid"):
                with LOCK:
                    r = _ref_add(run["cuid0"], ev, info, run["file"])
                ref = _ref_of({run["cuid0"]: r}, run["cuid0"], int(cfg["index"].get("ref_min") or 3))
                ev.update(I0_new=ref["I0_all"], S0_new=ref["S0_all"], n_new=ref["n"], nS_new=ref["nS"], ref_min=ref["ref_min"])
                ev["notes"].append(f"aggiunta alle tarature della colonnina ({ref['n']}/{ref['ref_min']})")
            meta = PROF_DIR / (run["file"][:-4] + ".json")
            save(meta, {**load(meta, {}), "info": info, "coppia_id": (run.get("pair") or {}).get("id", ""),
                        "esito": ev.get("esito"), "valida": ev.get("valid"), "motivi": ev.get("reasons"),
                        "note_calcolo": ev.get("notes"), "fine": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "risultati": {k: v for k, v in ev.items() if k not in ("reasons", "notes")}})
            tar = info.get("tipo") == "taratura"
            row = {"file": run["file"], "cuid": run["cuid0"], "colonnina": st["name"],
                   "data_ora": datetime.fromtimestamp(phase_t.get(0, time.time())).strftime("%d/%m/%Y %H:%M"),
                   "tipo": "taratura" if tar else "rilevamento", "configurazione": info.get("config", ""), "ripetizione": rep,
                   "riferimento": "si" if tar else "no", "luogo": info.get("place", ""), "temp_esterna": info.get("ambient", ""),
                   "livello_led": info.get("led_level", "") or _f4(ev.get("led")), "campioni": ev.get("campioni", ""),
                   **_info_fields(info), **_ev_fields(ev, tar)}
            with LOCK:
                _summary_migrate()
                new = not F_PROF_SUMMARY.exists()
                with F_PROF_SUMMARY.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as fsum:
                    w = csv.writer(fsum, delimiter=";")
                    if new:
                        w.writerow(SUMMARY_COLS)
                    w.writerow([row.get(c, "") for c in SUMMARY_COLS])
            try:
                _pair_done(run, rep, ev, cfg)
            except Exception as e:
                run["log"].append(f"{datetime.now():%H:%M:%S} coppia: {describe_error(e)}")
        if run.get("cuid0") and run.get("file"):
            with LOCK:
                lp = load(F_PROF_LAST, {})
                lp[st["id"]] = time.time()
                save(F_PROF_LAST, lp)
        audit(st, "profilo termico v3", f"{info.get('config')} rip. {rep}", run["result"] == "completata",
              (run.get("eval") or {}).get("esito") or run["reason"] or run["result"])
        run["state"] = "fatto"
        if stop_was:
            ctrl["stop"].set()


def _tempconten(st):
    """Legge TempContEn sulla scheda (04;00;TempContEn): "1" attivo, "0" disattivo, None nessuna risposta."""
    try:
        return aux_send({"station_id": st["id"], "line": "04;00;TempContEn", "label": "Verifica controllo termico", "wait": 6}).get("value")
    except Exception:
        return None


def _wd_start(st, secs):
    """Rete di sicurezza sulla colonnina: dopo secs spegne riscaldatore e ventola e riattiva il controllo termico,
    anche se il PC si spegne o perde la connessione. Restituisce il PID, da fermare a fine prova."""
    q = shlex.quote(aux_cfg()["command_path"])
    job = f"sleep {int(secs)}; printf '07;00;00;01;00;01\\n' > {q}; sleep 5; printf '05;00;TempContEn;1\\n' > {q}"
    _, out, _ = ssh_exec(st, f"nohup sh -c {shlex.quote(job)} >/dev/null 2>&1 </dev/null & echo $!", timeout=15)
    pid = (out.strip().split() or [""])[-1]
    return pid if pid.isdigit() else ""


def _wd_stop(st, pid):
    if pid and str(pid).isdigit():
        try:
            ssh_exec(st, f"kill {int(pid)} 2>/dev/null; true", timeout=10)
        except Exception:
            pass


def _prof_close(run, ctrl, st):
    """Chiusura: uscite spente, controllo termico riattivato e verificato rileggendo TempContEn; tre tentativi."""
    def warn(msg):
        run["reason"] = (run["reason"] + " | " if run["reason"] else "") + "ATTENZIONE: " + msg
    ok_off = ok_on = False
    for k in range(3):
        try:
            if not ok_off:
                ok_off = _prof_cmd(run, ctrl, st, "07;00;00;01;00;01", "Riscaldatore e ventola spenti") == "ok"
            if not ok_on:
                ok_on = _prof_cmd(run, ctrl, st, "05;00;TempContEn;1", "Controllo termico riattivato") == "ok"
        except Exception as e:
            run["log"].append(f"{datetime.now():%H:%M:%S} chiusura, tentativo {k + 1}: {describe_error(e)}")
        if ok_off and ok_on:
            break
        ctrl["stop"].wait(5)
    val = None
    for k in range(3):
        val = _tempconten(st)
        run["log"].append(f"{datetime.now():%H:%M:%S} Verifica TempContEn: {val if val is not None else 'nessuna risposta'}")
        if val == "1":
            break
        if val == "0":
            try:
                _prof_cmd(run, ctrl, st, "05;00;TempContEn;1", "Controllo termico riattivato (nuovo tentativo)")
            except Exception as e:
                run["log"].append(f"{datetime.now():%H:%M:%S} riattivazione: {describe_error(e)}")
        ctrl["stop"].wait(5)
    run["tempconten"] = val
    if val == "1":
        ok_on = True
    elif val == "0":
        ok_on = False
    elif ok_on:
        warn("controllo termico riattivato ma non verificato (nessuna risposta a 04;00;TempContEn)")
    if not ok_off:
        warn("riscaldatore e ventola spenti senza conferma")
    if not ok_on:
        warn("controllo termico NON riattivato: usa Ripristina ora")
    if ok_off and ok_on:
        _wd_stop(st, run.get("_wd"))
        return True
    return False


def _meteo(cfg, lat=None, lon=None):
    m = cfg.get("meteo") or {}
    lat = _flt(lat if lat not in (None, "") else m.get("lat"))
    lon = _flt(lon if lon not in (None, "") else m.get("lon"))
    if lat is None or lon is None:
        raise ValueError("imposta le coordinate del sito (Impostazioni del profilo → Meteo)")
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,wind_speed_10m,cloud_cover,shortwave_radiation&wind_speed_unit=kmh&timezone=auto")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            c = json.load(r).get("current") or {}
    except Exception as e:
        raise RuntimeError(f"meteo non disponibile (serve internet sul PC): {e}")
    return {"temp": c.get("temperature_2m"), "vento": c.get("wind_speed_10m"), "nuvole": c.get("cloud_cover"),
            "irr": c.get("shortwave_radiation"), "ora": c.get("time"), "lat": lat, "lon": lon}


PAIRS = {}


def _pair_wait(run, ctrl, rep, cfg):
    """Prova in coppia: le due colonnine partono insieme, quando entrambe hanno le condizioni di partenza."""
    p = run.get("pair")
    if not p:
        return
    P = PAIRS[p["id"]]
    run["_alone"] = False
    with P["cond"]:
        P["ready"][run["id"]] = rep
        P["cond"].notify_all()
    t0, lim = time.time(), float((cfg.get("pair") or {}).get("wait_min") or 20) * 60
    run["state"] = "attesa coppia"
    while True:
        if ctrl["stop"].is_set():
            raise ProfAbort("fermata dall'operatore prima dell'inizio")
        with P["cond"]:
            if P["ready"].get(p["partner"]) == rep:
                break
            gone = P["finished"].get(p["partner"]) == rep or PROFRUNS.get(p["partner"], {}).get("done", True)
            if gone or time.time() - t0 > lim:
                run["_alone"] = True
                run["log"].append(f"{datetime.now():%H:%M:%S} coppia: {p['partner_name']} non pronta, prova da sola")
                break
            P["cond"].wait(2)
    run["state"] = "attesa"


def _pair_compare(evp, evs, ix):
    """Confronto con la sentinella misurata nello stesso momento: Q = Ī/Ī_sent, P = S̄/S̄_sent."""
    c = {"Q": None, "P": None, "esito": "", "esito_I": "", "esito_S": "", "nota": ""}
    if not (evp.get("valid") and evs.get("valid")):
        c["nota"] = "coppia non confrontabile: " + ("la prova" if not evp.get("valid") else "la sentinella") + " non è valida"
        return c
    if not evs.get("I_medio") or evp.get("I_medio") is None:
        return c
    Q = evp["I_medio"] / evs["I_medio"]
    eI = "attaccato" if Q <= float(ix["q_low"]) else "sospetto distacco" if Q >= float(ix["q_high"]) else "incerto"
    if evp.get("C") is not None and evp["C"] > float(ix.get("c_max") or 0.3):
        eI = "incerto"
    Pv = evp["S_medio"] / evs["S_medio"] if evp.get("S_medio") is not None and evs.get("S_medio") else None
    eS = _esito_P(Pv, ix)
    full = ix.get("combine") or "I"
    mode = full if full in ("I", "S", "IS") else ("IS" if eS else "I")
    if mode != "I" and not eS:
        mode = "I"
    if mode == "S":
        esito = eS
    elif mode == "IS":
        sc = SCORE[eI] + SCORE[eS]
        esito = "sospetto distacco" if sc >= 1 else "attaccato" if sc <= -1 else "incerto"
    else:
        esito = eI
    esito = _combine_abs(esito, evp.get("esito_abs", ""), full)
    mode = full if full in ("ABS", "ABS_OR") else mode
    c.update(Q=Q, P=Pv, esito_I=eI, esito_S=eS, esito=esito, combinazione=mode,
             Qk=(evp["k_medio"] / evs["k_medio"]) if evp.get("k_medio") and evs.get("k_medio") else None,
             Qtau=(evs["tau_medio"] / evp["tau_medio"]) if evp.get("tau_medio") and evs.get("tau_medio") else None)
    return c


def _pair_rows(rows, pid, fp, fs, comp):
    """Scrive nel riepilogo il risultato della coppia (fp = prova, fs = sentinella)."""
    for r in rows:
        if r.get("file") == fp:
            r.update(coppia=pid, ruolo_coppia="prova", file_coppia=fs, Q_coppia=_f4(comp.get("Q")), P_coppia=_f4(comp.get("P")),
                     esito_coppia=comp.get("esito", ""))
            if comp.get("esito") and r.get("valida") == "si":
                r["esito"] = comp["esito"]
        elif r.get("file") == fs:
            r.update(coppia=pid, ruolo_coppia="sentinella", file_coppia=fp)


def _pair_done(run, rep, ev, cfg):
    p = run.get("pair")
    if not p:
        return
    P = PAIRS.get(p["id"])
    with P["cond"]:
        P["finished"][run["id"]] = rep
        P["cond"].notify_all()
        if run.get("_alone") or not run.get("file"):
            return
        slot = P["res"].setdefault(rep, {})
        slot[p["role"]] = {"ev": ev, "file": run["file"], "rid": run["id"]}
        if len(slot) < 2:
            return
    t, s = slot["prova"], slot["sentinella"]
    comp = _pair_compare(t["ev"], s["ev"], cfg["index"])
    t["ev"]["coppia"] = {**comp, "ruolo": "prova", "sentinella": PROFRUNS[s["rid"]]["station"], "file": s["file"]}
    if comp.get("esito") and t["ev"].get("valid"):
        t["ev"]["esito_proprio"], t["ev"]["esito"] = t["ev"].get("esito"), comp["esito"]
    s["ev"]["coppia"] = {"ruolo": "sentinella", "prova": PROFRUNS[t["rid"]]["station"], "file": t["file"]}
    with LOCK:
        rows = _summary_read()
        _pair_rows(rows, p["id"], t["file"], s["file"], comp)
        _summary_write(rows)
        for x in (t, s):
            mp = PROF_DIR / (x["file"][:-4] + ".json")
            save(mp, {**load(mp, {}), "esito": x["ev"].get("esito"), "coppia": x["ev"]["coppia"]})
        for r in PROFRUNS.values():
            for h in r.get("history", []):
                if h.get("file") == t["file"]:
                    h.update(esito=t["ev"].get("esito"), Q_coppia=comp.get("Q"), P_coppia=comp.get("P"))


def _f4(v):
    return "" if v is None else str(round(v, 4)).replace(".", ",")


def _ev_fields(ev, tar):
    """Colonne del riepilogo che dipendono dal calcolo (prova appena fatta o forzata valida)."""
    ms = ev.get("misure") or []
    m1 = ms[0] if ms else {}
    def avg(k):
        v = [x[k] for x in ms if x.get(k) is not None]
        return sum(v) / len(v) if v else None
    f = _f4
    return {"rif_cuid": ev.get("rif_cuid", ""), "valida": "si" if ev.get("valid") else "no", "motivo": " | ".join(ev.get("reasons") or []),
            "I1": f(ev.get("I1")), "I2": f(ev.get("I2")), "I_medio": f(ev.get("I_medio")), "C": f(ev.get("C")),
            # taratura: I0/S0 = riferimento precedente, Q/P = coerenza con le tarature precedenti
            "I0": f(ev.get("I0_prev") if tar else ev.get("I0")), "Q": f(ev.get("rI") if tar else ev.get("Q")),
            "esito_I": ev.get("esito_I", ""), "S1": f(ev.get("S1")), "S2": f(ev.get("S2")), "S_medio": f(ev.get("S_medio")),
            "S0": f(ev.get("S0_prev") if tar else ev.get("S0")), "P": f(ev.get("rS") if tar else ev.get("P")),
            "esito_S": ev.get("esito_S", ""), "combinazione": COMBINE_LBL.get(ev.get("combinazione"), ""),
            "n_rif": ev.get("n_new", "") if tar else ev.get("n_rif", ""), "metodo": ev.get("metodo", ""), "esito": ev.get("esito", ""),
            "delta": f(avg("delta")), "R": f(avg("R")), "calo_aux_60s": f(avg("calo60")), "pendenza_carica": f(avg("pend_carica")),
            "T_aux_on": f(m1.get("T_aux_on")), "I_n": f(ev.get("I_n")),
            "T_base": f(ev.get("T_base")), "k1": f(ev.get("k1")), "k2": f(ev.get("k2")), "k_medio": f(ev.get("k_medio")),
            "tau1": f(ev.get("tau1")), "tau2": f(ev.get("tau2")), "tau_medio": f(ev.get("tau_medio")), "lux_medio": f(ev.get("lux_medio")),
            "modello": ev.get("modello", ""), "T_aria": f(ev.get("T_aria")), "F1": f(ev.get("F1")), "F2": f(ev.get("F2")),
            "F_medio": f(ev.get("F_medio")), "Tinf1": f(ev.get("Tinf1")), "Tinf2": f(ev.get("Tinf2")), "Tinf_medio": f(ev.get("Tinf_medio")),
            "fit_r2": f(ev.get("fit_r2")), "k_aria_medio": f(ev.get("k_aria_medio")), "dT_riposo": f(ev.get("dT_riposo")),
            "esito_abs": ev.get("esito_abs", ""), "regole_abs": ev.get("regole_abs", ""),
            "deriva": f(ev.get("deriva")), "notte": "" if ev.get("notte") is None else ("si" if ev.get("notte") else "no")}


def _info_fields(info):
    m = info.get("meteo") or {}
    g = lambda k: "" if m.get(k) is None else str(m[k]).replace(".", ",")
    return {"configurazione": info.get("config", ""), "luogo": info.get("place", ""), "temp_esterna": info.get("ambient", ""),
            "note": info.get("notes", ""), "campagna": info.get("campagna", ""), "origine": info.get("origine", ""),
            "meteo_temp": g("temp"), "meteo_vento": g("vento"), "meteo_nuvole": g("nuvole"), "meteo_irr": g("irr")}


def _prof_cmd(run, ctrl, st, line, label):
    ev = _retry_conn(run, ctrl, lambda: aux_send({"station_id": st["id"], "line": line, "label": label, "wait": 4}))
    run["log"].append(f"{datetime.now():%H:%M:%S} {label}: {ev['status']}")
    if ev["status"] == "ko":
        raise ProfAbort(f"{label}: la scheda ha risposto con errore")
    return ev["status"]


def _prof_thread(rid):
    run, ctrl = PROFRUNS[rid], PROFCTRL[rid]
    for k in range(int(run["repeats"])):
        _prof_one(rid, run["rep_first"] + k)
        ev = run.get("eval") or {}
        run["history"].append({"rep": run["rep"], "result": run["result"], "reason": run["reason"], "file": run["file"],
                               "esito": ev.get("esito"), "valid": ev.get("valid"), "I_medio": ev.get("I_medio"), "Q": ev.get("Q"),
                               "S_medio": ev.get("S_medio"), "P": ev.get("P"), "tipo": ev.get("tipo"),
                               "rI": ev.get("rI"), "rS": ev.get("rS"), "n_new": ev.get("n_new"),
                               "Q_coppia": (ev.get("coppia") or {}).get("Q"), "P_coppia": (ev.get("coppia") or {}).get("P")})
        if ctrl["stop"].is_set() or "colonnina cambiata" in run["reason"]:
            break
    run.update(done=True, state="fatto")


def profile_start(body):
    cfg = profile_cfg()
    if _flt(cfg["safety"].get("aux_max")) is None or _flt(cfg["safety"].get("temp_max")) is None:
        raise ValueError("imposta prima le soglie di sicurezza per AUX TEMPERATURE e TEMPERATURE (Impostazioni del profilo)")
    nb = (cfg["pre"].get("not_before") or "").strip()
    if nb and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", nb):
        raise ValueError("orario di partenza non valido (formato HH:MM)")
    rmin = float(cfg["pre"].get("rif_min_s") or 0)
    if rmin and not any(ph.get("role") == "rif" and float(ph.get("s") or 0) >= rmin for ph in cfg["phases"]):
        raise ValueError(f"la sequenza deve iniziare con un passo di riferimento di almeno {rmin:g} s (serve a misurare la deriva della base)")
    if float(cfg.get("sample_s") or 5) > float(cfg["valid"].get("sample_max_s") or 6):
        raise ValueError(f"il campionamento ({cfg.get('sample_s')} s) supera il massimo ammesso ({cfg['valid'].get('sample_max_s')} s)")
    info = dict(body.get("info") or {})
    if info.get("tipo") not in ("taratura", "rilevamento"):
        info["tipo"] = "taratura" if info.get("reference") else "rilevamento"
    if info["tipo"] == "taratura":                       # la taratura si fa solo con lo schermo verificato attaccato
        info.update(config="attaccato", reference=True, ref_cuid="")
    else:
        info["reference"] = False
    if not (info.get("config") or "").strip():
        raise ValueError("indica la configurazione dello schermo")
    sts = [s for s in stations() if s["id"] in (body.get("station_ids") or [])]
    if not sts:
        raise ValueError("seleziona almeno una colonnina")
    pair = body.get("pair") or {}
    sent = pair.get("sentinel_id")
    if sent:
        if info["tipo"] != "rilevamento":
            raise ValueError("la prova in coppia si fa in modalità Rilevamento")
        if len(sts) != 2 or sent not in [x["id"] for x in sts]:
            raise ValueError("per la prova in coppia seleziona esattamente due colonnine e indica quale è la sentinella")
    pid = uuid.uuid4().hex[:8] if sent else ""
    if pid:
        PAIRS[pid] = {"cond": threading.Condition(), "ready": {}, "finished": {}, "res": {}}
    busy = [s["name"] for s in sts if any(not r["done"] and r["station_id"] == s["id"] for r in PROFRUNS.values())]
    if busy:
        raise ValueError("profilo già in corso su: " + ", ".join(busy))
    ids, rids = [], {st["id"]: uuid.uuid4().hex[:10] for st in sts}
    for st in sts:
        rid = rids[st["id"]]
        inf = dict(info)
        pr = None
        if pid:
            other = next(x for x in sts if x["id"] != st["id"])
            role = "sentinella" if st["id"] == sent else "prova"
            pr = {"id": pid, "role": role, "partner": rids[other["id"]], "partner_name": other["name"]}
            inf.update(coppia=pid, ruolo_coppia=role, ref_cuid="")
            if role == "sentinella":
                inf["config"] = "attaccato"
        PROFRUNS[rid] = {"id": rid, "station_id": st["id"], "station": st["name"], "host": st["host"], "info": inf, "pair": pr,
                         "repeats": max(1, int(body.get("repeats") or 1)), "rep_first": max(1, int(info.get("rep") or 1)),
                         "rep": None, "done": False, "state": "attesa", "phases": cfg["phases"], "history": [],
                         "result": None, "reason": "", "log": [], "samples": [], "conditions": [], "last": {}}
        PROFCTRL[rid] = {"stop": threading.Event(), "skip": threading.Event(), "station": st, "cfg": cfg}
        audit(st, "profilo termico v3", f"avviato: {inf.get('config')}" + (f" (coppia, {pr['role']})" if pr else ""), True)
        threading.Thread(target=_prof_thread, args=(rid,), daemon=True).start()
        ids.append(rid)
    return ids


def profile_runs():
    with LOCK:
        return json.loads(json.dumps([{k: v for k, v in r.items() if not k.startswith("_")} for r in list(PROFRUNS.values())[::-1]], default=str))


def _fnum(x):
    """Numero da un campo del CSV: accetta virgola decimale e unità (es. "30,49 C", "0,10 A", "1,2 °C")."""
    if x in (None, ""):
        return None
    v = _flt(str(x).replace(",", "."))
    return v if v is not None else _num(str(x))


def _summary_migrate():
    """Porta riepilogo_v3.csv e riferimenti.json al formato 1.11 (indicatore S, tipo di prova). Copia di sicurezza prima."""
    if not F_PROF_SUMMARY.exists():
        return
    with F_PROF_SUMMARY.open(encoding="utf-8-sig") as f:
        rd = csv.reader(f, delimiter=";")
        head = next(rd, [])
        if head == SUMMARY_COLS:
            return
        rows = [dict(zip(head, r)) for r in rd]
    bak = PROF_DIR / f"riepilogo_v3_copia_{datetime.now():%Y%m%d_%H%M%S}.csv"
    shutil.copy2(F_PROF_SUMMARY, bak)
    f4 = lambda v: "" if v is None else str(round(v, 4)).replace(".", ",")
    for r in rows:
        tar = r.get("riferimento") == "si"
        r.setdefault("tipo", "taratura" if tar else "rilevamento")
        meta = load(PROF_DIR / (r.get("file", "")[:-4] + ".json"), {}) if r.get("file") else {}
        ms = ((meta.get("risultati") or {}).get("misure") or [])[:2]
        ss = [m.get("pend_carica") for m in ms]
        if "S1" not in r:
            r["S1"], r["S2"] = (f4(ss[0]) if ss else ""), (f4(ss[1]) if len(ss) > 1 else "")
            r["S_medio"] = r.get("pendenza_carica", "")
        if "esito_I" not in r:
            r["esito_I"] = r.get("esito", "") if r.get("valida") == "si" and not tar else ""
            if tar and r.get("valida") == "si":
                r["esito"], r["I0"], r["Q"] = "taratura", "", ""
            elif r.get("valida") == "si":
                r["combinazione"] = "solo I"
    with F_PROF_SUMMARY.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(SUMMARY_COLS)
        for r in rows:
            w.writerow([r.get(c, "") for c in SUMMARY_COLS])
    # riferimenti: S delle tarature già fatte, ritrovate nel riepilogo per CUID e valore di I
    refs = load(F_PROF_REF, {})
    changed = False
    for cuid, ref in refs.items():
        cand = [r for r in rows if r.get("cuid") == cuid and r.get("riferimento") == "si" and r.get("valida") == "si"]
        for t in ref.get("tests", []):
            if "S" in t:
                continue
            m = next((r for r in cand if _fnum(r.get("I_medio")) is not None and abs(_fnum(r["I_medio"]) - t["I"]) < 1e-3), None)
            t["S"] = _fnum(m.get("S_medio")) if m else None
            if m:
                t.setdefault("file", m.get("file", ""))
            changed = True
        s0 = _median(t.get("S") for t in ref.get("tests", []))
        if ref.get("S0") != (round(s0, 4) if s0 is not None else None):
            ref["S0"], changed = (round(s0, 4) if s0 is not None else None), True
    if changed:
        save(F_PROF_REF, refs)
    audit(None, "profilo termico", f"riepilogo convertito al formato {VERSION} (copia: {bak.name})", True)


PROF_TRASH = PROF_DIR / "cestino"
_NAME_RX = re.compile(r"[A-Za-z0-9._-]+\.csv")


def _summary_read():
    _summary_migrate()
    if not F_PROF_SUMMARY.exists():
        return []
    with F_PROF_SUMMARY.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _summary_write(rows):
    tmp = F_PROF_SUMMARY.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(SUMMARY_COLS)
        for r in rows:
            w.writerow([r.get(c, "") for c in SUMMARY_COLS])
    os.replace(tmp, F_PROF_SUMMARY)


def _ref_recalc(ref):
    i0, s0 = _median(t.get("I") for t in ref["tests"]), _median(t.get("S") for t in ref["tests"])
    ref["I0"] = round(i0, 4) if i0 is not None else None
    ref["S0"] = round(s0, 4) if s0 is not None else None


def _ref_remove(refs, row):
    """Toglie dai riferimenti la taratura del riepilogo (per nome del file, o per valore di I se registrata prima della 1.11)."""
    ref = refs.get(row.get("cuid", ""))
    if not ref:
        return False
    tests = ref.get("tests", [])
    k = next((i for i, t in enumerate(tests) if t.get("file") == row.get("file")), None)
    if k is None:
        I = _fnum(row.get("I_medio"))
        k = next((i for i, t in enumerate(tests) if not t.get("file") and I is not None and abs(t["I"] - I) < 1e-3), None)
    if k is None:
        return False
    tests.pop(k)
    if tests:
        _ref_recalc(ref)
    else:
        refs.pop(row["cuid"], None)
    return True


def _check_names(files):
    files = [f for f in files if f]
    if not files or any(not _NAME_RX.fullmatch(f) for f in files):
        raise ValueError("nome di file non valido")
    busy = {r.get("file") for r in PROFRUNS.values() if not r["done"]}
    if busy & set(files):
        raise ValueError("la prova è ancora in corso: fermala prima")
    return files


def profile_delete(body):
    """Cancella prove: tolte dal riepilogo e, se tarature, dai riferimenti; CSV e JSON spostati in profiles/cestino."""
    files = _check_names(body.get("files") or [body.get("file")])
    with LOCK:
        rows = _summary_read()
        gone = [r for r in rows if r.get("file") in files]
        _summary_write([r for r in rows if r.get("file") not in files])
        refs = load(F_PROF_REF, {})
        changed = [r["file"] for r in gone if r.get("valida") == "si" and (r.get("tipo") == "taratura" or r.get("riferimento") == "si") and _ref_remove(refs, r)]
        if changed:
            save(F_PROF_REF, refs)
        PROF_TRASH.mkdir(exist_ok=True)
        for f in files:
            for p in (PROF_DIR / f, PROF_DIR / (f[:-4] + ".json")):
                if p.exists():
                    os.replace(p, PROF_TRASH / p.name)
        for k in [k for k, r in PROFRUNS.items() if r["done"] and r.get("file") in files]:
            PROFRUNS.pop(k, None)
            PROFCTRL.pop(k, None)
    for r in gone:
        audit(None, "profilo termico", f"prova cancellata (nel cestino): {r['file']} {r.get('cuid', '')}", True,
              "tolta dai riferimenti" if r["file"] in changed else "")
    return {"deleted": len(gone), "refs_changed": len(changed)}


def profile_force(body):
    """Forza valida una prova non valida (decisione dell'operatore) e la ricalcola con i parametri attuali."""
    file = _check_names([body.get("file")])[0]
    meta_p = PROF_DIR / (file[:-4] + ".json")
    meta = load(meta_p, {})
    res = dict(meta.get("risultati") or {})
    ms = res.get("misure") or []
    if res.get("I_medio") is None and len(ms) >= 2 and ms[0].get("I") is not None and ms[1].get("I") is not None:
        I1, I2 = ms[0]["I"], ms[1]["I"]
        res.update(I1=I1, I2=I2, I_medio=(I1 + I2) / 2, C=abs(I1 - I2) / ((I1 + I2) / 2) if I1 + I2 else None)
    if res.get("I_medio") is None:
        raise ValueError("l'indice non è stato calcolato su entrambe le misure (prova interrotta o dati mancanti): non si può forzare valida")
    if "S_medio" not in res:
        ss = [m.get("pend_carica") for m in ms[:2]]
        res.update(S1=ss[0] if ss else None, S2=ss[1] if len(ss) > 1 else None,
                   S_medio=sum(ss) / 2 if len(ss) == 2 and None not in ss else None)
    info = dict(meta.get("info") or {})
    info.setdefault("tipo", "taratura" if info.get("reference") else "rilevamento")
    tar = info["tipo"] == "taratura"
    cuid = meta.get("cuid", "")
    old = meta.get("motivi") or []
    cfg = profile_cfg()
    with LOCK:
        rows = _summary_read()
        row = next((r for r in rows if r.get("file") == file), None)
        if row is None:
            raise KeyError("prova non trovata nel riepilogo")
        refs = load(F_PROF_REF, {})
        if tar and _ref_remove(refs, row):
            save(F_PROF_REF, refs)
        res.update(valid=True, reasons=[], notes=[])
        ev = _classify(res, cuid, cfg, info)
        if tar:
            r = _ref_add(cuid, ev, info, file)
            ref = _ref_of({cuid: r}, cuid, int(cfg["index"].get("ref_min") or 3))
            ev.update(I0_new=ref["I0_all"], S0_new=ref["S0_all"], n_new=ref["n"], nS_new=ref["nS"], ref_min=ref["ref_min"])
        ev["notes"].insert(0, "forzata valida dall'operatore" + (": " + "; ".join(old) if old else ""))
        row.update(_ev_fields(ev, tar))
        row.update(forzata="si", motivo=("forzata valida; motivi originali: " + " | ".join(old)) if old else "forzata valida")
        _summary_write(rows)
        save(meta_p, {**meta, "esito": ev.get("esito"), "valida": True, "forzata": {"data": datetime.now().isoformat(timespec="seconds"), "motivi_originali": old},
                      "note_calcolo": ev.get("notes"), "risultati": {k: v for k, v in ev.items() if k not in ("reasons", "notes")}})
        for r in PROFRUNS.values():
            if r["done"] and r.get("file") == file:
                r["eval"] = ev
    audit(None, "profilo termico", f"prova forzata valida: {file} {cuid}", True, f"esito {ev.get('esito')}; motivi originali: {'; '.join(old)}")
    return {"esito": ev.get("esito"), "eval": json.loads(json.dumps(ev, default=str))}


def profile_series(name):
    """Campioni di una prova registrata (PoleDesk o prova_schermo.sh), per i grafici."""
    path = sc_series_path(name)
    rows, phase_t, phases, _ = _load_test_csv(path)
    rows = [r for r in rows if r["ok"]]
    out = {"file": name, "t": [r["t"] for r in rows], "passo": [r["i"] + 1 for r in rows],
           "aux": [r["aux"] or None for r in rows], "temp": [r["temp"] or None for r in rows],
           "cpu": [r.get("cpu") or None for r in rows], "led": [r["led"] for r in rows], "lux": [r.get("lux") for r in rows]}
    out["phases"] = [{"i": i + 1, "name": ph["name"], "heater": bool(ph["heater"]), "fan": bool(ph["fan"]), "t0": phase_t[i],
                      "t1": max((r["t"] for r in rows if r["i"] == i), default=phase_t[i])} for i, ph in enumerate(phases) if i in phase_t]
    meta = load(PROF_DIR / (name[:-4] + ".json"), {})
    out["t_on"] = [m.get("t_on") for m in ((meta.get("risultati") or {}).get("misure") or [])]
    out["T_base"] = (meta.get("risultati") or {}).get("T_base")
    out["fit"] = [{"t_on": m.get("t_on"), "tau": m.get("tau"), "Tinf": m.get("Tinf"), "A": m.get("fit_A"),
                   "t_end": next((ph["t1"] for ph in out["phases"] if ph["t0"] <= (m.get("t_on") or 0) <= ph["t1"] + 5), None)}
                  for m in ((meta.get("risultati") or {}).get("misure") or []) if m.get("tau")]
    out["row"] = next((r for r in prove_unite() if r.get("file") == name), {})
    if not out["fit"] and path.parent != PROF_DIR:      # prove di Prova/Campo: accensione della ventola dalle misure
        u = (_UNI["dec"] or {}).get(name)
        out["t_on"] = [m.get("t_on") for m in ((u or {}).get("ev") or {}).get("misure", [])]
    return out


def profile_history(all_rows=False):
    """Tutte le prove (Profilo termico, Prova, Campo) con l'esito del motore unico, la più recente per prima."""
    _summary_migrate()
    rows = prove_unite()
    return {"rows": rows[::-1] if all_rows else rows[::-1][:400], "legenda": esiti_legenda()}


# ---------------------------------------------------------------- ricalcolo, importazione, modifica, campagne

F_CAMP = PROF_DIR / "campagne.json"
F_PROF_LAST = PROF_DIR / "ultima_prova.json"


def _load_test_csv(path):
    """Legge il CSV di una prova, di PoleDesk o di prova_schermo.sh, nel formato usato da _prof_evaluate."""
    with path.open(encoding="utf-8-sig") as f:
        data = list(csv.DictReader(f, delimiter=";"))
    rows, phase_t, ph, cuid = [], {}, {}, ""
    for r in data:
        try:
            t = float(r["epoch"]) if r.get("epoch") else datetime.fromisoformat(r["timestamp"]).timestamp()
            i = int(r.get("passo") or 0) - 1
        except (ValueError, KeyError, TypeError):
            continue
        if i < 0:
            continue
        g = lambda c: _fnum(r.get(c))
        hs = r.get("stato_riscaldatore", r.get("aux_heater_power"))
        fs = r.get("stato_ventola", r.get("aux_fan_power"))
        rows.append({"t": t, "i": i, "ok": 1 if str(r.get("ok", "1")).strip() in ("1", "True", "true") else 0,
                     "aux": g("aux_temperature"), "temp": g("temperature"), "cpu": g("cpu_temperature"), "led": g("aux_leds_current"), "lux": g("aux_lux"),
                     "user": r.get("user_status") or None, "auxfault": g("aux_fault"), "auxfault_raw": r.get("aux_fault"),
                     "tamper": r.get("antitamper") or None,
                     "heater": _onoff(hs) if hs not in (None, "") else None, "fan": _onoff(fs) if fs not in (None, "") else None})
        cuid = cuid or (r.get("cuid") or "")
        if i not in phase_t:
            phase_t[i] = t
            ph[i] = {"name": r.get("nome_passo", ""), "heater": int(r.get("cmd_riscaldatore") == "1"),
                     "fan": int(r.get("cmd_ventola") == "1")}
    phases, seen_rif = [], False
    keys = sorted(ph)
    for i in range(max(keys) + 1 if keys else 0):
        x = ph.get(i, {"name": f"passo {i + 1}", "heater": 0, "fan": 0})
        role = "carica" if x["heater"] and not x["fan"] else "misura" if x["fan"] and not x["heater"] else \
            ("recupero" if seen_rif else "rif") if not x["heater"] and not x["fan"] else "altro"
        seen_rif = seen_rif or role == "rif"
        nxt = phase_t.get(i + 1)
        last = max((r["t"] for r in rows if r["i"] == i), default=phase_t.get(i, 0))
        phases.append({**x, "role": role, "s": round((nxt - phase_t[i]) if nxt and i in phase_t else (last - phase_t.get(i, last) + 5))})
    return rows, phase_t, phases, cuid


def _t_air(info):
    v = _fnum(info.get("ambient"))
    if v is None:
        v = _flt((info.get("meteo") or {}).get("temp"))
    return v


def _eval_file(file, cfg, meta=None, t_air=None, in_coppia=False):
    """Rielabora una prova dai suoi campioni con i parametri attuali (sequenza della prova stessa)."""
    meta = meta if meta is not None else load(PROF_DIR / (file[:-4] + ".json"), {})
    rows, phase_t, phases_csv, cuid = _load_test_csv(PROF_DIR / file)
    if not rows:
        return None, cuid
    phases = ((meta.get("parametri") or {}).get("phases")) or phases_csv
    c = {**cfg, "phases": phases}
    if t_air is None:
        t_air = _t_air(meta.get("info") or {})
    return _prof_evaluate(rows, phase_t, {"led_ref": meta.get("led_rif"), "t_air": t_air, "in_coppia": in_coppia}, c), cuid or meta.get("cuid", "")


def _meta_info(meta, row):
    info = dict(meta.get("info") or {})
    info.setdefault("tipo", "taratura" if info.get("reference") or row.get("riferimento") == "si" else "rilevamento")
    for col, key in (("configurazione", "config"), ("luogo", "place"), ("temp_esterna", "ambient"), ("note", "notes"),
                     ("campagna", "campagna"), ("livello_led", "led_level")):
        if row.get(col, "") != "" or key in ("campagna",):
            info[key] = row.get(col, "")
    return info


def _row_time(r):
    try:
        return datetime.strptime(r.get("data_ora", ""), "%d/%m/%Y %H:%M").timestamp()
    except ValueError:
        return 0


def profile_recalc(body=None):
    """Ricalcola tutte le prove dai CSV con i parametri attuali. Le tarature restano nel riferimento solo se c'erano
    (gli azzeramenti sono rispettati) e se sono ancora valide o forzate. Copia di sicurezza prima."""
    cfg = profile_cfg()
    ix, ref_min = cfg["index"], int(cfg["index"].get("ref_min") or 3)
    out = {"ricalcolate": 0, "saltate": 0, "cambiate": 0, "tarature_uscite": 0}
    with LOCK:
        rows = _summary_read()
        if not rows:
            return out
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(F_PROF_SUMMARY, PROF_DIR / f"riepilogo_v3_copia_{stamp}.csv")
        if F_PROF_REF.exists():
            shutil.copy2(F_PROF_REF, PROF_DIR / f"riferimenti_copia_{stamp}.json")
        old_refs = load(F_PROF_REF, {})
        member = set()                                    # tarature presenti oggi nel riferimento
        for r in rows:
            ref = old_refs.get(r.get("cuid", ""))
            if not ref:
                continue
            I = _fnum(r.get("I_medio"))
            if any(t.get("file") == r.get("file") or (not t.get("file") and I is not None and abs(t["I"] - I) < 1e-3) for t in ref.get("tests", [])):
                member.add(r["file"])
        files = {r["file"] for r in rows}
        refs = {}
        for cu, ref in old_refs.items():                   # tarature senza riga nel riepilogo: restano come sono
            keep = [t for t in ref.get("tests", []) if t.get("file") and t["file"] not in files]
            if keep:
                refs[cu] = {**ref, "tests": keep}
        evs = {}
        order = sorted(rows, key=lambda r: (_row_time(r), r.get("file", "")))
        for phase in ("taratura", "rilevamento"):
            for r in order:
                tar = r.get("tipo") == "taratura" or r.get("riferimento") == "si"
                if tar != (phase == "taratura"):
                    continue
                if r.get("esito") == "interrotta" or not (PROF_DIR / r["file"]).exists():
                    out["saltate"] += 1
                    continue
                meta_p = PROF_DIR / (r["file"][:-4] + ".json")
                meta = load(meta_p, {})
                info = _meta_info(meta, r)
                try:
                    res, cuid = _eval_file(r["file"], cfg, meta, _t_air(info), r.get("ruolo_coppia") in ("prova", "sentinella"))
                except Exception as e:
                    res = None
                if not res:
                    out["saltate"] += 1
                    continue
                cuid = r.get("cuid") or cuid
                forced = r.get("forzata") == "si"
                if forced and res.get("I_medio") is not None:
                    res["notes"].insert(0, "forzata valida dall'operatore: " + "; ".join(res["reasons"]))
                    res.update(valid=True, reasons=[])
                ev = _classify(res, cuid, cfg, info, refs)
                if tar:
                    if r["file"] in member and ev.get("valid") and r.get("fuori_rif") != "si":
                        rr = _ref_add(cuid, ev, info, r["file"], refs)
                        rf = _ref_of({cuid: rr}, cuid, ref_min)
                        ev.update(I0_new=rf["I0_all"], S0_new=rf["S0_all"], n_new=rf["n"], nS_new=rf["nS"], ref_min=ref_min)
                    elif r["file"] in member:
                        out["tarature_uscite"] += 1
                        ev["notes"].append("non più valida con i parametri attuali: tolta dal riferimento")
                before = (r.get("esito"), r.get("valida"))
                r.update(_ev_fields(ev, tar))
                if forced:
                    r["valida"] = "si"
                out["cambiate"] += (r.get("esito"), r.get("valida")) != before
                evs[r["file"]] = ev
                out["ricalcolate"] += 1
                save(meta_p, {**meta, "esito": ev.get("esito"), "valida": ev.get("valid"), "motivi": ev.get("reasons"),
                              "note_calcolo": ev.get("notes"), "ricalcolata": datetime.now().isoformat(timespec="seconds"),
                              "risultati": {k: v for k, v in ev.items() if k not in ("reasons", "notes")}})
        for r in rows:                                     # coppie con i nuovi valori
            if r.get("ruolo_coppia") == "prova" and r.get("file") in evs and r.get("file_coppia") in evs:
                comp = _pair_compare(evs[r["file"]], evs[r["file_coppia"]], ix)
                _pair_rows(rows, r.get("coppia", ""), r["file"], r["file_coppia"], comp)
        for cu in refs:
            _ref_recalc(refs[cu])
        save(F_PROF_REF, refs)
        _summary_write(rows)
    audit(None, "profilo termico", "ricalcolo di tutte le prove con i parametri attuali", True,
          f"{out['ricalcolate']} ricalcolate, {out['cambiate']} con esito cambiato, {out['saltate']} saltate (copia {stamp})")
    out["copia"] = stamp
    return out


def profile_import(body):
    """Importa il CSV di prova_schermo.sh (o di PoleDesk) e lo valuta con lo stesso calcolo delle prove dal PC."""
    name = os.path.basename(str(body.get("name") or "prova.csv"))
    text = body.get("content") or ""
    head = text.lstrip("\ufeff").split("\n", 1)[0]
    if "aux_temperature" not in head or "passo" not in head:
        raise ValueError("non è il CSV di una prova (mancano le colonne passo e aux_temperature)")
    base = _safe_name(re.sub(r"\.csv$", "", name, flags=re.I))
    file = base + ".csv"
    if (PROF_DIR / file).exists():
        if (PROF_DIR / file).read_text(encoding="utf-8-sig") == text.lstrip("\ufeff"):
            raise ValueError(f"prova già importata: {file}")
        file = f"{base}_imp{datetime.now():%H%M%S}.csv"
    (PROF_DIR / file).write_text(text.lstrip("\ufeff"), encoding="utf-8-sig")
    rows, phase_t, phases, cuid = _load_test_csv(PROF_DIR / file)
    if len(rows) < 5:
        (PROF_DIR / file).unlink()
        raise ValueError("il file non contiene campioni leggibili")
    inf = dict(body.get("info") or {})
    tipo = "taratura" if inf.get("tipo") == "taratura" else "rilevamento"
    m = re.match(r"(.+?)_(.+)_(\d{8})_(\d{4})$", base)
    info = {"tipo": tipo, "reference": tipo == "taratura", "config": "attaccato" if tipo == "taratura" else (inf.get("config") or (m.group(2) if m else "")),
            "place": inf.get("place", ""), "ambient": inf.get("ambient", ""), "notes": inf.get("notes", ""), "campagna": inf.get("campagna", ""),
            "ref_cuid": inf.get("ref_cuid", ""), "origine": "prova_schermo.sh" if "epoch" in head else "importata"}
    meta = {"spec": "v3", "cuid": cuid, "colonnina": inf.get("station", ""), "info": info, "origine": info["origine"],
            "inizio": datetime.fromtimestamp(rows[0]["t"]).astimezone().isoformat(timespec="seconds"),
            "parametri": {"phases": phases}, "importata": datetime.now().isoformat(timespec="seconds")}
    cfg = profile_cfg()
    with LOCK:
        ev = _classify(_eval_file(file, cfg, meta)[0], cuid, cfg, info)
        if tipo == "taratura" and ev.get("valid"):
            r = _ref_add(cuid, ev, info, file)
            rf = _ref_of({cuid: r}, cuid, int(cfg["index"].get("ref_min") or 3))
            ev.update(I0_new=rf["I0_all"], S0_new=rf["S0_all"], n_new=rf["n"], nS_new=rf["nS"], ref_min=rf["ref_min"])
        save(PROF_DIR / (file[:-4] + ".json"), {**meta, "esito": ev.get("esito"), "valida": ev.get("valid"), "motivi": ev.get("reasons"),
                                                 "note_calcolo": ev.get("notes"),
                                                 "risultati": {k: v for k, v in ev.items() if k not in ("reasons", "notes")}})
        row = {"file": file, "cuid": cuid, "colonnina": inf.get("station", "") or "(sulla colonnina)",
               "data_ora": datetime.fromtimestamp(rows[0]["t"]).strftime("%d/%m/%Y %H:%M"), "tipo": tipo, "ripetizione": 1,
               "riferimento": "si" if tipo == "taratura" else "no", "livello_led": _f4(ev.get("led")), "campioni": ev.get("campioni", ""),
               **_info_fields(info), **_ev_fields(ev, tipo == "taratura")}
        rws = _summary_read()
        rws.append(row)
        rws.sort(key=_row_time)
        _summary_write(rws)
    audit(None, "profilo termico", f"prova importata: {file} {cuid}", True, ev.get("esito", ""))
    return {"file": file, "cuid": cuid, "esito": ev.get("esito"), "valid": ev.get("valid"), "reasons": ev.get("reasons"),
            "notes": ev.get("notes"), "I_medio": ev.get("I_medio"), "S_medio": ev.get("S_medio")}


EDITABLE = {"configurazione": "config", "luogo": "place", "temp_esterna": "ambient", "livello_led": "led_level",
            "note": "notes", "campagna": "campagna"}


def profile_edit(body):
    """Corregge i dati descrittivi di una prova (luogo, temperatura esterna, schermo, LED, note, campagna)."""
    file = _check_names([body.get("file")])[0]
    f = body.get("fields") or {}
    with LOCK:
        rows = _summary_read()
        row = next((r for r in rows if r.get("file") == file), None)
        if row is None:
            raise KeyError("prova non trovata nel riepilogo")
        meta_p = PROF_DIR / (file[:-4] + ".json")
        meta = load(meta_p, {})
        info = dict(meta.get("info") or {})
        changed = []
        for col, key in EDITABLE.items():
            if col not in f or (col == "configurazione" and row.get("tipo") == "taratura"):
                continue
            v = str(f[col] or "").strip()
            if v != row.get(col, ""):
                changed.append(f"{col}: {row.get(col, '')!r} → {v!r}")
                row[col], info[key] = v, v
                if col == "temp_esterna":
                    info.pop("ambient_src", None)
        if changed:
            _summary_write(rows)
            if meta:
                save(meta_p, {**meta, "info": info, "modifiche": (meta.get("modifiche") or []) +
                              [{"data": datetime.now().isoformat(timespec="seconds"), "campi": changed}]})
    if changed:
        audit(None, "profilo termico", f"dati della prova modificati: {file}", True, "; ".join(changed))
    return {"changed": changed}


def campaigns():
    c = load(F_CAMP, {})
    for r in profile_history(True)["rows"]:
        if r.get("campagna") and r["campagna"] not in c:
            c[r["campagna"]] = {"obiettivo": "", "creata": ""}
    return c


def campaign_save(body):
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("indica il nome della campagna")
    c = load(F_CAMP, {})
    c[name] = {**c.get(name, {"creata": datetime.now().isoformat(timespec="seconds"), "obiettivo": ""}),
               **{k: str(body.get(k) or "") for k in ("obiettivo", "conclusioni") if k in body}}
    save(F_CAMP, c)
    return c


def profile_ref_reset(body):
    cu = body.get("cuid", "")
    with LOCK:
        refs = load(F_PROF_REF, {})
        refs.pop(cu, None)
        save(F_PROF_REF, refs)
        rows = _summary_read()
        hit = [r for r in rows if r.get("cuid") == cu and (r.get("tipo") == "taratura" or r.get("riferimento") == "si")]
        for r in hit:
            r["fuori_rif"] = "si"
        if hit:
            _summary_write(rows)
    audit(None, "profilo termico v3", f"riferimento azzerato: {body.get('cuid')}", True)


# ---------------------------------------------------------------- anagrafica

def public_jump(j):
    return {k: v for k, v in j.items() if k != "password"} | {"has_password": bool(j.get("password"))}


def jumps_save(body):
    old = {j["id"]: j for j in jumps()}
    out = []
    for j in body.get("jumps", []):
        jid = j.get("id") or uuid.uuid4().hex[:8]
        if not (j.get("host") or "").strip() or not (j.get("name") or "").strip():
            raise ValueError("ogni jump host deve avere nome e indirizzo")
        prev = old.get(jid, {})
        host, user = j["host"].strip(), (j.get("username") or "").strip()
        if "@" in host:                    # stringa di connessione di un PAM incollata nell'indirizzo: utente@host
            user, host = host.rsplit("@", 1)
        rec = {"id": jid, "name": j["name"].strip(), "host": host, "port": int(j.get("port") or 22), "tunnel": j.get("tunnel") or prev.get("tunnel", "auto"),
               "username": user, "key_path": (j.get("key_path") or "").strip(),
               "source_ip": (j.get("source_ip") or "").strip(), "via": (j.get("via") or "").strip() if j.get("via") != jid else "",
               "password": prev.get("password", "")}
        if j.get("password"):
            rec["password"] = j["password"]
        if j.get("password_clear"):
            rec["password"] = ""
        out.append(rec)
        POOL.drop("jump:" + jid)
    save(F_JUMPS, out)
    for st in stations():                         # le connessioni che passano dai jump host vanno rifatte
        if st.get("jump") != "direct":
            POOL.drop(st["id"])
    return [public_jump(j) for j in out]


def jump_test(body):
    j = _jump_by_id(body.get("id", ""))
    if not j:
        raise KeyError("jump host non trovato")
    POOL.drop("jump:" + j["id"])
    t0 = time.time()
    c = POOL.jump_client(j)
    chan = c.get_transport().open_session(timeout=10)
    chan.settimeout(10)
    chan.exec_command("hostname; uname -sr")
    out = b""
    while not chan.exit_status_ready() or chan.recv_ready():
        if chan.recv_ready():
            out += chan.recv(4096)
        else:
            time.sleep(0.05)
        if time.time() - t0 > 15:
            break
    chan.close()
    res = {"ok": True, "ms": int((time.time() - t0) * 1000), "info": out.decode("utf-8", "replace").strip()}
    if body.get("target"):                          # prova anche il passaggio verso una colonnina
        host, _, port = body["target"].partition(":")
        POOL.channel(j, host, int(port or 22)).close()
        res["target"] = f"{host}:{port or 22} raggiungibile dal jump host"
    audit(None, "jump host", f"prova {j['name']}", True, res.get("target", ""))
    return res


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def public_station(s):
    return {k: v for k, v in s.items() if k != "password"} | {"has_password": bool(s.get("password"))}


def upsert_station(body):
    sts = stations()
    sid = body.get("id") or uuid.uuid4().hex[:8]
    old = next((s for s in sts if s["id"] == sid), {})
    st = {"id": sid, "name": (body.get("name") or body.get("host") or "").strip(),
          "host": (body.get("host") or "").strip(), "port": int(body.get("port") or 22),
          "username": (body.get("username") or "").strip(), "key_path": (body.get("key_path") or "").strip(),
          "source_ip": (body.get("source_ip") or "").strip(), "jump": (body.get("jump") or "").strip(),
          "tags": [t for t in body.get("tags", []) if t], "vars": body.get("vars") or {},
          "password": old.get("password", "")}
    if not st["host"]:
        raise ValueError("indirizzo IP obbligatorio")
    if body.get("password"):
        st["password"] = body["password"]
    if body.get("password_clear"):
        st["password"] = ""
    sts = [s for s in sts if s["id"] != sid] + [st]
    save(F_STATIONS, sts)
    POOL.drop(sid)
    return st


def import_csv(text):
    sample = text[:2000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    known = {"name", "host", "port", "username", "password", "key_path", "tags", "source_ip", "jump"}
    sts, added, updated = stations(), 0, 0
    by_host = {(s["host"], s.get("source_ip", "")): s for s in sts}
    for r in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        host = r.get("host") or r.get("ip")
        if not host:
            continue
        cur = by_host.get((host, r.get("source_ip", "")))
        st = cur or {"id": uuid.uuid4().hex[:8], "host": host, "vars": {}, "tags": []}
        st["name"] = r.get("name") or st.get("name") or host
        st["port"] = int(r.get("port") or st.get("port") or 22)
        for k in ("username", "password", "key_path", "source_ip"):
            if r.get(k):
                st[k] = r[k]
        if r.get("jump"):                                # nome o id del jump host, oppure "diretto"
            v = r["jump"].strip()
            jm = next((x for x in jumps() if x["id"] == v or x["name"].lower() == v.lower()), None)
            st["jump"] = "direct" if v.lower() in ("diretto", "direct", "nessuno") else (jm["id"] if jm else st.get("jump", ""))
        if r.get("tags"):
            st["tags"] = [t for t in re.split(r"[|, ]+", r["tags"]) if t]
        st["vars"] = {**st.get("vars", {}), **{k: v for k, v in r.items() if k not in known | {"ip"} and v}}
        if cur:
            updated += 1
        else:
            sts.append(st)
            by_host[(host, st.get("source_ip", ""))] = st
            added += 1
    save(F_STATIONS, sts)
    return {"added": added, "updated": updated}


def export_csv():
    sts = stations()
    var_keys = sorted({k for s in sts for k in (s.get("vars") or {})})
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    jn = {j["id"]: j["name"] for j in jumps()}
    w.writerow(["name", "host", "port", "username", "key_path", "source_ip", "jump", "tags", *var_keys])
    for s in sts:
        w.writerow([s["name"], s["host"], s.get("port", 22), s.get("username", ""), s.get("key_path", ""), s.get("source_ip", ""),
                    "diretto" if s.get("jump") == "direct" else jn.get(s.get("jump", ""), ""),
                    "|".join(s.get("tags", [])), *[(s.get("vars") or {}).get(k, "") for k in var_keys]])
    return buf.getvalue()


# ---------------------------------------------------------------- distribuzione: pacchetti di file e comandi su molte colonnine

DIST_DIR = DATA / "distribuzione"
DIST_FILES, DIST_RUNS, DIST_OUT = DIST_DIR / "file", DIST_DIR / "esecuzioni", DIST_DIR / "raccolte"
for _d in (DIST_DIR, DIST_FILES, DIST_RUNS, DIST_OUT):
    _d.mkdir(parents=True, exist_ok=True)
F_PKGS = DIST_DIR / "pacchetti.json"
MAX_FETCH = 200_000_000          # byte massimi scaricati per file
_PKG_FILE_RX = re.compile(r"[A-Za-z0-9._+-]{1,120}")
_GLOB_RX = re.compile(r"[A-Za-z0-9._*?\[\]/+-]{1,200}")
DISTRUNS, DISTCTRL = {}, {}

_PS = "cd {{dir}} && sh prova_schermo.sh"
DEFAULT_PKGS = [
    {"id": "ps-installa", "name": "Prova schermo · copia lo script e controlla", "dir": "/home/root/test", "files": [],
     "params": [],
     "steps": [{"type": "invia"},
               {"type": "comando", "command": _PS + " --stato", "timeout": 60, "expect": "TempContEn letto: 1"}],
     "note": "Aggiungi prova_schermo.sh (e se vuoi prova_schermo.conf e i seq_*.conf) con «Aggiungi file»."},
    {"id": "ps-piano", "name": "Prova schermo · lancia un piano", "dir": "/home/root/test", "files": [],
     "params": [{"name": "quando", "default": "--subito --prova", "descr": "--subito | --tra MIN | --alle HH:MM[,HH:MM] [--giorni N] [--prova] [--scala X]"},
                {"name": "stato_schermo", "default": "", "descr": "attaccato / tolto; vuoto = variabile stato_schermo della colonnina"},
                {"name": "sequenze", "default": "\"T1=rif:20 ventola:20 recupero:20 carica:20 recupero:20\" \"T2=rif:20 carica:20 misura:30 recupero:20 carica:20 misura:30\"",
                 "descr": "una o più sequenze \"NOME=ruolo:secondi ...\" oppure nomi di file seq_NOME.conf"}],
     "steps": [{"type": "comando", "command": _PS + " {{quando}} --nome {{stato_schermo}} {{sequenze}}", "timeout": 60, "expect": "Avvii:"}]},
    {"id": "ps-stato", "name": "Prova schermo · stato", "dir": "/home/root/test", "files": [], "params": [],
     "steps": [{"type": "comando", "command": _PS + " --stato; tail -n 5 programmata.log 2>/dev/null", "timeout": 60}]},
    {"id": "ps-ferma", "name": "Prova schermo · annulla il piano e ferma la prova", "dir": "/home/root/test", "files": [], "params": [],
     "steps": [{"type": "comando", "command": _PS + " --annulla; sh prova_schermo.sh --ferma", "timeout": 60},
               {"type": "comando", "command": "sleep 20; " + _PS + " --stato", "timeout": 90, "expect": "TempContEn letto: 1"}]},
    {"id": "ps-raccogli", "name": "Prova schermo · raccogli i dati", "dir": "/home/root/test", "files": [], "params": [],
     "steps": [{"type": "comando", "command": _PS + " --archivio", "timeout": 120, "expect": "Archivio pronto"},
               {"type": "scarica", "pattern": "archivio_*.tar.gz", "newest": True, "delete": True, "import": True}]},
]


def packages():
    p = load(F_PKGS, None)
    if p is None:
        save(F_PKGS, DEFAULT_PKGS)
        return json.loads(json.dumps(DEFAULT_PKGS))
    return p


def _pkg(pid):
    p = next((x for x in packages() if x["id"] == pid), None)
    if not p:
        raise KeyError("pacchetto non trovato")
    return p


def _clean_pkg(p):
    steps = []
    for s in p.get("steps") or []:
        t = s.get("type")
        if t == "invia":
            steps.append({"type": "invia", "only": [f for f in (s.get("only") or []) if f]})
        elif t == "comando":
            if not (s.get("command") or "").strip():
                raise ValueError("un passo comando è vuoto")
            steps.append({"type": "comando", "command": s["command"], "timeout": int(s.get("timeout") or 60),
                          "expect": s.get("expect") or "", "continue": bool(s.get("continue"))})
        elif t == "scarica":
            pat = (s.get("pattern") or "").strip()
            if not _GLOB_RX.fullmatch(pat) or ".." in pat:
                raise ValueError(f"file da scaricare non valido: {pat!r} (lettere, numeri, . _ - / e i caratteri jolly * ?)")
            steps.append({"type": "scarica", "pattern": pat, "newest": bool(s.get("newest")), "delete": bool(s.get("delete")),
                          "import": bool(s.get("import"))})
        elif t == "attendi":
            steps.append({"type": "attendi", "seconds": max(1, int(s.get("seconds") or 10))})
    d = (p.get("dir") or "").strip()
    if not d.startswith("/") or not re.fullmatch(r"[A-Za-z0-9._/+-]+", d):
        raise ValueError("cartella sulla colonnina non valida (percorso assoluto, es. /home/root/test)")
    params = [{"name": x["name"].strip(), "default": x.get("default", ""), "descr": x.get("descr", "")}
              for x in p.get("params") or [] if re.fullmatch(r"\w+", (x.get("name") or "").strip())]
    return {"id": p.get("id") or uuid.uuid4().hex[:8], "name": (p.get("name") or "senza nome").strip(), "dir": d.rstrip("/") or "/",
            "files": [f for f in p.get("files") or [] if _PKG_FILE_RX.fullmatch(f.get("name", ""))],
            "params": params, "steps": steps, "note": p.get("note", "")}


def pkg_save(body):
    with LOCK:
        pk = packages()
        if body.get("delete"):
            pid = body["delete"]
            pk = [x for x in pk if x["id"] != pid]
            shutil.rmtree(DIST_FILES / pid, ignore_errors=True)
        elif body.get("import"):
            for x in body["import"] if isinstance(body["import"], list) else [body["import"]]:
                files = x.get("files") or []
                c = _clean_pkg({**x, "id": uuid.uuid4().hex[:8], "files": []})
                for f in files:                        # i file viaggiano nel JSON esportato (base64)
                    if f.get("data") and _PKG_FILE_RX.fullmatch(f.get("name", "")):
                        c["files"].append(_store_file(c["id"], f["name"], base64.b64decode(f["data"]), f.get("exec")))
                pk.append(c)
        else:
            c = _clean_pkg(body["package"])
            old = next((x for x in pk if x["id"] == c["id"]), None)
            if old:   # i file si aggiungono e tolgono con le loro chiamate: qui conta solo l'elenco (con exec)
                names = {f["name"]: f for f in old.get("files", [])}
                c["files"] = [{**names[f["name"]], "exec": bool(f.get("exec"))} for f in c["files"] if f["name"] in names]
                pk = [c if x["id"] == c["id"] else x for x in pk]
            else:
                c["files"] = []
                pk.append(c)
        save(F_PKGS, pk)
        return pk


def _store_file(pid, name, data, exe=None):
    d = DIST_FILES / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return {"name": name, "size": len(data), "md5": hashlib.md5(data).hexdigest(),
            "exec": bool(exe) if exe is not None else name.endswith(".sh"), "added": datetime.now().isoformat(timespec="seconds")}


def pkg_file(body):
    """Aggiunge (contenuto base64 dal browser) o toglie un file di un pacchetto."""
    pid, name = body["id"], os.path.basename(body.get("name") or "")
    if not _PKG_FILE_RX.fullmatch(name):
        raise ValueError("nome di file non valido (lettere, numeri, . _ - +)")
    with LOCK:
        pk = packages()
        p = next((x for x in pk if x["id"] == pid), None)
        if not p:
            raise KeyError("pacchetto non trovato")
        p["files"] = [f for f in p.get("files", []) if f["name"] != name]
        if body.get("remove"):
            (DIST_FILES / pid / name).unlink(missing_ok=True)
        else:
            data = base64.b64decode(body.get("data") or "")
            if name.endswith((".sh", ".conf")) and b"\r\n" in data:
                data = data.replace(b"\r\n", b"\n")    # file salvati su Windows: sulla colonnina servono a capo Unix
            p["files"].append(_store_file(pid, name, data))
        save(F_PKGS, pk)
        return p


def pkg_export(pid):
    p = _pkg(pid)
    out = {**p, "files": [{**f, "data": base64.b64encode((DIST_FILES / pid / f["name"]).read_bytes()).decode()}
                          for f in p.get("files", []) if (DIST_FILES / pid / f["name"]).exists()]}
    out.pop("id", None)
    return out


def ssh_raw(st, cmd, timeout=60, max_bytes=MAX_FETCH):
    """Come ssh_exec ma restituisce lo stdout in byte (file binari) e non tronca fino a max_bytes."""
    for attempt in (0, 1):
        try:
            c = POOL.get(st)
            chan = c.get_transport().open_session(timeout=10)
            chan.settimeout(1.0)
            chan.exec_command(cmd)
            out, err, deadline = bytearray(), bytearray(), time.time() + timeout
            while True:
                if chan.recv_ready():
                    out += chan.recv(262144)
                    deadline = max(deadline, time.time() + 30)      # finché arrivano dati non scade
                elif chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                elif chan.exit_status_ready():
                    break
                elif time.time() > deadline:
                    chan.close()
                    raise CommandTimeout(f"trasferimento non terminato entro {timeout} s", "", err.decode("utf-8", "replace"))
                else:
                    time.sleep(0.01)
                if len(out) > max_bytes:
                    chan.close()
                    raise OSError(f"file più grande di {max_bytes // 1_000_000} MB")
            while chan.recv_ready():
                out += chan.recv(262144)
            code = chan.recv_exit_status()
            chan.close()
            return code, bytes(out), err.decode("utf-8", "replace")
        except TimeoutError:
            raise
        except (paramiko.SSHException, OSError, EOFError) as e:
            POOL.drop(st["id"])
            if attempt or isinstance(e, ConnectionError) and "nuovo tentativo" in str(e):
                raise


def _md5_remote(st, path):
    code, out, _ = ssh_exec(st, f"md5sum -- {shlex.quote(path)} 2>/dev/null || wc -c < {shlex.quote(path)}", timeout=60)
    v = out.strip().split()
    return (v[0] if v else ""), code


def _put(st, local, remote, exe, log):
    data = local.read_bytes()
    q, tmp = shlex.quote(remote), shlex.quote(remote + ".tmp-poledesk")
    sent = False
    sftp = _sftp(st)
    if sftp:
        try:
            with sftp.open(remote + ".tmp-poledesk", "wb") as f:
                f.set_pipelined(True)
                f.write(data)
            sent = True
        except Exception:
            sent = False
        finally:
            sftp.close()
    if not sent:
        code, _, err = ssh_exec(st, f"cat > {tmp}", timeout=max(60, len(data) // 20000), stdin_data=data)
        if code:
            raise OSError(err.strip() or f"scrittura di {remote} non riuscita")
    code, _, err = ssh_exec(st, f"mv -f -- {tmp} {q}" + (f" && chmod +x -- {q}" if exe else ""))
    if code:
        raise OSError(err.strip() or f"spostamento di {remote} non riuscito")
    h, _ = _md5_remote(st, remote)
    want = hashlib.md5(data).hexdigest()
    if h == want:
        log(f"inviato {remote} ({len(data)} byte), verificato md5")
    elif h == str(len(data)):
        log(f"inviato {remote} ({len(data)} byte), verificata la dimensione (md5sum assente)")
    else:
        raise OSError(f"{remote}: verifica non riuscita (atteso {want}, letto {h or 'niente'})")


def _get(st, remote, local):
    data = None
    sftp = _sftp(st)
    if sftp:
        try:
            buf = io.BytesIO()
            sftp.getfo(remote, buf)
            data = buf.getvalue()
        except Exception:
            data = None
        finally:
            sftp.close()
    if data is None:
        code, data, err = ssh_raw(st, f"cat -- {shlex.quote(remote)}", timeout=120)
        if code:
            raise OSError(err.strip() or f"lettura di {remote} non riuscita")
    h, _ = _md5_remote(st, remote)
    if h and h != hashlib.md5(data).hexdigest() and h != str(len(data)):
        raise OSError(f"{remote}: il file scaricato non corrisponde a quello sulla colonnina")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(data)
    return len(data)


def _import_tests(st, path, log):
    """Importa in Profilo termico i CSV delle prove contenuti in un CSV o in un archivio .tar.gz scaricato."""
    texts = []
    if path.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path, "r:gz") as tf:
            for m in tf.getmembers():
                if m.isfile() and m.name.endswith(".csv") and not os.path.basename(m.name).startswith(("risultati", "baseline", "controlli", "diagnosi")) \
                        and "_continua_" not in m.name:
                    texts.append((os.path.basename(m.name), tf.extractfile(m).read().decode("utf-8", "replace")))
    elif path.suffix == ".csv":
        texts.append((path.name, path.read_text(encoding="utf-8", errors="replace")))
    n = 0
    for name, text in texts:
        try:
            profile_import({"name": name, "content": text, "info": {"station": st.get("name", "")}})
            n += 1
        except Exception as e:
            if "già importata" not in str(e) and "non è il CSV" not in str(e):
                log(f"  {name}: non importato ({e})")
    if texts:
        log(f"importate in Profilo termico {n} prove su {len(texts)} file CSV")
    return n


def _dist_one(rid, st):
    run, ctrl = DISTRUNS[rid], DISTCTRL[rid]
    r = run["stations"][st["id"]]
    r.update(state="in corso", started=time.time())

    def log(msg):
        with LOCK:
            r["log"].append(f"{datetime.now():%H:%M:%S} {msg}")

    eid = (run.get("exec") or {}).get(st["id"])
    if eid:
        log(f"esecuzione {eid}")
    p = run["package"]
    vals = {"dir": p["dir"], **{k: v for k, v in run["params"].items() if v != ""}, **(run.get("per_station") or {}).get(st["id"], {})}
    try:
        for i, s in enumerate(p["steps"], 1):
            if ctrl["stop"].is_set():
                raise Aborted("fermata dall'operatore")
            r["step"] = f"{i}/{len(p['steps'])} {s['type']}"
            r["cur"] = i
            _ex_cmd(eid, rid, i, fase="inviato")
            if s["type"] == "invia":
                ssh_exec(st, f"mkdir -p -- {shlex.quote(p['dir'])}")
                files = [f for f in p["files"] if not s.get("only") or f["name"] in s["only"]]
                if not files:
                    log("nessun file da inviare nel pacchetto")
                for f in files:
                    _put(st, DIST_FILES / run["package_id"] / f["name"], f"{p['dir']}/{f['name']}", f.get("exec"), log)
                audit(st, "distribuzione: invio", f"{p['name']}: {', '.join(f['name'] for f in files)}", True)
                _ex_cmd(eid, rid, i, fase="verificato", esito="inviati: " + ", ".join(f["name"] for f in files), exit=0)
            elif s["type"] == "comando":
                try:
                    cmd = render(s["command"], {**st, "vars": {**(st.get("vars") or {}), **vals}})
                except ValueError as e:
                    raise ValueError(f"{e} (parametro vuoto e variabile non definita nell'anagrafica della colonnina)") from e
                log(f"$ {cmd}")
                _ex_cmd(eid, rid, i, comando=cmd)
                try:
                    code, out, err = ssh_exec(st, cmd, timeout=s.get("timeout") or 60)
                except CommandTimeout as e:
                    code, out, err = None, e.out, (e.err + "\n" if e.err else "") + describe_error(e)
                for line in (out + ("\n" + err if err.strip() else "")).strip().splitlines()[-200:]:
                    log("  " + line)
                ok = code == 0 and (not s.get("expect") or re.search(s["expect"], out + err) is not None)
                audit(st, "distribuzione: comando", cmd, ok, f"exit {code}" + (f" · esecuzione {eid}" if eid else ""))
                _ex_cmd(eid, rid, i, fase="ricevuto", exit=code, uscita=(out + ("\n" + err if err.strip() else "")).strip().splitlines()[-8:])
                _ex_cmd(eid, rid, i, fase=("verificato" if s.get("expect") else "ricevuto") if ok else "fallito", continua=bool(s.get("continue")),
                        esito=("trovato «" + s["expect"] + "»" if s.get("expect") else "exit 0") if ok else
                        (f"exit {code}" if code != 0 else f"nell'uscita manca «{s['expect']}»") + (": comando partito ma risultato non confermato" if code == 0 else ""))
                r["last"] = (out.strip().splitlines() or err.strip().splitlines() or [""])[-1][:200]
                if not ok:
                    why = f"exit {code}" if code != 0 else f"nell'uscita manca «{s['expect']}»"
                    if s.get("continue"):
                        log(f"passo {i} non riuscito ({why}), si continua")
                    else:
                        raise RuntimeError(f"passo {i} non riuscito ({why})")
            elif s["type"] == "scarica":
                pat = s["pattern"] if s["pattern"].startswith("/") else f"{p['dir']}/{s['pattern']}"
                lst = f"ls -1t {pat} 2>/dev/null | head -n 1" if s.get("newest") else f"for f in {pat}; do [ -f \"$f\" ] && echo \"$f\"; done"
                _, out, _ = ssh_exec(st, lst)
                remote = [x for x in out.splitlines() if x.strip()]
                if not remote:
                    raise RuntimeError(f"nessun file corrisponde a {pat}")
                dest = DIST_OUT / rid / _safe_name(st.get("name") or st["id"])
                for rf in remote:
                    loc = dest / os.path.basename(rf)
                    n = _get(st, rf, loc)
                    log(f"scaricato {rf} ({n} byte)")
                    with LOCK:
                        r["files"].append(str(loc.relative_to(DIST_OUT / rid)).replace("\\", "/"))
                    if s.get("import"):
                        r["imported"] += _import_tests(st, loc, log)
                    if s.get("delete"):
                        ssh_exec(st, f"rm -f -- {shlex.quote(rf)}")
                        log(f"cancellato dalla colonnina {rf}")
                audit(st, "distribuzione: download", f"{p['name']}: {len(remote)} file", True)
            elif s["type"] == "attendi":
                log(f"attesa {s['seconds']} s")
                if ctrl["stop"].wait(s["seconds"]):
                    raise Aborted("fermata dall'operatore")
            if s["type"] in ("scarica", "attendi"):
                _ex_cmd(eid, rid, i, fase="verificato", esito="fatto")
        r.update(state="ok")
    except Aborted as e:
        r.update(state="fermata", error=str(e))
        log(str(e))
    except Exception as e:
        r.update(state="errore", error=describe_error(e))
        log("ERRORE: " + describe_error(e))
        if eid and r.get("cur"):
            with _EX_LOCK:
                ex = ex_load(eid)
                c = next((x for x in (ex or {}).get("comandi", []) if x["run"] == rid and x["n"] == r["cur"]), None)
            if c and c.get("stato") in ("inviato", "preparato", "ricevuto"):
                _ex_cmd(eid, rid, r["cur"], fase="fallito", esito=describe_error(e))
    r["ended"] = time.time()
    with LOCK:
        last = all(x["state"] not in ("in coda", "in corso") for x in run["stations"].values()) and not run.get("closing")
        if last:
            run["closing"] = True
    if last and run.get("kind", "").startswith("sc-"):
        try:
            sc_after_run(run)
        except Exception as e:
            run["camp_error"] = describe_error(e)
    with LOCK:
        if last:
            run["done"], run["ended"] = True, time.time()
        save(DIST_RUNS / f"{rid}.json", run)


def dist_start(body):
    return dist_launch(_pkg(body.get("package_id", "")), body)


def dist_launch(p, body, kind="", per_station=None, exec_map=None):
    sts = [s for s in stations() if s["id"] in (body.get("station_ids") or [])]
    if not sts:
        raise ValueError("nessuna colonnina selezionata")
    if not p["steps"]:
        raise ValueError("il pacchetto non ha passi")
    if any(s["type"] == "invia" for s in p["steps"]):
        miss = [f["name"] for f in p["files"] if not (DIST_FILES / p["id"] / f["name"]).exists()]
        if miss or not p["files"]:
            raise ValueError("file del pacchetto mancanti: " + (", ".join(miss) if miss else "aggiungine almeno uno"))
    params = {x["name"]: str((body.get("params") or {}).get(x["name"], x.get("default", ""))) for x in p.get("params", [])}
    rid = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
    par = max(1, min(int(body.get("parallel") or 5), int(settings()["max_parallel"]), 50))
    run = {"id": rid, "package_id": p["id"], "package": p, "params": params, "parallel": par, "kind": kind,
           "per_station": per_station or {}, "started": time.time(), "done": False, "exec": exec_map or {},
           "stations": {s["id"]: {"name": s.get("name", ""), "host": s.get("host", ""), "state": "in coda", "step": "", "last": "",
                                  "error": "", "log": [], "files": [], "imported": 0} for s in sts}}
    DISTRUNS[rid], DISTCTRL[rid] = run, {"stop": threading.Event()}
    if run["exec"]:
        _ex_track_prepare(run)
    for old in sorted(DISTRUNS)[:-20]:
        if DISTRUNS[old]["done"]:
            DISTRUNS.pop(old, None)
            DISTCTRL.pop(old, None)
    pool = ThreadPoolExecutor(max_workers=par)
    for s in sts:
        pool.submit(_dist_one, rid, s)
    pool.shutdown(wait=False)
    audit(None, "distribuzione", f"avvio {p['name']} su {len(sts)} colonnine", True, json.dumps(params, ensure_ascii=False)[:250])
    return rid


def dist_run(rid):
    run = DISTRUNS.get(rid) or load(DIST_RUNS / f"{rid}.json", None)
    if not run:
        raise KeyError("esecuzione non trovata")
    with LOCK:
        return json.loads(json.dumps(run))


def dist_runs():
    out = []
    for f in sorted(DIST_RUNS.glob("*.json"), reverse=True)[:40]:
        r = DISTRUNS.get(f.stem) or load(f, {})
        if r:
            sts = r.get("stations", {}).values()
            out.append({"id": r["id"], "package": r.get("package", {}).get("name", ""), "started": r.get("started"), "done": r.get("done"),
                        "n": len(sts), "ok": sum(x["state"] == "ok" for x in sts), "err": sum(x["state"] == "errore" for x in sts),
                        "files": sum(len(x.get("files", [])) for x in sts)})
    live = [r for k, r in DISTRUNS.items() if not (DIST_RUNS / f"{k}.json").exists()]
    for r in live:
        sts = r["stations"].values()
        out.insert(0, {"id": r["id"], "package": r["package"]["name"], "started": r["started"], "done": r["done"], "n": len(sts),
                       "ok": sum(x["state"] == "ok" for x in sts), "err": sum(x["state"] == "errore" for x in sts),
                       "files": sum(len(x.get("files", [])) for x in sts)})
    return out


def dist_report(rid):
    run = dist_run(rid)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["colonnina", "host", "esito", "errore", "ultimo_output", "file_scaricati", "prove_importate", "durata_s", "registro"])
    for s in run["stations"].values():
        w.writerow([s["name"], s["host"], s["state"], s.get("error", ""), s.get("last", ""), " ".join(s.get("files", [])), s.get("imported", 0),
                    int((s.get("ended") or time.time()) - (s.get("started") or time.time())) if s.get("started") else "",
                    " | ".join(s.get("log", []))[:30000]])
    return buf.getvalue()


def dist_zip(rid):
    if not re.fullmatch(r"[\w-]+", rid):
        raise KeyError("esecuzione non trovata")
    d = DIST_OUT / rid
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"rapporto_{rid}.csv", dist_report(rid).encode("utf-8-sig"))
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(d))
    return buf.getvalue()


def dist_open(rid):
    d = DIST_OUT / rid if rid else DIST_OUT
    d.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "startfile"):
        os.startfile(str(d))
    return {"path": str(d)}



# ---------------------------------------------------------------- schermo: test di laboratorio e prove massive
# Dati in data/schermo: soglie.json (soglie e validità, si modifica anche a mano), sequenze/seq_*.conf,
# laboratorio.json e massive.json (impostazioni delle due pagine), campo.json (lista delle colonnine di campo,
# stato verificato), prove.json (prove valutate), diagnosi.json, stato.json, meteo.json (cache di Text), dati/ (CSV).

SC_DIR = DATA / "schermo"
SC_SEQ, SC_DATI = SC_DIR / "sequenze", SC_DIR / "dati"
for _d in (SC_DIR, SC_SEQ, SC_DATI):
    _d.mkdir(parents=True, exist_ok=True)
F_SC_SOGLIE, F_SC_LAB, F_SC_MASS = SC_DIR / "soglie.json", SC_DIR / "laboratorio.json", SC_DIR / "massive.json"
F_SC_CAMPO, F_SC_PROVE, F_SC_DIAG = SC_DIR / "campo.json", SC_DIR / "prove.json", SC_DIR / "diagnosi.json"
F_SC_STATO, F_SC_METEO = SC_DIR / "stato.json", SC_DIR / "meteo.json"

# Valori iniziali: alla prima apertura vengono scritti in soglie.json; da lì in poi vale il file.
SC_SOGLIE_INIZIALI = {
    "_nota": "Soglie provvisorie. Si modificano da PoleDesk (Prova → Avanzate → Soglie e validità) o qui a mano; valgono dopo Rivaluta.",
    "validita": {"led_max_A": 1.0, "led_var_max_A": 0.1, "user_ok": "IDLE", "deriva_max": 0.05, "C_max": 0.35, "tolleranza_s": 15,
                 "notte": "aperto", "notte_dalle": "19:00", "notte_alle": "06:00", "base_tol": 0.3, "base_min": 30},
    "diagnostica": {"minuti": 2, "ping_ko_min_pct": 80, "aux_fault_min_pct": 50, "aux_fermi": True},
    "regole": {
        "T2": {"I_min": 1.9, "IA_min": 0.55, "tau_max": 190, "R_min": 4, "rimbalzo_max": 105, "In_min": 0.25, "Q_min": 0.5, "frast_min": 0.025},
    },
    # esito: attaccato con 0 condizioni vere, sospetto da sospetto_min a staccato_min − 1, staccato da staccato_min in su
    # o con «sospetto» in conferma_notti notti diverse; incerto se un indicatore è entro ±margine_pct% della soglia e cambierebbe l'esito
    "esito": {"staccato_min_condizioni": 3, "sospetto_min": 1, "min_calcolabili": 3, "conferma_notti": 2, "margine_pct": 10},
    # metodo B: Ī rispetto al riferimento I₀ della colonnina (mediana delle tarature valide della stessa sequenza)
    "metodo_B": {"rif_min": 3, "q_attaccato": 1.3, "q_staccato": 1.8},
    "frastagliatura": {"finestra_s": 120},
    "meteo": {"attivo": True},
}
SC_COND = [  # chiave della soglia, indicatore, verso, etichetta
    ("I_min", "I", ">=", "Ī ≥ {} °C/min"), ("IA_min", "IA", ">=", "Ī/A ≥ {}"), ("tau_max", "tau", "<", "τ < {} s"),
    ("R_min", "R", ">=", "R ≥ {}"), ("rimbalzo_max", "rimbalzo", "<", "rimbalzo < {} %"),
    ("In_min", "In", ">=", "In ≥ {}"), ("Q_min", "Q", ">=", "Q ≥ {}")]

SC_SEQ_INIZIALI = {
    "T2": ("T2 (46 min), sequenza delle prove massive: soglie di decisione tarate su questa.",
           [("Riferimento", "rif", 300, 0, 0), ("Carica 1", "carica", 300, 1, 0), ("Misura 1", "misura", 480, 0, 1),
            ("Recupero", "recupero", 900, 0, 0), ("Carica 2", "carica", 300, 1, 0), ("Misura 2", "misura", 480, 0, 1)]),
    "T2L": ("T2L (61 min), carica e recupero più lunghi: indicatori calcolati e salvati, soglie da ritarare.",
            [("Riferimento", "rif", 300, 0, 0), ("Carica 1", "carica", 600, 1, 0), ("Misura 1", "misura", 480, 0, 1),
             ("Recupero", "recupero", 1200, 0, 0), ("Carica 2", "carica", 600, 1, 0), ("Misura 2", "misura", 480, 0, 1)]),
}
SC_RUOLI = {"rif": (0, 0), "carica": (1, 0), "misura": (0, 1), "ventola": (0, 1), "recupero": (0, 0)}

# condizioni di partenza: inviate alla colonnina in coda a ogni seq_*.conf (lo script le legge da lì)
PART_DEFAULT = {"stable_tol": 0.3, "pre_wait_min": 60, "min_uptime_h": 3, "user_ok": "IDLE", "min_free_mb": 5}
# quando: "subito" (una volta, adesso) · "una" (una volta all'ora indicata) · "ogni" (ogni notte all'ora indicata per N notti)
CONT_DEFAULT = {"ogni_ore": 4, "volte": 6, "ore_dalle": False, "continua": False, "continua_s": 180, "continua_ore": 24,
                "monitor": True, "monitor_timeout": 30}
LAB_DEFAULT = {"quando": "subito", "ora": "22:00", "giorni": 3, "sequenze": ["T2"], "ruota": False, "parti_sempre": False,
               "pausa_min": 60, "base_min": 30, "ridotti": False, "scala": 0.05, "nome": "lab", "parallelo": 5, "dir": "/home/root/test",
               "monitor_s": 30}
MASS_DEFAULT = {"quando": "una", "ora": "00:30", "giorni": 1, "sequenze": ["T2", "T2L"], "ruota": False, "parti_sempre": False,
                "pausa_min": 60, "base_min": 30, "ridotti": False, "scala": 0.05, "nome": "campo", "usa_anagrafica": True,
                "parallelo": 10, "dir": "/home/root/test", "monitor_s": 120}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def sc_soglie():
    if not F_SC_SOGLIE.exists():
        save(F_SC_SOGLIE, SC_SOGLIE_INIZIALI)
    c = load(F_SC_SOGLIE, {})
    if "conferma_notti" not in (c.get("esito") or {}):
        # 1.30: legenda degli esiti (proposta del 23/11): staccato da 3 condizioni, frastagliatura in ventola per T2 (provvisoria)
        c.setdefault("esito", {})["staccato_min_condizioni"] = 3
        if isinstance(c.get("regole"), dict) and isinstance(c["regole"].get("T2"), dict):
            c["regole"]["T2"].setdefault("frast_min", 0.025)
        c["esito"].update({k: v for k, v in SC_SOGLIE_INIZIALI["esito"].items() if k not in c["esito"]})
        save(F_SC_SOGLIE, c)
    out = _merge(SC_SOGLIE_INIZIALI, c)
    out["regole"] = c.get("regole") if isinstance(c.get("regole"), dict) else SC_SOGLIE_INIZIALI["regole"]
    return out


def sc_soglie_save(body):
    c = body.get("soglie") or {}

    def num(v, where):
        try:
            x = float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            raise ValueError(f"valore non numerico in {where}: {v}")
        if not math.isfinite(x):
            raise ValueError(f"valore non valido in {where}")
        return x
    out = sc_soglie()
    for k in ("led_max_A", "led_var_max_A", "deriva_max", "C_max", "tolleranza_s", "base_tol", "base_min"):
        if k in (c.get("validita") or {}):
            out["validita"][k] = num(c["validita"][k], "validità " + k)
    if (c.get("validita") or {}).get("notte") in ("sempre", "aperto", "mai"):
        out["validita"]["notte"] = c["validita"]["notte"]
    for k in ("notte_dalle", "notte_alle"):
        v = str((c.get("validita") or {}).get(k) or "")
        if v:
            if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
                raise ValueError(f"validità {k}: ora HH:MM")
            out["validita"][k] = v
    for k in ("rif_min", "q_attaccato", "q_staccato"):
        if k in (c.get("metodo_B") or {}):
            out.setdefault("metodo_B", {})[k] = num(c["metodo_B"][k], "metodo B " + k)
    if "finestra_s" in (c.get("frastagliatura") or {}):
        out.setdefault("frastagliatura", {})["finestra_s"] = max(20.0, num(c["frastagliatura"]["finestra_s"], "frastagliatura"))
    if "user_ok" in (c.get("validita") or {}):
        out["validita"]["user_ok"] = str(c["validita"]["user_ok"]).strip()
    for k in ("minuti", "ping_ko_min_pct", "aux_fault_min_pct"):
        if k in (c.get("diagnostica") or {}):
            out["diagnostica"][k] = num(c["diagnostica"][k], "diagnostica " + k)
    if "aux_fermi" in (c.get("diagnostica") or {}):
        out["diagnostica"]["aux_fermi"] = bool(c["diagnostica"]["aux_fermi"])
    if isinstance(c.get("regole"), dict):
        reg = {}
        for seq, r in c["regole"].items():
            seq = re.sub(r"[^A-Za-z0-9_-]", "", str(seq))
            if seq:
                reg[seq] = {k: num(v, f"regola {seq} {k}") for k, v in (r or {}).items() if k in dict((x[0], 1) for x in _cond_list()) and v not in ("", None)}
        out["regole"] = reg
    for k in ("staccato_min_condizioni", "min_calcolabili", "sospetto_min", "conferma_notti"):
        if k in (c.get("esito") or {}):
            out["esito"][k] = max(1, int(num(c["esito"][k], "esito " + k)))
    if "margine_pct" in (c.get("esito") or {}):
        out["esito"]["margine_pct"] = max(0.0, num(c["esito"]["margine_pct"], "esito margine"))
    if out["esito"]["sospetto_min"] >= out["esito"]["staccato_min_condizioni"]:
        raise ValueError("esito: «sospetto da» deve essere minore di «staccato da»")
    if "attivo" in (c.get("meteo") or {}):
        out["meteo"]["attivo"] = bool(c["meteo"]["attivo"])
    save(F_SC_SOGLIE, out)
    return out


# ---------------- sequenze (file seq_*.conf, gli stessi che vanno sulla colonnina)

def _seq_text(name, descr, steps, extra=None):
    lines = [f"# seq_{name}.conf — {descr}".rstrip(" —"), "# Passi: nome;ruolo;secondi;riscaldatore;ventola (1 = acceso)", 'STEPS="']
    lines += [f"{n};{r};{int(s)};{int(h)};{int(f)}" for n, r, s, h, f in steps]
    lines += ['"', "# limiti di sicurezza di questa sequenza (il guardiano a tempo si calcola dalla durata dei passi)",
              f"AUX_MAX={(extra or {}).get('AUX_MAX', 60)}", f"TEMP_MAX={(extra or {}).get('TEMP_MAX', 60)}"]
    return "\n".join(lines) + "\n"


def _seq_parse(text):
    descr = ""
    m = re.match(r"#\s*seq_[^ ]*\s*—\s*(.*)", text)
    if m:
        descr = m.group(1).strip()
    st = re.search(r'STEPS="(.*?)"', text, re.S)
    steps = []
    for line in (st.group(1) if st else "").splitlines():
        p = [x.strip() for x in line.split(";")]
        if len(p) >= 5 and p[0] and not p[0].startswith("#"):
            try:
                steps.append({"nome": p[0], "ruolo": p[1], "s": int(float(p[2])), "h": int(float(p[3])), "f": int(float(p[4]))})
            except ValueError:
                pass
    extra = {k: v for k, v in re.findall(r"^(AUX_MAX|TEMP_MAX)=([\d.]+)", text, re.M)}
    return {"descr": descr, "passi": steps, "AUX_MAX": float(extra.get("AUX_MAX", 60)), "TEMP_MAX": float(extra.get("TEMP_MAX", 60))}


def sc_sequenze():
    for n, (d, st) in SC_SEQ_INIZIALI.items():
        f = SC_SEQ / f"seq_{n}.conf"
        cur = _seq_parse(f.read_text(encoding="utf-8", errors="replace")) if f.exists() else None
        if cur is None or not cur["passi"]:
            # sequenza di base mancante o senza passi (file vuoto o rovinato): si ricrea; il file rovinato resta come .vuoto
            if f.exists():
                f.replace(f.with_name(f.name + ".vuoto"))
            src = Path(__file__).resolve().parent / f"seq_{n}.conf"      # file della release accanto a server.py
            blob = src.read_bytes().replace(b"\r\n", b"\n") if src.exists() else b""
            if not _seq_parse(blob.decode("utf-8", "replace"))["passi"]:
                blob = _seq_text(n, d, st).encode()                       # valori della release, se il file accanto è vuoto
            f.write_bytes(blob)
    out = []
    for f in sorted(SC_SEQ.glob("seq_*.conf")):
        x = _seq_parse(f.read_text(encoding="utf-8", errors="replace"))
        out.append({"nome": f.stem[4:], **x, "durata": sum(p["s"] for p in x["passi"])})
    return out


def sc_seq_save(body):
    n = str(body.get("nome", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", n):
        raise ValueError("nome della sequenza: lettere, numeri, - e _ (max 20), es. T2 o T2L")
    steps = []
    for i, p in enumerate(body.get("passi") or [], 1):
        nome, ruolo = str(p.get("nome", "")).replace(";", ",").strip() or f"Passo {i}", str(p.get("ruolo", ""))
        if ruolo not in SC_RUOLI:
            raise ValueError(f"passo {i}: ruolo non valido ({ruolo})")
        s = int(float(p.get("s") or 0))
        if s < 10:
            raise ValueError(f"passo {i}: almeno 10 secondi")
        steps.append((nome, ruolo, s, 1 if p.get("h") in (1, "1", True) else 0, 1 if p.get("f") in (1, "1", True) else 0))
    if not steps:
        raise ValueError("la sequenza non ha passi")
    roles = [s[1] for s in steps]
    if roles[0] != "rif":
        raise ValueError("il primo passo deve essere il Riferimento (rif): serve a misurare la deriva della base")
    extra = {"AUX_MAX": float(body.get("AUX_MAX") or 60), "TEMP_MAX": float(body.get("TEMP_MAX") or 60)}
    (SC_SEQ / f"seq_{n}.conf").write_text(_seq_text(n, str(body.get("descr", "")).replace("\n", " ").strip(), steps, extra), encoding="utf-8", newline="\n")
    return sc_sequenze()


def sc_seq_import(body):
    """Importa uno o più file seq_*.conf (testo) come sequenze di PoleDesk."""
    out = []
    for f in body.get("files") or []:
        name = os.path.basename(str(f.get("name") or ""))
        m = re.fullmatch(r"(?:seq_)?([A-Za-z0-9_-]{1,20})\.conf", name)
        if not m:
            raise ValueError(f"{name}: il nome deve essere seq_NOME.conf (NOME: lettere, numeri, - e _)")
        x = _seq_parse(str(f.get("text") or "").replace("\r\n", "\n"))
        if not x["passi"]:
            raise ValueError(f"{name}: nessun passo trovato (serve STEPS=\"nome;ruolo;secondi;scaldiglia;ventola …\")")
        sc_seq_save({"nome": m.group(1), "descr": x["descr"], "passi": x["passi"], "AUX_MAX": x["AUX_MAX"], "TEMP_MAX": x["TEMP_MAX"]})
        out.append(m.group(1))
    return {"sequenze": sc_sequenze(), "importate": out}


def sc_seq_delete(body):
    n = str(body.get("nome", ""))
    if n in SC_SEQ_INIZIALI:
        raise ValueError(f"{n} è una sequenza di base: si modifica ma non si cancella")
    (SC_SEQ / f"seq_{_safe_name(n)}.conf").unlink(missing_ok=True)
    return sc_sequenze()


def _seq_def(name):
    f = SC_SEQ / f"seq_{_safe_name(name)}.conf"
    return _seq_parse(f.read_text(encoding="utf-8", errors="replace")) if f.exists() else None


# ---------------- impostazioni delle due pagine, lista di campo

def sc_cfg(kind):
    f, d = (F_SC_LAB, LAB_DEFAULT) if kind == "lab" else (F_SC_MASS, MASS_DEFAULT)
    c = {**CONT_DEFAULT, **d, **load(f, {})}
    c["partenza"] = {**PART_DEFAULT, **(c.get("partenza") or {})}
    return c


def sc_cfg_save(kind, body):
    c = sc_cfg(kind)
    d = {**CONT_DEFAULT, **(LAB_DEFAULT if kind == "lab" else MASS_DEFAULT)}
    for k in d:
        if k in body:
            c[k] = body[k]
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(c["ora"])):
        raise ValueError("ora non valida (HH:MM)")
    known = {s["nome"] for s in sc_sequenze()}
    seqs = [s for s in c["sequenze"] if s]
    miss = [s for s in seqs if s not in known]
    if miss:
        raise ValueError("sequenze inesistenti: " + ", ".join(miss))
    if not seqs:
        raise ValueError("scegli almeno una sequenza")
    if len(seqs) > 5:
        raise ValueError("al massimo 5 sequenze per notte")
    c["sequenze"] = seqs
    if c.get("quando") not in ("subito", "una", "ogni", "ore"):
        c["quando"] = "subito"
    for k in ("ruota", "parti_sempre", "ridotti", "ore_dalle", "continua", "monitor"):
        c[k] = bool(c.get(k))
    try:
        c["scala"] = min(1.0, max(0.005, float(str(c.get("scala") or 0.05).replace(",", "."))))
    except ValueError:
        c["scala"] = 0.05
    try:
        c["ogni_ore"] = round(float(str(c.get("ogni_ore") or 4).replace(",", ".")), 2)
    except ValueError:
        raise ValueError("«ripeti ogni» vuole un numero di ore")
    c["volte"] = max(1, min(200, int(float(c.get("volte") or 1))))
    c["continua_s"] = max(5, min(86400, int(float(c.get("continua_s") or 180))))
    try:
        c["monitor_s"] = max(10, min(3600, int(float(str(c.get("monitor_s") or 30).replace(",", ".")))))
    except ValueError:
        raise ValueError("monitor: intervallo in secondi (10–3600)")
    c["monitor_timeout"] = max(10, min(120, int(float(c.get("monitor_timeout") or 30))))
    c["continua_ore"] = max(1, min(24 * 31, int(float(c.get("continua_ore") or 24))))
    for k in ("pausa_min", "base_min", "giorni", "parallelo"):
        if k in c:
            c[k] = max(0, int(float(c[k] or 0)))
    c["giorni"] = min(30, max(1, c["giorni"]))
    for k in ("notti", "monitora", "monitora_min", "test_scala", "salta_diagnosi"):      # campi di versioni precedenti
        c.pop(k, None)
    c["parallelo"] = max(1, c["parallelo"])
    pa = {**PART_DEFAULT, **(body.get("partenza") or c.get("partenza") or {})}

    def pn(k, lo, hi, name):
        try:
            x = float(str(pa[k]).replace(",", "."))
        except (TypeError, ValueError):
            raise ValueError(f"condizioni di partenza: {name} non è un numero")
        if not lo <= x <= hi:
            raise ValueError(f"condizioni di partenza: {name} fuori dall'intervallo {lo}–{hi}")
        return x
    pa = {"stable_tol": pn("stable_tol", 0.05, 5, "tolleranza della base"), "pre_wait_min": int(pn("pre_wait_min", 1, 240, "attesa massima")),
          "min_uptime_h": pn("min_uptime_h", 0, 72, "ore di accensione"), "user_ok": re.sub(r"[^A-Za-z0-9_ -]", "", str(pa.get("user_ok") or "")).strip(),
          "min_free_mb": pn("min_free_mb", 1, 1000, "spazio libero")}
    if c["quando"] == "ore":
        if c["ogni_ore"] < 0.25:
            raise ValueError("«ripeti ogni» almeno 0,25 ore (15 minuti)")
        k = c["scala"] if c["ridotti"] else 1
        dur = sum(x["durata"] for x in sc_sequenze() if x["nome"] in c["sequenze"]) * k + c["pausa_min"] * 60 * (len(c["sequenze"]) - 1)
        if not c["parti_sempre"]:
            dur += c["base_min"] * 60 * len(c["sequenze"])
        if c["ogni_ore"] * 3600 < dur:
            raise ValueError(f"«ripeti ogni {c['ogni_ore']:g} ore» è più corto di un giro di prove (circa {round(dur / 60)} min): "
                             "l'avvio successivo arriverebbe con la prova precedente ancora in corso")
    if not c["parti_sempre"] and c["base_min"] and c["base_min"] >= pa["pre_wait_min"]:
        raise ValueError(f"l'attesa massima della base ({pa['pre_wait_min']} min) deve superare l'attesa base stabile ({c['base_min']} min), "
                         "altrimenti la finestra non fa in tempo a riempirsi: metti almeno " + str(c["base_min"] + 1) + " min, meglio il doppio")
    c["partenza"] = pa
    c["nome"] = re.sub(r"[^A-Za-z0-9_-]", "-", str(c.get("nome") or "prova"))
    save(F_SC_LAB if kind == "lab" else F_SC_MASS, c)
    return c


def lab_calendario(c):
    """Sequenze notte per notte (modo «ogni notte» con «ruota l'ordine»): l'ordine scorre di una posizione a ogni notte."""
    s, out = list(c["sequenze"]), []
    for k in range(int(c.get("giorni") or 1)):
        r = k % len(s) if c.get("ruota") and len(s) > 1 else 0
        out.append(s[r:] + s[:r])
    return out


def sc_campo():
    return load(F_SC_CAMPO, {"colonnine": [], "id_cuid": {}})


def _st_serial(st, idc=None):
    v = st.get("vars") or {}
    return v.get("seriale") or v.get("cuid") or (idc or {}).get(st["id"]) or ""


def _match_station(serial, sts, idc):
    s = serial.strip().lower()
    for st in sts:
        if s and s in (str(_st_serial(st, idc)).lower(), str(st.get("name", "")).lower()):
            return st
    for st in sts:
        if s and len(s) >= 6 and s in str(st.get("name", "")).lower():
            return st
    return None


def sc_campo_set(body):
    """Lista di campo: seriali incollati e/o righe dell'anagrafica (seriale; indirizzo; latitudine; longitudine)."""
    cp = sc_campo()
    old = {x["seriale"]: x for x in cp["colonnine"]}
    rows = []
    text = str(body.get("testo") or "")
    lines = [l for l in text.replace("\r", "").split("\n") if l.strip()]
    if lines:
        sep = ";" if lines[0].count(";") >= lines[0].count(",") and ";" in lines[0] else ("," if "," in lines[0] and not re.search(r"\d,\d", lines[0]) else ("\t" if "\t" in lines[0] else None))
        head = [h.strip().lower() for h in lines[0].split(sep)] if sep else []
        has_head = sep and any(h in ("seriale", "cuid", "serial", "indirizzo", "latitudine", "lat") for h in head)
        idx = lambda *names: next((head.index(n) for n in names if n in head), None)
        iS, iA, iLa, iLo = (idx("seriale", "cuid", "serial"), idx("indirizzo", "luogo", "address"), idx("latitudine", "lat"), idx("longitudine", "lon", "lng")) if has_head else (0, 1, 2, 3)
        for l in lines[1:] if has_head else lines:
            f = [x.strip() for x in l.split(sep)] if sep else l.split()
            if not sep and len(f) > 1:          # più seriali sulla stessa riga separati da spazi
                rows += [{"seriale": x} for x in f]
                continue
            g = lambda i: f[i] if i is not None and i < len(f) else ""
            if g(iS):
                rows.append({"seriale": g(iS), "indirizzo": g(iA), "lat": _flt(g(iLa)), "lon": _flt(g(iLo))})
    if body.get("sostituisci"):
        old_keep, cp["colonnine"] = old, []
    else:
        old_keep = old
    have = {x["seriale"] for x in cp["colonnine"]}
    for r in rows:
        s = re.sub(r"\s+", "", r["seriale"])
        o = old_keep.get(s, {})
        x = {"seriale": s, "indirizzo": r.get("indirizzo") or o.get("indirizzo", ""), "lat": r.get("lat") if r.get("lat") is not None else o.get("lat"),
             "lon": r.get("lon") if r.get("lon") is not None else o.get("lon"), "verificato": o.get("verificato") or {}}
        if s in have:
            cp["colonnine"] = [x if y["seriale"] == s else y for y in cp["colonnine"]]
        else:
            cp["colonnine"].append(x)
            have.add(s)
    for s in body.get("togli") or []:
        cp["colonnine"] = [y for y in cp["colonnine"] if y["seriale"] != s]
    save(F_SC_CAMPO, cp)
    return sc_mass_view()


def sc_link(body):
    """Collega a mano un seriale della lista a una colonnina dell'anagrafica (o aggiunge alla lista le colonnine scelte)."""
    cp = sc_campo()
    cp.setdefault("link", {})
    have = {x["seriale"] for x in cp["colonnine"]}
    for it in body.get("items") or []:
        s, sid = re.sub(r"\s+", "", str(it.get("seriale", ""))), str(it.get("station_id", ""))
        if not s:
            continue
        if sid:
            station_by_id(sid)
            cp["link"][s] = sid
        else:
            cp["link"].pop(s, None)
        if s not in have:
            cp["colonnine"].append({"seriale": s, "indirizzo": "", "lat": None, "lon": None, "verificato": {}})
            have.add(s)
    save(F_SC_CAMPO, cp)
    return sc_mass_view()


def sc_verificato(body):
    cp = sc_campo()
    s = body.get("seriale", "")
    for x in cp["colonnine"]:
        if x["seriale"] == s:
            stato = body.get("stato", "")
            if stato not in ("", "attaccato", "staccato", "colla che cede", "ignoto"):
                raise ValueError("stato verificato non valido")
            x["verificato"] = {"stato": stato, "data": str(body.get("data", ""))[:10], "fonte": str(body.get("fonte", ""))[:80]}
            save(F_SC_CAMPO, cp)
            return x
    raise KeyError("seriale non in lista")


def _coords(cuid, st=None):
    for x in sc_campo()["colonnine"]:
        if x["seriale"] == cuid and x.get("lat") is not None and x.get("lon") is not None:
            return x["lat"], x["lon"]
    v = (st or {}).get("vars") or {}
    la, lo = _flt(v.get("lat", v.get("latitudine"))), _flt(v.get("lon", v.get("longitudine")))
    return (la, lo) if la is not None and lo is not None else (None, None)


# ---------------- temperatura esterna (Open-Meteo, archivio delle previsioni)

_METEO_KO = {}


def sc_text(lat, lon, t):
    """Temperatura dell'aria all'istante t (epoch), interpolata tra i valori orari; None se non disponibile."""
    if lat is None or lon is None or not t:
        return None, None
    day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
    key = f"{lat:.3f},{lon:.3f},{day}"
    cache = load(F_SC_METEO, {})
    ser = cache.get(key)
    if ser is None and time.time() - _METEO_KO.get(key, 0) < 600:     # appena fallito: niente nuovi tentativi per 10 min
        return None, None
    if ser is None:
        d0 = datetime.fromtimestamp(t - 86400, timezone.utc).strftime("%Y-%m-%d")
        d1 = datetime.fromtimestamp(t + 86400, timezone.utc).strftime("%Y-%m-%d")
        url = ("https://historical-forecast-api.open-meteo.com/v1/forecast"
               f"?latitude={lat:.4f}&longitude={lon:.4f}&start_date={d0}&end_date={d1}"
               "&hourly=temperature_2m,wind_speed_10m&timezone=GMT&format=csv")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                text = r.read().decode("utf-8", "replace")
        except Exception:
            _METEO_KO[key] = time.time()
            return None, None
        ser, on = [], False
        for line in text.splitlines():
            p = line.split(",")
            if p and p[0] == "time":
                on = True
                continue
            if on and len(p) >= 2 and p[1] != "":
                try:
                    ts = datetime.strptime(p[0], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp()
                    ser.append([ts, float(p[1]), _flt(p[2]) if len(p) > 2 else None])
                except ValueError:
                    pass
        if not ser:
            return None, None
        with LOCK:
            cache = load(F_SC_METEO, {})
            cache[key] = ser
            if len(cache) > 3000:
                cache = dict(list(cache.items())[-2000:])
            save(F_SC_METEO, cache)
    for a, b in zip(ser, ser[1:]):
        if a[0] <= t <= b[0]:
            k = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0
            w = None if a[2] is None or b[2] is None else a[2] + k * (b[2] - a[2])
            return a[1] + k * (b[1] - a[1]), w
    return None, None


# ---------------- indicatori di una prova (dal CSV di prova_schermo.sh)

def _mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def _frast(pts, win=120):
    """Frastagliatura: deviazione standard di (AUX − media mobile centrata di win secondi). Solo i campioni con la
    finestra intera dentro il tratto (niente effetti di bordo). None se il tratto è troppo corto."""
    pts = [(t, a) for t, a in pts if a is not None]
    if len(pts) < 8:
        return None
    h = win / 2
    t0, t1 = pts[0][0], pts[-1][0]
    if t1 - t0 < win + 10:
        return None
    pre = [0.0]
    for _, a in pts:
        pre.append(pre[-1] + a)
    res, lo, hi = [], 0, 0
    for i, (t, a) in enumerate(pts):
        if t - h < t0 - 1 or t + h > t1 + 1:
            continue
        while pts[lo][0] < t - h:
            lo += 1
        while hi < len(pts) and pts[hi][0] <= t + h:
            hi += 1
        n = hi - lo
        if n >= 3:
            res.append(a - (pre[hi] - pre[lo]) / n)
    if len(res) < 5:
        return None
    m = sum(res) / len(res)
    return math.sqrt(sum((x - m) ** 2 for x in res) / (len(res) - 1))


def _base_rows(path):
    """Campioni dell'attesa della base stabile (passo 0 del CSV di prova_schermo.sh): (t, aux, led)."""
    out = []
    try:
        with Path(path).open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                if str(r.get("passo", "")).strip() != "0" or str(r.get("ok", "1")).strip() not in ("1", "True", "true"):
                    continue
                try:
                    t = float(r["epoch"]) if r.get("epoch") else datetime.fromisoformat(r["timestamp"]).timestamp()
                except (ValueError, KeyError, TypeError):
                    continue
                out.append((t, _fnum(r.get("aux_temperature")), _fnum(r.get("aux_leds_current"))))
    except OSError:
        pass
    return out


def _fmt(v, n=2):
    if v is None:
        return "–"
    return (f"{v:.0f}" if abs(v) >= 100 and n <= 2 else f"{v:.{n}f}").replace(".", ",")


def sc_eval_csv(path, seq=None, soglie=None, coords=(None, None), t_ext=None):
    """Validità (ogni controllo con valore e ✓/✗), indicatori e frastagliatura di una prova.
    Soglie di validità da soglie.json. Vale per i CSV di prova_schermo.sh e per quelli del Profilo termico."""
    so = soglie or sc_soglie()
    va = so["validita"]
    rows, phase_t, phases, cuid = _load_test_csv(Path(path))
    rows = [r for r in rows if r["ok"] and r["aux"] is not None]
    out = {"cuid": cuid, "valida": False, "motivi": [], "controlli": [], "misure": [], "ind": {}, "Text": None, "vento": None, "frast": {}}

    def chk(nome, valore, regola, ok, motivo=None):
        out["controlli"].append({"nome": nome, "valore": valore, "regola": regola, "ok": ok})
        if ok is False:
            out["motivi"].append(motivo or f"{nome}: {valore} ({regola})")
    if not rows:
        chk("campioni", "nessuno", "almeno una misura", False, "nessun campione")
        return out
    sd = _seq_def(seq) if seq else None
    # durate previste dal file della sequenza, solo se la prova le ha usate davvero (non con i tempi ridotti)
    if sd and len(sd["passi"]) == len(phases) and all(abs(ph["s"] - p["s"]) <= max(20, 0.2 * p["s"]) for ph, p in zip(phases[:-1], sd["passi"][:-1])):
        for ph, p in zip(phases, sd["passi"]):
            ph["s"] = p["s"]
    T = lambda a, b: [r for r in rows if a <= r["t"] < b]

    def near(x):
        r = min(rows, key=lambda r: abs(r["t"] - x))
        return r["aux"] if abs(r["t"] - x) <= 20 else None

    def end_of(i):
        return phase_t.get(i + 1, phase_t.get(i, 0) + phases[i]["s"])
    out["inizio"] = phase_t.get(0)
    out["fasi"] = [{"nome": ph["name"], "ruolo": ph["role"], "s": ph["s"]} for ph in phases]
    # notte: si decide nel motore (dipende dal luogo); qui solo se l'ora è nella finestra
    out["notte"] = _in_night(rows[0]["t"], {"night_from": va.get("notte_dalle", "19:00"), "night_to": va.get("notte_alle", "06:00")})
    # LED
    leds = [r["led"] for r in rows if r.get("led") is not None]
    out["led_max"] = max(leds) if leds else None
    out["led_min"] = min(leds) if leds else None
    lmax, lvar = float(va["led_max_A"]), float(va.get("led_var_max_A", 0.1))
    chk("corrente barra LED", f"{_fmt(max(leds))} A" if leds else "non letta", f"< {_fmt(lmax)} A",
        None if not leds else max(leds) < lmax, f"corrente LED {_fmt(max(leds)) if leds else ''} A (massimo {_fmt(lmax)})")
    if leds and max(leds) - min(leds) > lvar:
        t0l = next((r["t"] for r in rows if r.get("led") is not None and abs(r["led"] - leds[0]) > lvar), None)
        quando = f" al minuto {(t0l - rows[0]['t']) / 60:.0f}" if t0l else ""
        chk("barra LED costante", f"da {_fmt(min(leds))} a {_fmt(max(leds))} A{quando}", f"variazione ≤ {_fmt(lvar)} A", False,
            f"barra LED cambiata durante la prova (da {_fmt(min(leds))} a {_fmt(max(leds))} A{quando})")
    else:
        chk("barra LED costante", f"±{_fmt((max(leds) - min(leds)) / 2)} A" if leds else "non letta", f"variazione ≤ {_fmt(lvar)} A", True if leds else None)
    uok = str(va.get("user_ok") or "")
    bad = sorted({r["user"] for r in rows if uok and r.get("user") and r["user"] != uok})
    us = sorted({r["user"] for r in rows if r.get("user")})
    chk("colonnina libera (USER STATUS)", ", ".join(us) or "non letto", f"sempre {uok}" if uok else "non richiesto",
        (not bad) if (uok and us) else None, "colonnina non libera (USER STATUS " + ", ".join(bad) + ")")
    # base stabile: dall'attesa registrata (passo 0), se c'è
    br = [(t, a, l) for t, a, l in _base_rows(path) if a is not None]
    bw = float(va.get("base_min", 30)) * 60
    btol = float(va.get("base_tol", 0.3))
    if br and br[-1][0] - br[0][0] >= 0.95 * bw:
        last = [a for t, a, _ in br if t >= br[-1][0] - bw]
        span = max(last) - min(last)
        chk("base stabile", f"escursione {_fmt(span)} °C in {bw / 60:.0f} min", f"≤ {_fmt(btol)} °C", span <= btol,
            f"base non stabile: escursione {_fmt(span)} °C negli ultimi {bw / 60:.0f} min (limite {_fmt(btol)})")
    else:
        chk("base stabile", "attesa non registrata (parti sempre o prova senza attesa)", f"≤ {_fmt(btol)} °C in {bw / 60:.0f} min", None)
    out["frast"]["base"] = _frast([(t, a) for t, a, _ in br], float((so.get("frastagliatura") or {}).get("finestra_s", 120))) if br else None
    tol = float(va["tolleranza_s"])
    mis = []
    for k, ph in enumerate(phases):
        if ph["role"] != "misura" or k == 0 or phases[k - 1]["role"] != "carica" or k not in phase_t:
            continue
        c, pre = k - 1, k - 2
        m = {"n": len(mis) + 1}
        rk = [r for r in rows if r["i"] == k]
        rc = [r for r in rows if r["i"] == c]
        d = None
        if pre >= 0 and phases[pre]["role"] in ("rif", "recupero") and pre in phase_t:
            e = end_of(pre)
            d = _slope([(r["t"], r["aux"]) for r in T(e - 180, e)])
        m["deriva"] = d
        dc = d or 0.0
        fon = [r for r in rk if r.get("fan")]
        hon = next((r["t"] for r in rc if r.get("heater")), None)
        hoff = next((r["t"] for r in rc + rk if hon is not None and r["t"] > hon and r.get("heater") is False), None)
        if hon is None:
            chk(f"scaldiglia letta accesa (misura {m['n']})", "mai letta accesa", f"{phases[c]['s']} s ± {tol:.0f}", False,
                f"misura {m['n']}: riscaldatore mai letto acceso")
        elif hoff is not None:
            du = hoff - hon
            chk(f"scaldiglia letta accesa (misura {m['n']})", f"{du:.0f} s", f"{phases[c]['s']} s ± {tol:.0f}", abs(du - phases[c]["s"]) <= tol,
                f"misura {m['n']}: riscaldatore {du:.0f} s invece di {phases[c]['s']}")
        else:
            chk(f"scaldiglia letta accesa (misura {m['n']})", "spegnimento non letto", f"{phases[c]['s']} s ± {tol:.0f}", None)
        if not fon:
            chk(f"ventola letta accesa (misura {m['n']})", "mai letta accesa", f"{ph['s']} s − {tol:.0f}", False, f"misura {m['n']}: ventola mai letta accesa")
            mis.append(m)
            continue
        ton, tlast = fon[0]["t"], fon[-1]["t"]
        fu = tlast + INTERVAL_S - ton
        chk(f"ventola letta accesa (misura {m['n']})", f"{fu:.0f} s", f"≥ {ph['s'] - tol:.0f} s", fu >= ph["s"] - tol,
            f"misura {m['n']}: ventola accesa {fu:.0f} s invece di {ph['s']}")
        best = None
        for r in rows:
            if ton <= r["t"] <= ton + 45:
                s = _slope([(x["t"], x["aux"]) for x in rows if r["t"] - 15 <= x["t"] < r["t"] + 15.001])
                if s is not None and (best is None or s < best):
                    best = s
        m["I"] = None if best is None else -best + dc
        m["T0"] = near(ton)
        tail = [r["aux"] for r in fon if r["t"] >= tlast - 30 + 0.001]
        m["Tfine"] = _mean(tail)
        m["A"] = (m["T0"] - m["Tfine"]) if m["T0"] is not None and m["Tfine"] is not None else None
        m["IA"] = m["I"] / m["A"] if m["I"] is not None and m["A"] and m["A"] > 0.05 else None
        f = _expfit([(r["t"] - ton, r["aux"]) for r in fon])
        m["tau"] = f["tau"] if f and f.get("tau") and (f.get("r2") or 0) > 0.9 else None
        m["Tinf"] = f.get("Tinf") if f else None
        if rc:
            a0, a1 = rc[0]["aux"], rc[-1]["aux"]
            m["salita"] = a1 - a0 - dc * (rc[-1]["t"] - rc[0]["t"]) / 60
        else:
            m["salita"] = None
        m["R"] = m["A"] / m["salita"] if m["A"] is not None and m["salita"] and m["salita"] > 0.1 else None
        m["rimbalzo"] = None
        if k + 1 < len(phases) and phases[k + 1]["role"] == "recupero" and (k + 1) in phase_t:
            ir = next((i for i, p in enumerate(phases) if p["role"] == "rif"), None)
            if ir is not None and ir in phase_t:
                er = end_of(ir)
                arif = _mean([r["aux"] for r in T(er - 60, er)])
                a15 = near(phase_t[k + 1] + min(900, phases[k + 1]["s"]))
                if arif is not None and a15 is not None and m["Tfine"] is not None and abs(arif - m["Tfine"]) > 0.1:
                    m["rimbalzo"] = 100 * (a15 - m["Tfine"]) / (arif - m["Tfine"])
        m["t_on"] = ton
        mis.append(m)
    out["misure"] = mis
    # frastagliatura per fase (media delle fasi con lo stesso ruolo); «ventola» = misure
    win = float((so.get("frastagliatura") or {}).get("finestra_s", 120))
    for role, key in (("rif", "rif"), ("carica", "carica"), ("misura", "ventola"), ("recupero", "recupero")):
        v = [_frast([(r["t"], r["aux"]) for r in rows if r["i"] == i], win) for i, ph in enumerate(phases) if ph["role"] == role]
        out["frast"][key] = _mean([x for x in v if x is not None])
    try:
        with open(path, encoding="utf-8-sig") as fh:
            ev = [r.get("evento") or "" for r in csv.DictReader(fh, delimiter=";")]
    except Exception:
        ev = []
    ntc, nus = sum("TempContEn" in e for e in ev), sum("uscite_rinviate" in e for e in ev)
    if ntc:
        chk("controllo termico automatico spento", f"tornato attivo {ntc} volte", "TempContEn = 0 per tutta la prova", False,
            f"TempContEn tornato attivo {ntc} volte durante la prova (ripristinato dallo script)")
    out["avvisi"] = [f"uscite non conformi: comando rinviato {nus} volte"] if nus else []
    I = [x.get("I") for x in mis[:2]]
    chk("indice su due misure", f"{sum(1 for x in I if x is not None)} misure calcolate", "2", len(mis) >= 2 and None not in I,
        "indice non calcolabile su due misure")
    d1 = mis[0].get("deriva") if mis else None
    dmax = float(va["deriva_max"])
    chk("deriva della base", "non misurata (manca il riferimento)" if d1 is None else f"{d1:+.3f} °C/min".replace(".", ","),
        f"|deriva| ≤ {_fmt(dmax, 3)} °C/min", None if d1 is None else abs(d1) <= dmax, f"base in deriva {d1:+.3f} °C/min" if d1 is not None else "")
    if d1 is None:
        out["motivi"].append("deriva non misurata (manca il riferimento)")
    Im = _mean(I) if None not in I and len(I) == 2 else None
    C = abs(I[0] - I[1]) / Im if Im else None
    chk("coerenza delle due misure (C)", _fmt(C), f"≤ {_fmt(float(va['C_max']))}", None if C is None else C <= float(va["C_max"]),
        f"misure non coerenti (C {_fmt(C)} oltre {_fmt(float(va['C_max']))})")
    lat, lon = coords
    if t_ext is not None:
        out["Text"] = t_ext
    elif so.get("meteo", {}).get("attivo") and mis and lat is not None:
        tx = [sc_text(lat, lon, x["t_on"]) for x in mis if x.get("t_on")]
        out["Text"] = _mean([a for a, _ in tx])
        out["vento"] = _mean([b for _, b in tx])
    for x in mis:
        tx = out["Text"]
        dT = (x["T0"] - tx) if tx is not None and x.get("T0") is not None else None
        x["In"] = x["I"] / dT if dT and dT > 0.5 and x.get("I") is not None else None
        x["Q"] = x["A"] / dT if dT and dT > 0.5 and x.get("A") is not None else None
    ind = {k: _mean([x.get(k) for x in mis[:2]]) for k in ("I", "A", "IA", "tau", "R", "salita", "In", "Q", "T0", "Tfine")}
    ind["rimbalzo"] = mis[0].get("rimbalzo") if mis else None
    ind["C"], ind["deriva"], ind["deriva2"] = C, d1, (mis[1].get("deriva") if len(mis) > 1 else None)
    ind["frast"] = out["frast"].get("ventola")
    out["ind"] = ind
    out["valida"] = not out["motivi"]
    return out


INTERVAL_S = 5


def sc_rule(ev, seq, soglie=None):
    """STACCATO se scatta una condizione (in OR); confidenza = condizioni vere su quelle calcolabili."""
    so = soglie or sc_soglie()
    r = (so.get("regole") or {}).get(seq)
    res = {"esito": "", "confidenza": "", "scattate": [], "condizioni": [], "regola": ""}
    if not ev.get("valida"):
        res.update(esito="DA RIPETERE", motivo="non valida: " + "; ".join(ev.get("motivi") or []))
        return res
    if not r:
        res.update(esito="SOLO INDICATORI", motivo=f"nessuna soglia per {seq}: indicatori salvati per la taratura")
        return res
    ind, vere, calc = ev["ind"], 0, 0
    for key, name, op, lab in SC_COND:
        if key not in r:
            continue
        v, s = ind.get(name), float(r[key])
        txt = lab.format(f"{s:g}".replace(".", ","))
        if v is None:
            res["condizioni"].append({"c": txt, "v": None, "vera": None})
            continue
        calc += 1
        ok = v >= s if op == ">=" else v < s
        vere += ok
        res["condizioni"].append({"c": txt, "v": v, "vera": ok})
        if ok:
            res["scattate"].append(txt)
    res["regola"] = " OR ".join(c["c"] for c in res["condizioni"])
    res["confidenza"] = f"{vere}/{calc}"
    es = so["esito"]
    if calc < int(es["min_calcolabili"]):
        res.update(esito="DA RIPETERE", motivo=f"solo {calc} condizioni calcolabili")
    elif vere == 0:
        res.update(esito="ATTACCATO", motivo="nessuna condizione scattata")
    elif vere >= int(es["staccato_min_condizioni"]):
        res.update(esito="STACCATO", motivo="scattate: " + ", ".join(res["scattate"]))
    else:
        res.update(esito="DA RIPETERE", motivo="poche condizioni vere (" + ", ".join(res["scattate"]) + ")")
    return res


def _notte(t):
    """Notte della prova: le prove fino a mezzogiorno appartengono alla sera prima."""
    if not t:
        return ""
    return datetime.fromtimestamp(t - 12 * 3600).strftime("%Y-%m-%d")


def _cfg_name(cfg):
    """«T2-campo» → (T2, campo)."""
    a, _, b = str(cfg or "").partition("-")
    return a, b


def sc_eval_archive(path, station):
    """Estrae dall'archivio CSV di prova, risultati e diagnosi; valuta ogni prova e salva in prove.json."""
    so = sc_soglie()
    items, risultati, diag = [], {}, []
    with tarfile.open(path, "r:gz") as tf:
        mem = [m for m in tf.getmembers() if m.isfile()]
        for m in mem:
            name = m.name.lstrip("./")
            base = os.path.basename(name)
            data = tf.extractfile(m).read()
            text = data.decode("utf-8", "replace").lstrip("﻿")
            if base.startswith("risultati") and base.endswith(".csv"):
                rd = list(csv.reader(io.StringIO(text), delimiter=";"))
                if rd and rd[0] and rd[0][0] == "data":
                    hd = rd[0]
                    for f in rd[1:]:
                        if len(f) == len(hd):
                            x = dict(zip(hd, f))
                            x["test"] = name.startswith("collaudo/")
                            risultati[x.get("file", "")] = x
                        elif len(f) >= 8 and f[4] in ("interrotta", "non partita"):     # righe di versioni vecchie
                            risultati[f[-1]] = {"data": f[0], "cuid": f[1], "configurazione": f[2], "esito": f[4], "validita": f[-3], "file": f[-1],
                                                "test": name.startswith("collaudo/")}
            elif base == "diagnosi.csv":
                for x in csv.DictReader(io.StringIO(text), delimiter=";"):
                    diag.append(x)
            elif base.endswith((".log", "_pagina.txt")) and re.match(r".+_\d{8}_\d{4}", base):
                d = SC_DATI / _safe_name(base.split("_", 1)[0])
                d.mkdir(parents=True, exist_ok=True)
                (d / base).write_bytes(data)
            elif base.endswith(".csv") and "_continua_" in base:
                d = SC_DATI / _safe_name(base.split("_continua_")[0]) / "continua"
                d.mkdir(parents=True, exist_ok=True)
                (d / base).write_bytes(data)
            elif base.endswith(".csv") and not base.startswith(("baseline", "controlli")):
                cu = text.split("\n", 2)[1].split(";")[2] if text.count("\n") > 1 and text.startswith("epoch;") else ""
                d = SC_DATI / _safe_name(cu or "sconosciuto")
                d.mkdir(parents=True, exist_ok=True)
                (d / base).write_bytes(data)
                items.append((name, d / base))
    prove = load(F_SC_PROVE, {})
    out = []
    for name, p in items:
        rr = risultati.get(p.name, {})
        m = re.match(r"(.+?)_(.+)_(\d{8}_\d{4})\.csv$", p.name)
        cuid, cfg = (m.group(1), m.group(2)) if m else ("", "")
        seq, nome = _cfg_name(rr.get("configurazione") or cfg)
        if cfg.startswith(("diagnosi", "monitor", "controlli")):
            continue
        it = sc_build_item(p, cuid, seq, nome, station, rr, name.startswith("collaudo/"), so)
        prove[p.name] = it
        out.append(it)
    for f, rr in risultati.items():                    # prove non partite: nessun CSV con i passi
        if rr.get("esito") == "non partita" and f not in prove:
            seq, nome = _cfg_name(rr.get("configurazione"))
            key = f"np_{rr.get('cuid', '')}_{rr.get('data', '')}_{seq}"
            try:
                t = datetime.strptime(rr.get("data", ""), "%Y-%m-%d %H:%M").timestamp()
            except ValueError:
                t = None
            prove[key] = {"file": "", "cuid": rr.get("cuid", ""), "colonnina": station.get("name", ""), "seq": seq, "nome": nome,
                          "inizio": t, "notte": _notte(t), "test": rr.get("test", False), "esito": "NON PARTITA",
                          "motivo": rr.get("validita", ""), "valida": False, "ind": {}, "misure": [], "Text": None}
    save(F_SC_PROVE, prove)
    if diag:
        sc_store_diag(station, diag[-1])
    return out


def sc_build_item(p, cuid, seq, nome, station, rr, test, so):
    la, lo = _coords(cuid, station)
    ev = sc_eval_csv(p, seq, so, (la, lo))
    cuid = ev.get("cuid") or cuid
    ru = sc_rule(ev, seq, so)
    if ev.get("avvisi"):
        ru["motivo"] = "; ".join(x for x in [ru.get("motivo", "")] + ev["avvisi"] if x)
    if rr.get("esito") == "interrotta":
        ru.update(esito="DA RIPETERE", motivo="interrotta: " + (rr.get("validita") or ""))
    t = ev.get("inizio")
    extra = [x for x in (p.with_suffix(".log").name, p.stem + "_pagina.txt") if (p.parent / x).exists()]
    return {"file": p.name, "dir": p.parent.name, "extra": extra, "cuid": cuid, "colonnina": station.get("name", ""), "seq": seq, "nome": nome,
            "inizio": t, "notte": _notte(t), "test": test, "valida": ev["valida"], "motivi_validita": ev["motivi"],
            "ind": ev["ind"], "misure": [{k: v for k, v in m.items() if k != "t_on"} for m in ev["misure"]],
            "Text": ev["Text"], "vento": ev["vento"], "led_max": ev.get("led_max"), "esito_script": rr.get("esito", ""), "avvisi": ev.get("avvisi") or [],
            "valutato": datetime.now().isoformat(timespec="seconds"), **ru}


def sc_reevaluate():
    so = sc_soglie()
    prove = load(F_SC_PROVE, {})
    sts = {s.get("name"): s for s in stations()}
    for k, it in list(prove.items()):
        if not it.get("file"):
            continue
        p = SC_DATI / it.get("dir", "") / it["file"]
        if p.is_file():
            prove[k] = sc_build_item(p, it["cuid"], it["seq"], it["nome"], sts.get(it.get("colonnina"), {"name": it.get("colonnina", "")}),
                                     {"esito": it.get("esito_script", ""), "validita": (it.get("motivo") or "").replace("interrotta: ", "")}, it.get("test"), so)
    save(F_SC_PROVE, prove)
    diag = load(F_SC_DIAG, {})
    for k, d in diag.items():
        diag[k] = {**d, **_diag_verdict(d.get("raw") or {}, so)}
    save(F_SC_DIAG, diag)
    return len(prove)


# ---------------- diagnostica di livello 0

_DIAG_COLS = ["cuid", "campioni", "ping_tentativi", "ping_ko_pct", "ping_ms", "aux_fault", "aux_fault_pct", "aux_fermi", "aux_vuoti_pct", "esito", "motivi"]


def _diag_verdict(raw, so):
    dg = so["diagnostica"]
    why, nota = [], []
    pk, fp = _flt(raw.get("ping_ko_pct")), _flt(raw.get("aux_fault_pct"))
    if pk is not None and pk >= float(dg["ping_ko_min_pct"]):
        why.append(f"schermo non risponde al ping ({pk:.0f}% falliti)")
    elif pk is None:
        nota.append("ping non eseguito (manca display_ip)")
    if fp is not None and fp >= float(dg["aux_fault_min_pct"]):
        why.append(f"AUX FAULT = {raw.get('aux_fault', '')}")
    if dg.get("aux_fermi") and str(raw.get("aux_fermi")) == "1":
        why.append("dati AUX fermi")
    vu = _flt(raw.get("aux_vuoti_pct"))
    if (_flt(raw.get("campioni")) or 0) == 0 or (vu is not None and vu >= 100):
        return {"esito": "DA VERIFICARE", "motivo": "pagina della colonnina non leggibile"}
    return {"esito": "ANOMALIA" if why else "OK", "motivo": "; ".join(why + nota)}


def sc_store_diag(station, raw):
    so = sc_soglie()
    raw = {k: raw.get(k, "") for k in _DIAG_COLS + ["data"]}
    k = raw.get("cuid") or station.get("name", "")
    with LOCK:
        d = load(F_SC_DIAG, {})
        old = d.get(k) or {}
        sto = (old.get("storico") or []) + ([{x: old[x] for x in ("cuid", "colonnina", "data", "raw", "esito", "motivo") if x in old}] if old.get("data") else [])
        d[k] = {"cuid": raw.get("cuid", ""), "colonnina": station.get("name", ""), "data": raw.get("data") or datetime.now().strftime("%Y-%m-%d %H:%M"),
                "raw": raw, **_diag_verdict(raw, so), "storico": [h for h in sto if h.get("data") != (raw.get("data") or "")][-60:]}
        save(F_SC_DIAG, d)
        if raw.get("cuid") and station.get("id"):
            cp = sc_campo()
            cp.setdefault("id_cuid", {})[station["id"]] = raw["cuid"]
            save(F_SC_CAMPO, cp)


# ---------------- stato per colonnina

def _stato_label(sb, ris):
    st, _, det = (sb or "").partition(";")
    if st == "in_corso":
        return "in corso", det
    if st == "attesa_base":
        return "in attesa della base stabile", ""
    if st == "programmata":
        return "programmata", ("prossimo avvio " + det) if det else ""
    if st == "pausa":
        return "in pausa tra le sequenze", det
    if st == "monitoraggio":
        return "monitoraggio passivo", det
    if st == "diagnosi":
        return "diagnostica in corso", ""
    if st == "anomala":
        return "interrotta in modo anomalo", det
    if st == "libera":
        last = ris[-1] if ris else None
        if last:
            es, v = last.get("esito", ""), last.get("validita", "")
            quando = f"{last.get('configurazione', '')} {last.get('data', '')[11:16]}".strip()
            if es == "non partita":
                return "libera · ultima non partita", f"{quando}: {v}"
            if es == "interrotta":
                return "libera · ultima interrotta", f"{quando}: {v} (da ripetere)"
            if es == "non valida" or v.startswith("non valida"):
                return "libera · ultima non valida", f"{quando}: {v.replace('non valida:', '').strip(' ,')} (da ripetere)"
            return "libera · ultima completata", quando
        return "nessuna prova", ""
    return "sconosciuto", sb or "nessuna risposta"


def _ll(line):
    """Riga del registro di Distribuzione senza l'ora davanti."""
    return re.sub(r"^\d\d:\d\d(:\d\d)?\s+", "", line).strip()


def _sc_store_stato(sid, x, kind):
    """Stato della prova dalla riga STATO_BREVE del registro (Stato, Avvia, Ferma, Raccogli)."""
    sb = next((_ll(l)[len("STATO_BREVE="):] for l in reversed(x.get("log", [])) if _ll(l).startswith("STATO_BREVE=")), None)
    ris = []
    for l in x.get("log", []):
        l = _ll(l)
        if l.startswith("RIS="):
            f = l[4:].split("/", 1)[-1].split(";")
            if len(f) >= 5:
                ris.append({"data": f[0], "configurazione": f[2], "esito": f[4], "validita": f[-3] if len(f) > 8 else ""})
    ris.sort(key=lambda r: r.get("data", ""))          # la più recente per ultima (test/ e collaudo/ mescolati)
    if sb is None and (kind.endswith(":stato") or kind.endswith(":pulisci") or kind.endswith(":raccogli")):
        lab, det = "non raggiungibile", x.get("error") or ""
    elif sb is None:
        lab, det = ("programmata", "") if x["state"] == "ok" else ("errore", x.get("error") or "")
    elif kind.endswith(":pulisci") and sb.startswith("libera"):
        lab, det = "fermata", "colonnina pulita: nessun piano, nessuna prova"
    else:
        lab, det = _stato_label(sb, ris)
    with LOCK:
        d = load(F_SC_STATO, {})
        d[sid] = {"stato": lab, "dettaglio": det, "ora": datetime.now().strftime("%Y-%m-%d %H:%M"), "colonnina": x["name"],
                  "continua": _continua(x)}
        save(F_SC_STATO, d)


def _continua(x):
    l = next((_ll(l) for l in reversed(x.get("log", [])) if _ll(l).startswith("CONTINUA=")), "")
    st, _, det = l[len("CONTINUA="):].partition(";")
    return f"acquisizione continua attiva: {det}" if st == "attiva" else ("acquisizione continua spenta" if st == "spenta" else "")


def sc_after_run(run):
    kind = run.get("kind", "")
    so = sc_soglie()
    sts = {s["id"]: s for s in stations()}
    for sid, x in run["stations"].items():
        st = sts.get(sid, {"id": sid, "name": x["name"]})
        if kind.endswith(":raccogli"):
            _sc_store_stato(sid, x, kind)
            for f in [f for f in x.get("files", []) if f.endswith(".tar.gz")][-1:]:
                try:
                    sc_eval_archive(DIST_OUT / run["id"] / f, st)
                except Exception as e:
                    x.setdefault("log", []).append("valutazione non riuscita: " + describe_error(e))
        elif kind.endswith(":diagnosi"):
            line = next((_ll(l) for l in reversed(x.get("log", [])) if _ll(l).startswith("DIAGNOSI;")), "")
            if line:
                sc_store_diag(st, dict(zip(_DIAG_COLS, line.split(";")[1:])))
            else:
                with LOCK:
                    d = load(F_SC_DIAG, {})
                    k = (load(F_SC_CAMPO, {}).get("id_cuid") or {}).get(sid) or x["name"]
                    d[k] = {"cuid": "", "colonnina": x["name"], "data": datetime.now().strftime("%Y-%m-%d %H:%M"), "raw": {},
                            "esito": "NON ESEGUITA", "motivo": x.get("error") or "nessuna risposta"}
                    save(F_SC_DIAG, d)
        elif kind.endswith(":verifica"):
            line = next((_ll(l) for l in reversed(x.get("log", [])) if _ll(l).startswith("VERIFICA=")), "")
            ok, _, det = line[len("VERIFICA="):].partition(";")
            lab = "pulita" if ok == "OK" else "da pulire: premi Ferma" if ok == "KO" else "non verificata"
            with LOCK:
                d = load(F_SC_STATO, {})
                d[sid] = {"stato": lab, "dettaglio": det or x.get("error") or "", "ora": datetime.now().strftime("%Y-%m-%d %H:%M"), "colonnina": x["name"],
                          "continua": _continua(x) or "acquisizione continua spenta"}
                save(F_SC_STATO, d)
        elif kind.endswith(":stato") or kind.endswith(":programma") or kind.endswith(":test") or kind.endswith(":pulisci") or kind.endswith(":continua"):
            _sc_store_stato(sid, x, kind)
        if any(kind.endswith(k) for k in (":programma", ":pulisci", ":continua", ":verifica", ":stato", ":raccogli")):
            EXEC.submit(lambda s=sid: _safe_poll(s))


def _safe_poll(sid):
    try:
        sc_monitor_poll(sid)
    except Exception:
        pass


# ---------------- azioni sulle colonnine (pacchetti di Distribuzione costruiti al volo)

def sc_action(body):
    kind = body.get("pagina")
    if kind not in ("lab", "mass"):
        raise ValueError("pagina sconosciuta")
    c = sc_cfg(kind)
    act = body.get("action")
    ps = "cd {{dir}} && sh prova_schermo.sh"
    files, steps = [], []
    data, ver = _camp_script()
    pid = f"schermo-{kind}"
    d = DIST_FILES / pid
    d.mkdir(parents=True, exist_ok=True)

    def add(name, blob, exe=False):
        (d / name).write_bytes(blob)
        files.append({"name": name, "size": len(blob), "md5": hashlib.md5(blob).hexdigest(), "exec": exe})
    add("prova_schermo.sh", data, True)
    pul = {"type": "comando", "command": ps + " --pulisci", "timeout": 150, "expect": "Colonnina pulita"}
    chk = {"type": "comando", "command": "cd {{dir}} && sh prova_schermo.sh --processi; sh prova_schermo.sh --stato", "timeout": 90,
           "expect": "piani in attesa: 1 "}
    so = sc_soglie()
    if act == "programma":
        for s in c["sequenze"]:
            f = SC_SEQ / f"seq_{s}.conf"
            if not f.exists():
                raise ValueError(f"sequenza {s} mancante")
            if not _seq_parse(f.read_text(encoding="utf-8", errors="replace"))["passi"]:
                raise ValueError(f"la sequenza {s} non ha passi (file {f} vuoto o rovinato): aprila in Prova → Avanzate → Sequenze e salvala, o eliminala")
            pa = c["partenza"]
            tail = ("\n# condizioni di partenza impostate in PoleDesk (" + ("Prova" if kind == "lab" else "Campo") + ")\n"
                    f"STABLE_TOL={pa['stable_tol']:g}\nPRE_WAIT_MIN={int(pa['pre_wait_min'])}\nMIN_UPTIME_H={int(round(pa['min_uptime_h']))}\n"
                    f"USER_OK=\"{pa['user_ok']}\"\nMIN_FREE_KB={int(pa['min_free_mb'] * 1000)}\n")
            add(f.name, f.read_bytes().replace(b"\r\n", b"\n").rstrip(b"\n") + b"\n" + tail.encode())
        if c["quando"] == "ore":
            q = (f"--alle {c['ora']}" if c["ore_dalle"] else "--subito") + f" --ogni-ore {c['ogni_ore']:g} --volte {c['volte']}"
            name = f"ogni {c['ogni_ore']:g} ore, {c['volte']} volte" + (f" dalle {c['ora']}" if c["ore_dalle"] else " da subito")
        elif c["quando"] == "subito":
            q, name = "--subito", "una volta, subito"
        elif c["quando"] == "una":
            q, name = f"--alle {c['ora']}", f"una volta alle {c['ora']}"
        elif c.get("ruota") and len(c["sequenze"]) > 1 and int(c["giorni"]) > 1:
            q = f"--alle {c['ora']} --calendario \"" + ";".join(",".join(n) for n in lab_calendario(c)) + '"'
            name = f"ogni notte alle {c['ora']} per {c['giorni']} notti, ordine che ruota"
        else:
            q = f"--alle {c['ora']}" + (f" --giorni {c['giorni']}" if int(c["giorni"]) > 1 else "")
            name = f"ogni notte alle {c['ora']} per {c['giorni']} notti"
        if not q.count("--calendario"):
            q += " " + " ".join(c["sequenze"])
        q += f" --pausa {c['pausa_min']}" + (" --parti-sempre --base 0" if c["parti_sempre"] else f" --base {c['base_min']}")
        if c["ridotti"]:
            q += f" --prova --scala {c['scala']:g}"
            name += ", tempi ridotti (dati in collaudo/)"
        if c["parti_sempre"]:
            name += ", parti sempre"
        # due pulizie: se la prima trova processi che si stanno ancora chiudendo, la seconda li ritrova chiusi
        steps = [{"type": "invia"}, {**pul, "continue": True}, pul,
                 {"type": "comando", "command": ps + f" {q} --nome {{{{nome_prova}}}} {{{{ipopt}}}} --id {{{{runid}}}}", "timeout": 60, "expect": "Avvii"}]
        if c["continua"]:
            steps.append({"type": "comando", "command": f"cd {{{{dir}}}} && IP_SCHERMO={{{{ipval}}}} sh prova_schermo.sh --continua-avvia {c['continua_s']} {c['continua_ore']}",
                          "timeout": 30, "expect": "Acquisizione continua avviata"})
            name += f" + acquisizione continua ogni {c['continua_s']} s"
        steps.append(chk)
    elif act == "diagnosi":
        steps = [{"type": "invia"},
                 {"type": "comando", "command": "cd {{dir}} && IP_SCHERMO={{ipval}} sh prova_schermo.sh --diagnosi " + str(int(so["diagnostica"]["minuti"])),
                  "timeout": int(so["diagnostica"]["minuti"]) * 60 + 90, "expect": "DIAGNOSI;"}]
        name = "diagnostica (livello 0, nessun comando)"
    elif act == "stato":
        steps = [{"type": "comando", "command": ps + " --processi; sh prova_schermo.sh --stato; echo '== log'; tail -n 6 programmata.log 2>/dev/null; true", "timeout": 60}]
        files = []
        name = "stato e processi"
    elif act == "continua":
        steps = [{"type": "invia", "only": ["prova_schermo.sh"]},
                 {"type": "comando", "command": f"cd {{{{dir}}}} && IP_SCHERMO={{{{ipval}}}} sh prova_schermo.sh --continua-avvia {c['continua_s']} {c['continua_ore']}",
                  "timeout": 30, "expect": "Acquisizione continua avviata"},
                 {"type": "comando", "command": ps + " --stato", "timeout": 60, "continue": True}]
        name = f"solo acquisizione continua ogni {c['continua_s']} s per {c['continua_ore']} h"
    elif act == "verifica":
        # invia lo script (versione corrente) e controlla: nessun processo, nessun file di stato, TempContEn = 1
        steps = [{"type": "invia", "only": ["prova_schermo.sh"]}, {"type": "comando", "command": ps + " --verifica", "timeout": 60, "expect": "VERIFICA="}]
        name = "verifica pulizia"
    elif act == "pulisci":
        steps = [pul, {"type": "comando", "command": ps + " --stato", "timeout": 60, "continue": True}]   # stato riletto: dopo Ferma non resta «in corso»
        files = []
        name = "ferma e pulisci"
    elif act == "raccogli":
        steps = [{"type": "comando", "command": ps + " --stato", "timeout": 60, "continue": True},
                 {"type": "comando", "command": ps + " --archivio", "timeout": 180, "expect": "Archivio pronto"},
                 {"type": "scarica", "pattern": "archivio_*.tar.gz", "newest": True, "delete": True, "import": True}]
        files = []
        name = "raccogli e valuta"
    else:
        raise ValueError("azione sconosciuta")
    titolo = ("Prova" if kind == "lab" else "Campo") + " · " + name
    p = {"id": pid, "name": titolo, "dir": c["dir"], "files": files, "params": [], "steps": steps}
    per, ids = {}, list(body.get("station_ids") or [])
    for st in stations():
        v = st.get("vars") or {}
        nm = v.get("stato_schermo", "") if c.get("usa_anagrafica", True) else ""
        ip = str(v.get("display_ip", "")).strip()
        ip = ip if re.fullmatch(r"[A-Za-z0-9.:-]+", ip) else ""
        per[st["id"]] = {"nome_prova": re.sub(r"[^A-Za-z0-9_-]", "-", nm or c.get("nome") or "prova"),
                         "ipopt": f"--ip-schermo {ip}" if ip else " ", "ipval": ip or "''"}
    exm = {}
    for sid in ids:
        st = next((x for x in stations() if x["id"] == sid), None)
        if not st:
            continue
        if act in ("programma", "continua"):
            exm[sid] = ex_nuova(kind, act, st, c, per.get(sid, {}).get("nome_prova", ""))
        elif ex_attiva(sid):
            exm[sid] = ex_attiva(sid)
        per.setdefault(sid, {})["runid"] = exm.get(sid) or "nessuna"
    rid = dist_launch(p, {**body, "station_ids": ids, "parallel": body.get("parallel") or c.get("parallelo") or 5}, kind=f"sc-{kind}:{act}",
                      per_station=per, exec_map=exm)
    return rid


def sc_precheck(body):
    """Prima di Avvia: legge lo stato delle colonnine (solo lettura) per avvisare se c'è una prova o un piano in corso."""
    c = sc_cfg(body.get("pagina") or "lab")
    sts = [x for x in stations() if x["id"] in (body.get("station_ids") or [])]

    def one(st):
        try:
            code, out, _ = ssh_exec(st, f"cd {shlex.quote(c['dir'])} 2>/dev/null && sh prova_schermo.sh --stato 2>/dev/null | grep STATO_BREVE", timeout=25)
        except Exception as e:
            return {"id": st["id"], "nome": st.get("name", ""), "stato": "non raggiungibile", "dettaglio": describe_error(e), "occupata": False}
        sb = next((l.strip()[len("STATO_BREVE="):] for l in out.splitlines() if l.strip().startswith("STATO_BREVE=")), "libera;")
        lab, det = _stato_label(sb, [])
        return {"id": st["id"], "nome": st.get("name", ""), "stato": lab, "dettaglio": det,
                "occupata": sb.split(";")[0] in ("anomala", "in_corso", "attesa_base", "programmata", "pausa", "monitoraggio", "diagnosi")}
    with ThreadPoolExecutor(max_workers=max(1, min(20, len(sts)))) as ex:
        return {"colonnine": list(ex.map(one, sts))}


# ---------------- motore unico di decisione (Prova, Campo, Profilo termico, Analisi, Conclusioni)
#
# Una sola regola, letta da soglie.json, per tutte le prove. Ordine di decisione:
#   livello 0 (diagnostica) → validità → taratura → condizioni del metodo A (soglie fisse) → metodo B (riferimento
#   della colonnina) → incerto (A/B in disaccordo o indicatore entro ±margine della soglia) → conferma su più notti.

ESITI = ["staccato (diagnostica)", "staccato", "sospetto", "incerto", "attaccato", "taratura", "non valida", "interrotta",
         "non partita", "solo indicatori"]
F_SC_META = SC_DIR / "prove_meta.json"     # dati inseriti a mano per le prove di Prova e Campo (luogo, tipo, schermo, campagna, note)
_EV_CACHE = {}
_UNI = {"key": None, "rows": None, "dec": None}
COND_FRAST = ("frast_min", "frast", ">=", "frastagliatura in ventola ≥ {} °C (solo all'aperto)")


def _cond_list():
    return SC_COND + [COND_FRAST]


def _esito_cfg(so):
    e = so.get("esito") or {}
    return {"sospetto_min": int(e.get("sospetto_min", 1)), "staccato_min": int(e.get("staccato_min_condizioni", 3)),
            "min_calc": int(e.get("min_calcolabili", 3)), "notti": int(e.get("conferma_notti", 2)), "margine": float(e.get("margine_pct", 10))}


def esiti_legenda(so=None):
    """Legenda degli esiti: la regola di ciascuno, con i numeri del file di configurazione."""
    so = so or sc_soglie()
    e, va, dg, b = _esito_cfg(so), so["validita"], so["diagnostica"], so.get("metodo_B") or {}
    nt = {"sempre": "sempre", "aperto": "solo all'aperto", "mai": "mai"}.get(va.get("notte", "aperto"), "solo all'aperto")
    regole = {seq: [lab.format(_fmt(float(r[k]), 3).rstrip("0").rstrip(",")) for k, _, _, lab in _cond_list() if k in r] for seq, r in (so.get("regole") or {}).items()}
    return {"file": str(F_SC_SOGLIE), "esiti": [
        {"esito": "staccato (diagnostica)", "regola": "livello 0 scattato: ping allo schermo assente (≥ " + _fmt(float(dg["ping_ko_min_pct"]), 0) +
         "% falliti), AUX FAULT ≠ 0 (≥ " + _fmt(float(dg["aux_fault_min_pct"]), 0) + "% dei campioni)" + (", dati AUX fermi" if dg.get("aux_fermi") else "") +
         ", nella stessa notte della prova. La prova termica si fa comunque e il suo esito resta visibile."},
        {"esito": "non valida", "regola": "manca almeno una condizione di validità: notte (" + nt + f", {va.get('notte_dalle', '19:00')}–{va.get('notte_alle', '06:00')})"
         f", LED < {_fmt(float(va['led_max_A']))} A e costante entro ±{_fmt(float(va.get('led_var_max_A', 0.1)))} A, colonnina libera (USER STATUS {va.get('user_ok') or '—'})"
         f", base stabile (≤ {_fmt(float(va.get('base_tol', 0.3)))} °C in {float(va.get('base_min', 30)):.0f} min, se registrata)"
         f", |deriva| ≤ {_fmt(float(va['deriva_max']), 3)} °C/min, scaldiglia e ventola lette accese per la durata prevista (± {float(va['tolleranza_s']):.0f} s)"
         f", C ≤ {_fmt(float(va['C_max']))}, TempContEn sempre a 0. Il pannello «Perché» dice quale manca."},
        {"esito": "taratura", "regola": f"prova segnata «taratura» (schermo verificato attaccato): costruisce il riferimento I₀ della colonnina per il metodo B "
         f"(mediana di Ī, servono {int(b.get('rif_min', 3))} tarature valide della stessa sequenza). Nessun esito."},
        {"esito": "attaccato", "regola": "nessuna condizione di distacco vera (su almeno " + str(e["min_calc"]) + " calcolabili)."},
        {"esito": "sospetto", "regola": f"da {e['sospetto_min']} a {e['staccato_min'] - 1} condizioni vere → ripetere la notte successiva."},
        {"esito": "staccato", "regola": f"ALLARME: {e['staccato_min']} o più condizioni vere, oppure «sospetto» confermato in {e['notti']} notti diverse "
         "(stessa colonnina)."},
        {"esito": "incerto", "regola": f"metodo A e metodo B in disaccordo (B: attaccato se Ī/I₀ ≤ {_fmt(float(b.get('q_attaccato', 1.3)))}, distacco se "
         f"≥ {_fmt(float(b.get('q_staccato', 1.8)))}), oppure un indicatore entro ±{e['margine']:.0f}% della sua soglia che basterebbe a cambiare l'esito, "
         f"oppure meno di {e['min_calc']} condizioni calcolabili."},
    ], "condizioni": regole, "nota": "Condizioni di distacco (metodo A, soglie fisse) per sequenza; la frastagliatura conta solo per le prove all'aperto."}


def _diag_storico():
    d = load(F_SC_DIAG, {})
    out = {}
    for k, x in d.items():
        for h in (x.get("storico") or []) + [x]:
            cu = h.get("cuid") or k
            out.setdefault(cu, []).append(h)
    return out


def _livello0(cuid, notte, so, diag):
    L = [h for h in diag.get(cuid, []) if h.get("data") and _notte(datetime.strptime(h["data"][:16], "%Y-%m-%d %H:%M").timestamp()) == notte]
    if not L:
        return {"eseguito": False, "scattato": False, "controlli": [], "testo": "diagnostica di livello 0 non eseguita in quella notte"}
    h = sorted(L, key=lambda x: x["data"])[-1]
    raw, dg = h.get("raw") or {}, so["diagnostica"]
    pk, fp = _flt(raw.get("ping_ko_pct")), _flt(raw.get("aux_fault_pct"))
    c = [{"nome": "ping allo schermo", "valore": "non eseguito (manca display_ip)" if pk is None else f"{pk:.0f}% falliti",
          "regola": f"scatta se ≥ {float(dg['ping_ko_min_pct']):.0f}%", "scattato": None if pk is None else pk >= float(dg["ping_ko_min_pct"])},
         {"nome": "AUX FAULT", "valore": "–" if fp is None else f"{raw.get('aux_fault') or '0'} in {fp:.0f}% dei campioni",
          "regola": f"scatta se ≠ 0 in ≥ {float(dg['aux_fault_min_pct']):.0f}%", "scattato": None if fp is None else fp >= float(dg["aux_fault_min_pct"])},
         {"nome": "dati AUX fermi", "valore": "sì" if str(raw.get("aux_fermi")) == "1" else "no", "regola": "scatta se fermi" if dg.get("aux_fermi") else "non usato",
          "scattato": (str(raw.get("aux_fermi")) == "1") if dg.get("aux_fermi") else None}]
    sc = any(x["scattato"] for x in c)
    return {"eseguito": True, "scattato": sc, "controlli": c, "data": h["data"], "testo": ("scattato: " + ", ".join(x["nome"] for x in c if x["scattato"])) if sc else "nessun controllo scattato"}


def _cat(n, e):
    return "attaccato" if n == 0 else "staccato" if n >= e["staccato_min"] else "sospetto" if n >= e["sospetto_min"] else "attaccato"


def _decidi(u, so, ctx):
    """Decisione di una prova. u: prova unificata (ev = valutazione di sc_eval_csv). ctx: tarature, diagnostica."""
    e, va, b = _esito_cfg(so), so["validita"], so.get("metodo_B") or {}
    ev, ind = u["ev"], u["ev"].get("ind") or {}
    d = {"livello0": _livello0(u["cuid"], u["notte"], so, ctx["diag"]), "validita": [dict(x) for x in ev.get("controlli") or []],
         "metodo": "", "metodo_perche": "", "condizioni": [], "n_vere": 0, "n_calc": 0, "catena": [], "esito_A": "", "esito_B": "", "QB": None, "I0": None}
    # notte: condizione di validità secondo il luogo
    pol = va.get("notte", "aperto")
    serve = pol == "sempre" or (pol == "aperto" and u["luogo"] == "aperto")
    d["validita"].insert(0, {"nome": "notte", "valore": ("sì" if ev.get("notte") else "no (di giorno)"),
                             "regola": f"{va.get('notte_dalle', '19:00')}–{va.get('notte_alle', '06:00')}" + ("" if serve else " · non richiesta per " + (u["luogo"] or "luogo non indicato")),
                             "ok": (bool(ev.get("notte")) if serve else None)})
    motivi = [x for x in d["validita"] if x["ok"] is False]
    if u.get("stato_script") in ("interrotta", "non partita"):
        d["catena"].append(f"prova {u['stato_script']}: {u.get('motivo_script', '')}")
        d["esito_termico"] = u["stato_script"]
    elif motivi:
        d["catena"].append("validità: manca " + "; ".join(f"{x['nome']} ({x['valore']}, richiesto {x['regola']})" for x in motivi))
        d["esito_termico"] = "non valida"
    elif u["tipo"] == "taratura":
        prev = [t for t in ctx["tar"].get((u["cuid"], u["seq"]), []) if t["t"] < u["t"]]
        I0p = _median([t["I"] for t in prev])
        r = ind.get("I") / I0p if ind.get("I") is not None and I0p else None
        d["catena"].append("prova segnata taratura: entra nel riferimento della colonnina, nessun esito" +
                           (f"; Ī/I₀ delle tarature precedenti = {_fmt(r)}" if r is not None else ""))
        d["esito_termico"] = "taratura"
    else:
        reg = (so.get("regole") or {}).get(u["seq"])
        if not reg:
            d["catena"].append(f"nessuna regola per la sequenza «{u['seq'] or '?'}» in soglie.json: solo indicatori")
            d["esito_termico"] = "solo indicatori"
        else:
            vere, calc = [], []
            for key, name, op, lab in _cond_list():
                if key not in reg:
                    continue
                thr = float(reg[key])
                v = ind.get(name)
                txt = lab.format(_fmt(thr, 3).rstrip("0").rstrip(","))
                c = {"ind": name, "condizione": txt, "valore": v, "soglia": thr, "verso": "≥" if op == ">=" else "<", "vera": None, "vicina": False}
                if key == "frast_min" and u["luogo"] != "aperto":
                    c["nota"] = "non applicata: " + ("in laboratorio" if u["luogo"] == "laboratorio" else "luogo non indicato") + " (vale solo all'aperto)"
                elif v is None:
                    c["nota"] = "non calcolabile"
                else:
                    c["vera"] = v >= thr if op == ">=" else v < thr
                    c["vicina"] = abs(v - thr) <= e["margine"] / 100 * abs(thr) if thr else False
                    calc.append(c)
                    if c["vera"]:
                        vere.append(c)
                d["condizioni"].append(c)
            d["n_vere"], d["n_calc"] = len(vere), len(calc)
            if len(calc) < e["min_calc"]:
                cA = "incerto"
                d["catena"].append(f"metodo A: solo {len(calc)} condizioni calcolabili (servono {e['min_calc']}) → incerto")
            else:
                cA = _cat(len(vere), e)
                d["catena"].append(f"metodo A: {len(vere)} condizioni vere su {len(calc)} calcolabili → {cA}" +
                                   (f" ({', '.join(c['condizione'] for c in vere)})" if vere else ""))
            d["esito_A"] = cA
            # indicatore vicino alla soglia che cambierebbe la categoria
            flip = []
            if cA != "incerto":
                for c in calc:
                    if c["vicina"] and _cat(len(vere) + (-1 if c["vera"] else 1), e) != cA:
                        flip.append(c)
            # metodo B
            tar = [t for t in ctx["tar"].get((u["cuid"], u["seq"]), []) if t["file"] != u["file"]]
            rmin = int(b.get("rif_min", 3))
            if len(tar) >= rmin and ind.get("I") is not None:
                I0 = _median([t["I"] for t in tar])
                Q = ind["I"] / I0 if I0 else None
                cB = "" if Q is None else "attaccato" if Q <= float(b.get("q_attaccato", 1.3)) else "distacco" if Q >= float(b.get("q_staccato", 1.8)) else "incerto"
                d.update(I0=I0, QB=Q, esito_B=cB, metodo="A + B",
                         metodo_perche=f"B disponibile: I₀ = {_fmt(I0)} °C/min da {len(tar)} tarature valide di questa colonnina con la sequenza {u['seq']}")
                d["catena"].append(f"metodo B: Ī/I₀ = {_fmt(Q)} → {cB} (attaccato ≤ {_fmt(float(b.get('q_attaccato', 1.3)))}, distacco ≥ {_fmt(float(b.get('q_staccato', 1.8)))})")
            else:
                d.update(metodo="A", metodo_perche=f"B non disponibile: {len(tar)}/{rmin} tarature valide di questa colonnina con la sequenza {u['seq']}"
                         + ("" if ind.get("I") is not None else "; Ī non calcolato"))
                cB = ""
            dis = cA != "incerto" and cB in ("attaccato", "distacco") and ((cA == "attaccato") != (cB == "attaccato"))
            if cA == "incerto":
                es = "incerto"
            elif dis:
                es = "incerto"
                d["catena"].append(f"A ({cA}) e B ({cB}) in disaccordo → incerto")
            elif flip:
                es = "incerto"
                d["catena"].append("indicatore entro ±" + f"{e['margine']:.0f}% della soglia che cambierebbe l'esito: " +
                                   ", ".join(f"{c['condizione']} (valore {_fmt(c['valore'], 3)})" for c in flip) + " → incerto")
                for c in flip:
                    c["decisiva"] = True
            else:
                es = cA
            d["esito_termico"] = es
    d["esito"] = d["esito_termico"]
    if d["livello0"]["scattato"]:
        d["esito"] = "staccato (diagnostica)"
        d["catena"].insert(0, f"livello 0 ({d['livello0'].get('data', '')}): {d['livello0']['testo']} → staccato (diagnostica); "
                              f"esito della prova termica: {d['esito_termico']}")
    return d


def _meta_sc():
    return load(F_SC_META, {})


def prove_unite(force=False):
    """Tutte le prove (Prova, Campo, Profilo termico) con lo stesso formato e la stessa decisione."""
    so = sc_soglie()
    mt = lambda p: p.stat().st_mtime if p.exists() else 0
    key = tuple(mt(p) for p in (F_SC_PROVE, F_PROF_SUMMARY, F_SC_SOGLIE, F_SC_DIAG, F_SC_META, SC_SEQ))
    if not force and _UNI["key"] == key and _UNI["rows"] is not None:
        return _UNI["rows"]
    items = []
    meta = _meta_sc()
    for it in load(F_SC_PROVE, {}).values():
        p = SC_DATI / it.get("dir", "") / it.get("file", "") if it.get("file") else None
        m = meta.get(it.get("file") or "", {})
        ev = it.get("ev") or {"controlli": [], "ind": it.get("ind") or {}, "misure": it.get("misure") or [], "valida": it.get("valida"),
                               "motivi": it.get("motivi_validita") or [], "frast": {}, "notte": None, "Text": it.get("Text")}
        if p is not None and p.is_file():
            ev = _ev_cached(p, it.get("seq"), so, _coords(it.get("cuid"), {"name": it.get("colonnina", "")}), None)
        items.append({"file": it.get("file") or f"np_{it.get('cuid')}_{it.get('inizio')}", "path": str(p) if p else "", "fonte": "Campo" if m.get("pagina") == "mass" else "Prova/Campo",
                      "cuid": it.get("cuid", ""), "colonnina": it.get("colonnina", ""), "seq": it.get("seq", ""), "t": it.get("inizio") or 0,
                      "notte": it.get("notte") or _notte(it.get("inizio")), "luogo": m.get("luogo", ""), "tipo": m.get("tipo") or "rilevamento",
                      "configurazione": m.get("configurazione", ""), "campagna": m.get("campagna", ""), "note": m.get("note", ""), "test": it.get("test", False),
                      "stato_script": "non partita" if it.get("esito") == "NON PARTITA" else "interrotta" if it.get("esito_script") == "interrotta" else "",
                      "motivo_script": it.get("motivo", ""), "ev": ev, "sc": it})
    for r in _summary_read():
        p = PROF_DIR / r.get("file", "")
        if not p.is_file():
            continue
        tx = _fnum(r.get("temp_esterna"))
        ev = _ev_cached(p, None, so, (None, None), tx)
        try:
            t = datetime.strptime(r.get("data_ora", ""), "%d/%m/%Y %H:%M").timestamp()
        except ValueError:
            t = ev.get("inizio") or 0
        seq = _seq_of(ev)
        luogo = (r.get("luogo") or "").strip().lower()
        luogo = "aperto" if luogo in ("aperto", "esterno", "all'aperto", "fuori") else "laboratorio" if luogo in ("laboratorio", "lab", "chiuso", "interno") else luogo
        items.append({"file": r["file"], "path": str(p), "fonte": "Profilo termico", "cuid": r.get("cuid", ""), "colonnina": r.get("colonnina", ""),
                      "seq": seq, "t": t, "notte": _notte(t), "luogo": luogo, "tipo": "taratura" if (r.get("tipo") == "taratura" or r.get("riferimento") == "si") else "rilevamento",
                      "configurazione": r.get("configurazione", ""), "campagna": r.get("campagna", ""), "note": r.get("note", ""), "test": False,
                      "stato_script": "interrotta" if r.get("esito") == "interrotta" else "", "motivo_script": r.get("motivo", ""), "ev": ev, "row": r})
    # tarature valide per colonnina e sequenza (metodo B), diagnostica
    ctx = {"tar": {}, "diag": _diag_storico()}
    for u in items:
        if u["tipo"] == "taratura" and u["ev"].get("valida") and (u["ev"].get("ind") or {}).get("I") is not None:
            ctx["tar"].setdefault((u["cuid"], u["seq"]), []).append({"file": u["file"], "t": u["t"], "I": u["ev"]["ind"]["I"]})
    for u in items:
        u["d"] = _decidi(u, so, ctx)
    # conferma su più notti: «sospetto» in N notti diverse della stessa colonnina → staccato
    e = _esito_cfg(so)
    by = {}
    for u in sorted(items, key=lambda x: x["t"]):
        if u["d"]["esito_termico"] in ("sospetto", "staccato") and not u["test"]:
            prev = by.setdefault(u["cuid"], [])
            notti = sorted({x["notte"] for x in prev} | {u["notte"]})
            if u["d"]["esito_termico"] == "sospetto" and len(notti) >= e["notti"]:
                u["d"]["esito_termico"] = "staccato"
                u["d"]["conferma"] = notti[-e["notti"]:]
                u["d"]["catena"].append(f"«sospetto» confermato in {e['notti']} notti diverse ({', '.join(n[8:10] + '/' + n[5:7] for n in notti[-e['notti']:])}) → staccato")
                if u["d"]["esito"] != "staccato (diagnostica)":
                    u["d"]["esito"] = "staccato"
            prev.append(u)
    rows = [_row_unita(u) for u in sorted(items, key=lambda x: x["t"])]
    _UNI.update(key=key, rows=rows, dec={u["file"]: u for u in items})
    return rows



def _seq_of(ev):
    """Sequenza di una prova del Profilo termico dai tempi dei passi (T1, T2, T2L...), altrimenti «profilo»."""
    fs = ev.get("fasi") or []
    for s in sc_sequenze():
        p = s["passi"]
        if len(p) == len(fs) and all(abs(a["s"] - x["s"]) <= max(20, 0.15 * x["s"]) for a, x in zip(fs[:-1], p[:-1])):
            return s["nome"]
    return "profilo"


def _ev_cached(p, seq, so, coords, tx):
    k = (str(p), p.stat().st_mtime, seq, tx, json.dumps(so, sort_keys=True)[:4000].__hash__())
    if k not in _EV_CACHE:
        try:
            _EV_CACHE[k] = sc_eval_csv(p, seq, so, coords, tx)
        except Exception as e:
            _EV_CACHE[k] = {"controlli": [{"nome": "lettura del file", "valore": describe_error(e), "regola": "CSV leggibile", "ok": False}],
                            "motivi": ["file non leggibile"], "ind": {}, "misure": [], "valida": False, "frast": {}}
    return _EV_CACHE[k]


def _row_unita(u):
    """Riga nel formato del riepilogo (per Analisi, Conclusioni, Grafici), con gli indicatori e l'esito del motore."""
    ev, d, ind = u["ev"], u["d"], u["ev"].get("ind") or {}
    fr = ev.get("frast") or {}
    s = lambda v, n=4: "" if v is None else f"{v:.{n}f}".replace(".", ",")
    base = dict(u.get("row") or {})
    base.update({"file": u["file"], "cuid": u["cuid"], "colonnina": u["colonnina"], "fonte": u["fonte"], "seq": u["seq"],
                 "data_ora": datetime.fromtimestamp(u["t"]).strftime("%d/%m/%Y %H:%M") if u["t"] else "", "luogo": u["luogo"],
                 "tipo": u["tipo"], "configurazione": u["configurazione"], "campagna": u["campagna"], "note": u["note"],
                 "notte": base.get("notte") or ("si" if ev.get("notte") else "no" if ev.get("notte") is False else ""),
                 "valida": "si" if not [x for x in d["validita"] if x["ok"] is False] and u["stato_script"] == "" else "no",
                 "motivo": "; ".join(f"{x['nome']}: {x['valore']}" for x in d["validita"] if x["ok"] is False) or base.get("motivo", ""),
                 "esito": d["esito"], "esito_termico": d["esito_termico"], "esito_A": d["esito_A"], "esito_B": d["esito_B"], "metodo": d["metodo"],
                 "n_vere": str(d["n_vere"]), "n_calc": str(d["n_calc"]), "QB": s(d["QB"], 3), "I0_B": s(d["I0"], 3),
                 "Ie": s(ind.get("I")), "IA": s(ind.get("IA")), "A_amp": s(ind.get("A")), "R_ar": s(ind.get("R")), "rimbalzo": s(ind.get("rimbalzo"), 1),
                 "In": s(ind.get("In")), "QA": s(ind.get("Q")), "tau_e": s(ind.get("tau"), 1), "C_e": s(ind.get("C")),
                 "frast_base": s(fr.get("base")), "frast_rif": s(fr.get("rif")), "frast_carica": s(fr.get("carica")), "frast_ventola": s(fr.get("ventola")),
                 "frast_recupero": s(fr.get("recupero")), "test": "si" if u["test"] else "", "perche": " · ".join(d["catena"])})
    if not u.get("row"):            # prove di Prova e Campo: campi del riepilogo che Analisi e Conclusioni usano
        base.update({"I_medio": s(ind.get("I")), "C": s(ind.get("C")), "tau_medio": s(ind.get("tau"), 1), "deriva": s(ind.get("deriva"), 4),
                     "temp_esterna": s(ev.get("Text"), 1), "Q": s(d["QB"], 3), "riferimento": "si" if u["tipo"] == "taratura" else "no",
                     "campioni": "", "forzata": "", "modello": ""})
    return base


def prova_perche(file):
    prove_unite()
    u = (_UNI["dec"] or {}).get(file)
    if not u:
        raise KeyError("prova non trovata")
    so = sc_soglie()
    ev, d = u["ev"], u["d"]
    fr = ev.get("frast") or {}
    return {"file": u["file"], "fonte": u["fonte"], "cuid": u["cuid"], "colonnina": u["colonnina"], "seq": u["seq"], "luogo": u["luogo"] or "non indicato",
            "tipo": u["tipo"], "schermo": u["configurazione"], "data": datetime.fromtimestamp(u["t"]).strftime("%d/%m/%Y %H:%M") if u["t"] else "",
            "notte": u["notte"], "esito": d["esito"], "esito_termico": d["esito_termico"], "livello0": d["livello0"], "validita": d["validita"],
            "metodo": d["metodo"], "metodo_perche": d["metodo_perche"], "condizioni": d["condizioni"], "n_vere": d["n_vere"], "n_calc": d["n_calc"],
            "esito_A": d["esito_A"], "esito_B": d["esito_B"], "QB": d["QB"], "I0": d["I0"], "catena": d["catena"],
            "regola": next((x["regola"] for x in esiti_legenda(so)["esiti"] if x["esito"] == d["esito"]), ""),
            "frastagliatura": [{"fase": k, "valore": fr.get(k)} for k in ("base", "rif", "carica", "ventola", "recupero")],
            "indicatori": ev.get("ind") or {}, "Text": ev.get("Text"), "avvisi": ev.get("avvisi") or [], "test": u["test"]}


def prove_meta_edit(body):
    """Dati inseriti a mano per una prova di Prova o Campo (luogo, tipo, schermo, campagna, note)."""
    f = str(body.get("file") or "")
    prove = load(F_SC_PROVE, {})
    if not any(it.get("file") == f for it in prove.values()):
        raise KeyError("prova non trovata")
    fl = body.get("fields") or {}
    with LOCK:
        m = _meta_sc()
        cur = m.setdefault(f, {})
        ch = []
        for k in ("luogo", "tipo", "configurazione", "campagna", "note"):
            if k in fl:
                v = str(fl[k] or "").strip()
                if k == "luogo" and v not in ("", "aperto", "laboratorio"):
                    raise ValueError("luogo: aperto o laboratorio")
                if k == "tipo" and v not in ("", "taratura", "rilevamento"):
                    raise ValueError("tipo: taratura o rilevamento")
                if v != cur.get(k, ""):
                    ch.append(f"{k}: {cur.get(k, '')!r} → {v!r}")
                    cur[k] = v
        save(F_SC_META, m)
    if ch:
        audit(None, "prove", f"dati della prova modificati: {f}", True, "; ".join(ch))
    return {"changed": ch}


def prove_luogo_edit(body):
    """Modifica unica: prove di Prova/Campo nei metadati, prove del Profilo termico nel riepilogo (luogo normalizzato)."""
    f = str(body.get("file") or "")
    if any(it.get("file") == f for it in load(F_SC_PROVE, {}).values()):
        return prove_meta_edit(body)
    return profile_edit(body)


def sc_series_path(name):
    """CSV di una prova: Profilo termico (cartella profiles) oppure Prova/Campo (cartella dati/<seriale>)."""
    if _NAME_RX.fullmatch(name or "") and (PROF_DIR / name).exists():
        return PROF_DIR / name
    if re.fullmatch(r"[A-Za-z0-9._-]+\.csv", name or ""):
        for p in SC_DATI.glob(f"*/{name}"):
            return p
    raise KeyError("file della prova non trovato")


# ---------------- monitor delle esecuzioni (tracciamento centralizzato, diagnostica automatica)
#
# Ogni «Avvia» crea un'ESECUZIONE con identificativo univoco (es. 096-20261119-153012), salvata in
# data/schermo/esecuzioni/<id>.json: piano previsto (avvii, sequenze, passi), comandi di PoleDesk con le loro fasi
# (preparato → inviato → ricevuto → verificato / fallito) e ultima fotografia della colonnina.
# Lo script riceve l'identificativo (--id) e scrive i suoi eventi in eventi.log con lo stesso id e lo stesso codice
# di step (A1 = avvio 1, A1.S2 = seconda sequenza, A1.S2.P3 = passo 3, .pre .base .tc0 .fine .pausa).
# Il monitor legge la colonnina SOLO con «prova_schermo.sh --monitor» (nessun comando alla scheda AUX, nessuna
# lettura di TempContEn, nessun file scritto), confronta piano ed eventi e cerca le anomalie.

SC_EXEC = SC_DIR / "esecuzioni"
SC_EXEC.mkdir(parents=True, exist_ok=True)
F_SC_ATTIVE = SC_EXEC / "attive.json"          # {station_id: id esecuzione attiva}
_EX_LOCK = threading.RLock()
MON_CACHE = {}                                  # station_id -> {"t": epoch PC, "m": dati letti, "err": errore, "out": uscita}
MON_BUSY = set()
MON_STEP_STATI = {"avviato": "in esecuzione", "completato": "completato", "fallito": "fallito", "interrotto": "interrotto",
                  "saltato": "saltato", "non_verificabile": "non verificabile", "in_attesa": "in attesa", "preparato": "in attesa", "chiuso": "interrotto"}


def _ex_path(eid):
    return SC_EXEC / f"{_safe_name(eid)}.json"


def ex_load(eid):
    return load(_ex_path(eid), None) if eid else None


def ex_save(ex):
    with _EX_LOCK:
        save(_ex_path(ex["id"]), ex)


def ex_attiva(sid):
    return load(F_SC_ATTIVE, {}).get(sid)


def _ex_set_attiva(sid, eid):
    with _EX_LOCK:
        d = load(F_SC_ATTIVE, {})
        if eid:
            d[sid] = eid
        else:
            d.pop(sid, None)
        save(F_SC_ATTIVE, d)


def _scaled(s, k):
    return max(10, int(s * k + 0.5)) if k != 1 else int(s)


def _ex_piano(c):
    """Piano previsto dalla configurazione della pagina: avvii (ora, sequenze) e passi con i tempi effettivi."""
    k = c["scala"] if c["ridotti"] else 1
    seqs = {}
    for n in c["sequenze"]:
        d = _seq_def(n) or {"passi": []}
        seqs[n] = [{**p, "s": _scaled(p["s"], k)} for p in d["passi"]]
    now = datetime.now()

    def at(hm, day=0):
        h, m = map(int, hm.split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t + timedelta(days=day)
    avvii = []
    if c["quando"] == "subito":
        avvii = [(now, c["sequenze"])]
    elif c["quando"] == "una":
        avvii = [(at(c["ora"]), c["sequenze"])]
    elif c["quando"] == "ore":
        t0 = at(c["ora"]) if c["ore_dalle"] else now
        avvii = [(t0 + timedelta(hours=c["ogni_ore"] * i), c["sequenze"]) for i in range(int(c["volte"]))]
    else:
        cal = lab_calendario(c) if c.get("ruota") and len(c["sequenze"]) > 1 and int(c["giorni"]) > 1 else [c["sequenze"]] * int(c["giorni"])
        avvii = [(at(c["ora"], i), s) for i, s in enumerate(cal)]
    return {"avvii": [{"ora": t.strftime("%Y-%m-%d %H:%M"), "seq": list(s)} for t, s in avvii], "passi": seqs,
            "pausa_min": int(c["pausa_min"]), "base_min": 0 if c["parti_sempre"] else int(c["base_min"]),
            "parti_sempre": bool(c["parti_sempre"]), "pre_wait_min": int(c["partenza"]["pre_wait_min"]),
            "stable_tol": c["partenza"]["stable_tol"], "ridotti": bool(c["ridotti"]), "scala": k,
            "continua": bool(c.get("continua")), "continua_s": c.get("continua_s")}


def ex_nuova(kind, act, st, c, nome):
    base = re.sub(r"[^A-Za-z0-9]", "", st.get("name") or st["id"])[-12:] or "col"
    eid = f"{base}-{datetime.now():%Y%m%d-%H%M%S}"
    ex = {"id": eid, "station_id": st["id"], "colonnina": st.get("name", ""), "pagina": kind, "tipo": act, "nome": nome,
          "creata": time.time(), "attiva": True, "chiusa": None, "piano": _ex_piano(c) if act == "programma" else None,
          "comandi": [], "anomalie": {}, "ultimo": None}
    ex_save(ex)
    _ex_set_attiva(st["id"], eid)
    return eid


def _ex_cmd(eid, rid, i, **kw):
    """Fase di un comando di PoleDesk nell'esecuzione (stesso evento mostrato in Log/Registro e nel monitor)."""
    if not eid:
        return
    with _EX_LOCK:
        ex = ex_load(eid)
        if not ex:
            return
        c = next((x for x in ex["comandi"] if x["run"] == rid and x["n"] == i), None)
        if c is None:
            c = {"run": rid, "n": i, "fasi": {}}
            ex["comandi"].append(c)
        fase = kw.pop("fase", None)
        if fase:
            c["fasi"][fase] = time.time()
            c["stato"] = fase
        c.update({k: v for k, v in kw.items() if v is not None})
        ex["comandi"] = ex["comandi"][-200:]
        ex_save(ex)


def _ex_track_prepare(run):
    """Alla creazione di un pacchetto delle pagine Prova/Campo: tutti i passi «preparato»."""
    for sid, eid in (run.get("exec") or {}).items():
        for i, s in enumerate(run["package"]["steps"], 1):
            lab = {"invia": "invio dello script e delle sequenze", "comando": s.get("command", ""), "scarica": "scarico dei dati",
                   "attendi": f"attesa {s.get('seconds')} s"}.get(s["type"], s["type"])
            _ex_cmd(eid, run["id"], i, fase="preparato", azione=run.get("kind", "").split(":")[-1], tipo=s["type"], comando=lab,
                    atteso=s.get("expect") or "", ora=time.time())


# ---------- lettura dalla colonnina (solo --monitor)

def _mon_cmd(d, eid):
    q = shlex.quote(d)
    # una versione vecchia dello script non conosce --monitor e lo prenderebbe per il nome di una prova: prima si controlla
    return (f"cd {q} 2>/dev/null || {{ echo MON_ASSENTE=cartella; exit 0; }}; "
            f"[ -f prova_schermo.sh ] || {{ echo MON_ASSENTE=script; exit 0; }}; "
            f"if grep -q -- '^--monitor)' prova_schermo.sh; then sh prova_schermo.sh --monitor {shlex.quote(eid or '')}; "
            f"else echo MON_VECCHIO=$(sed -n 's/^VERSIONE=\"\\(.*\\)\"/\\1/p' prova_schermo.sh); "
            f"for f in .programmata .prova_in_corso .continua; do [ -f $f ] && echo MON_FILE=$f; done; fi")


def _mon_parse(out):
    m = {"EV": [], "PR": [], "PL": [], "RL": [], "GL": [], "JR": [], "JU": [], "DM": [], "F": {}, "VIVO": {}, "AVVIO": []}
    for line in out.splitlines():
        k, sep, v = line.partition("=")
        if not sep:
            continue
        if k in ("EV", "PR", "PL", "RL", "GL", "JR", "JU", "DM", "MON_FILE"):
            m.setdefault(k, []).append(v)
        elif k.startswith("F_"):
            f = m["F"].setdefault(k[2:], {})
            kk, s2, vv = v.partition("=")
            if s2:
                vv = vv.strip("'")
                if kk == "AVVIO":
                    f.setdefault("AVVIO", []).append(vv)
                else:
                    f[kk] = vv
            else:
                f["_"] = v
        elif k.startswith("VIVO_"):
            p = v.split("|", 3) + ["", "", "", ""]
            m["VIVO"][k[5:]] = {"pid": p[0], "vivo": p[1] == "1", "cg": p[2], "cmd": p[3]}
        else:
            m[k] = v
    ev = []
    for e in m["EV"]:
        p = e.split("|", 4)
        if len(p) == 5 and p[0].isdigit():
            ev.append({"t": int(p[0]), "run": p[1], "step": p[2], "stato": p[3], "det": p[4]})
    m["EVENTI"] = ev
    return m


def sc_monitor_poll(sid, eid=None):
    """Una lettura della colonnina (solo --monitor). Risultato in MON_CACHE."""
    st = station_by_id(sid)
    ex = ex_load(eid or ex_attiva(sid))
    d = (ex and sc_cfg(ex["pagina"])["dir"]) or sc_cfg("lab")["dir"]
    t = time.time()
    try:
        code, out, err = ssh_exec(st, _mon_cmd(d, ex["id"] if ex else ""), timeout=int(sc_cfg((ex or {}).get("pagina") or "lab").get("monitor_timeout", 30)))
        m = _mon_parse(out)
        res = {"t": t, "m": m, "err": "" if (m.get("MON_FINE") or m.get("MON_VECCHIO") is not None or m.get("MON_ASSENTE")) else
               f"risposta incompleta (exit {code}){': ' + err.strip()[-200:] if err.strip() else ''}", "out": out[-20000:]}
    except CommandTimeout as e:
        res = {"t": t, "m": _mon_parse(e.out or ""), "err": "nessuna risposta entro il tempo massimo: " + describe_error(e), "timeout": True, "out": e.out or ""}
    except Exception as e:
        res = {"t": t, "m": None, "err": describe_error(e), "out": ""}
    prev = MON_CACHE.get(sid) or {}
    res["ok_t"] = t if res["m"] is not None and not res["err"] else prev.get("ok_t")
    if res["m"] is None and prev.get("m") is not None:
        res["ultimo_m"] = prev.get("m") if prev.get("m") else prev.get("ultimo_m")
        res["ultimo_t"] = prev.get("ok_t")
    elif res["m"] is None:
        res["ultimo_m"], res["ultimo_t"] = prev.get("ultimo_m"), prev.get("ultimo_t")
    MON_CACHE[sid] = res
    if ex:
        v = sc_monitor_view(sid, ex, res)
        _ex_close_if_done(ex, v, res)
    else:
        v = sc_monitor_view(sid, None, res)
    _mon_store_stato(sid, v)
    return v


def _ex_close_if_done(ex, v, res):
    m = res.get("m")
    if not m or res.get("err") or m.get("MON_VECCHIO") is not None:
        return
    f = m["F"]
    nulla = not f.get("programmata") and not f.get("prova_in_corso") and not f.get("lock") and not f.get("continua") and \
        not any(x["vivo"] for x in m["VIVO"].values())
    eta = time.time() - ex["creata"]
    if nulla and eta > 90:
        with _EX_LOCK:
            ex = ex_load(ex["id"]) or ex
            if ex.get("attiva"):
                ex["attiva"], ex["chiusa"] = False, time.time()
                ex["esito_finale"] = v.get("stato", "")
                ex_save(ex)
                if ex_attiva(ex["station_id"]) == ex["id"]:
                    _ex_set_attiva(ex["station_id"], None)


# ---------- costruzione della vista: step previsti + eventi + vivacità dei processi + anomalie

def _hms(t, sec=False):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S" if sec else "%H:%M") if t else ""


def _dur(s):
    if s is None:
        return ""
    s = int(max(0, s))
    return f"{s // 3600} h {s % 3600 // 60:02d} min" if s >= 3600 else f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def _onoff_s(v):
    v = str(v or "").strip().upper()
    if not v:
        return None
    if v.startswith(("ON", "TRUE", "YES")):
        return 1
    if v.startswith(("OFF", "FALSE", "NO")):
        return 0
    try:
        return 1 if float(v.replace(",", ".")) > 0 else 0
    except ValueError:
        return None


def _kv(det, key):
    m = re.search(rf"(?:^|\s){key}=(\S+)", det or "")
    return m.group(1) if m else ""


def _death(m, role, fdata, ev_last_t):
    """Perché il processo registrato (piano, prova, continua) non c'è più: causa accertata, probabile o sconosciuta."""
    pid = (fdata or {}).get("PID", "")
    cg = (fdata or {}).get("CGROUP", "")
    unit = cg.rsplit("/", 1)[-1] if cg else ""
    ver, cause, kind = [], "", "sconosciuta"
    ver.append(f"processo {pid}: non esiste più (letto in /proc)")
    boot = (fdata or {}).get("BOOT", "")
    up = int(m.get("MON_UPTIME") or 0)
    now = int(m.get("MON_ORA") or time.time())
    if boot and m.get("MON_BOOT") and boot != m.get("MON_BOOT"):
        return "accertata", f"la colonnina si è riavviata alle {_hms(now - up)} (accesa da {_dur(up)}): il riavvio chiude piano, prove e acquisizione", \
            ver + [f"identificativo di avvio del sistema cambiato (registrato {boot[:8]}…, ora {m['MON_BOOT'][:8]}…)"], "riavvio"
    ver.append(f"nessun riavvio: colonnina accesa da {_dur(up)}")
    jr = m.get("JR", []) + m.get("JU", [])
    oom = [l for l in jr + m.get("DM", []) if re.search(r"Out of memory|oom-kill|Killed process " + re.escape(pid) + r"\b", l, re.I)]
    if oom:
        return "accertata", "il sistema ha chiuso il processo per memoria esaurita (OOM)", ver + ["journal/kernel: " + oom[-1][-160:]], "memoria"
    kill = [l for l in jr if re.search(r"Killing process " + re.escape(pid) + r"\b", l)]
    stop = [l for l in jr if unit and unit in l and re.search(r"Stopp|Deactivated|Succeeded|Removed|Failed", l)]
    if unit:
        ver.append(f"gruppo del processo: {cg}")
    if kill or (stop and unit.startswith("sshd@")):
        l = (kill or stop)[-1]
        ts = re.match(r"^(\d+)(?:\.\d+)?\s", l)
        quando = f" alle {_hms(int(ts.group(1)))}" if ts else ""
        if unit.startswith("sshd@") or "session" in unit:
            txt = (f"chiusura del collegamento SSH che lo aveva lanciato ({unit}){quando}: systemd chiude il servizio del collegamento "
                   "e con lui tutti i processi partiti da lì")
        else:
            txt = f"systemd ha chiuso il servizio {unit}{quando} e con lui il processo"
        return "accertata", txt, ver + ["journal: " + l[-180:]], "sessione"
    term = [e for e in m.get("EVENTI", []) if e["step"] == "piano" and e["stato"] == "interrotto" and "TERM" in e["det"]]
    if term and role == "piano":
        return "probabile", f"il processo ha ricevuto un segnale di chiusura (TERM) alle {_hms(term[-1]['t'])} da fuori dallo script", \
            ver + ["evento dello script: segnale TERM ricevuto", "journal: nessuna riga che indichi chi l'ha mandato" if not jr else
                   f"journal: {len(jr)} righe lette, nessuna sul processo"], "segnale"
    if m.get("JR") is None or not jr:
        ver.append("journal di systemd: nessuna riga utile (o journal non disponibile)")
    else:
        ver.append(f"journal di systemd: {len(jr)} righe lette dal momento dell'ultimo segno di vita, nessuna sul processo")
    return "sconosciuta", "processo terminato senza chiusura regolare: causa non trovata nel journal né nel kernel" + \
        (f" (ultimo segno di vita alle {_hms(ev_last_t)})" if ev_last_t else ""), ver, "ignota"


def sc_monitor_view(sid, ex, res):
    st = station_by_id(sid) if any(s["id"] == sid for s in stations()) else {"id": sid, "name": sid}
    m = res.get("m")
    stale = m is None
    if m is None:
        m = res.get("ultimo_m") or _mon_parse("")
    pc = time.time()
    now = int(m.get("MON_ORA") or 0) or int(pc)
    if stale:
        now = int(pc)
    off = (int(m["MON_ORA"]) - int(res.get("t") or pc)) if m.get("MON_ORA") and not stale else 0
    eid = (ex or {}).get("id", "")
    F = m["F"]
    sched, inc, cont = F.get("programmata") or {}, F.get("prova_in_corso") or {}, F.get("continua") or {}
    run_staz = sched.get("RUN") or inc.get("RUN") or m.get("MON_RUN_ULTIMO") or ""
    if not ex and run_staz:
        ex = ex_load(run_staz)
        eid = (ex or {}).get("id", "")
    evs = [e for e in m.get("EVENTI", []) if not eid or e["run"] == eid]
    V = m.get("VIVO", {})
    vivo = {r: (V.get(r) or {}).get("vivo", False) for r in ("piano", "prova", "continua", "guardiano")}
    piano = (ex or {}).get("piano") or {}
    INTERVAL = INTERVAL_S
    anom = []

    def A(code, grav, step, titolo, det="", causa=None, verifiche=None, azione="", btn=""):
        anom.append({"codice": code, "gravita": grav, "step": step, "titolo": titolo, "dettaglio": det,
                     "causa": causa or ["sconosciuta", ""], "verifiche": verifiche or [], "azione": azione, "pulsante": btn})

    by = {}
    for e in evs:
        by.setdefault(e["step"], []).append(e)

    def state_of(step):
        L = [e for e in by.get(step, []) if e["stato"] != "avviso"]
        if not L:
            return None, None, None, "", []
        s0 = next((e["t"] for e in L if e["stato"] == "avviato"), None)
        last = L[-1]
        st_ = MON_STEP_STATI.get(last["stato"], last["stato"])
        t1 = last["t"] if last["stato"] not in ("avviato", "in_attesa") else None
        return st_, s0 or (L[0]["t"] if last["stato"] != "in_attesa" else None), t1, last["det"], [e for e in by.get(step, []) if e["stato"] == "avviso"]

    rows = []

    def row(id_, nome, descr, livello, prev_cmd="", atteso="", dur=None, owner="prova"):
        s_, t0, t1, det, avv = state_of(id_)
        r = {"id": id_, "nome": nome, "descr": descr, "livello": livello, "stato": s_ or "in attesa", "inizio": t0, "fine": t1,
             "det": det, "avvisi": [a["det"] for a in avv], "cmd_previsto": prev_cmd, "cmd_inviato": "", "atteso": atteso,
             "effettivo": "", "errore": "", "causa": "", "dur_prev": dur, "owner": owner, "ha_eventi": s_ is not None}
        cmd = _kv(det if s_ else "", "cmd") or next((_kv(e["det"], "cmd") for e in by.get(id_, []) if _kv(e["det"], "cmd")), "")
        if not cmd and id_.endswith(".tc0"):
            cmd = next((e["det"].replace("comando ", "") for e in by.get(id_, []) if e["stato"] == "avviato"), "")
        r["cmd_inviato"] = (cmd + (f" alle {_hms(t0, True)}" if t0 and cmd else "")) if cmd else ""
        if s_ in ("completato", "non verificabile", "saltato"):
            r["effettivo"] = det
        elif s_ in ("fallito", "interrotto"):
            r["errore"] = det
        rows.append(r)
        return r

    # 0. comandi di PoleDesk (preparazione)
    cmds = (ex or {}).get("comandi", [])
    prog_run = next((c["run"] for c in reversed(cmds) if c.get("azione") == "programma"), None)
    pc_cmds = [c for c in cmds if c["run"] == prog_run]
    if ex and ex.get("tipo") == "programma" and pc_cmds:
        r = {"id": "prep", "nome": "Preparazione e invio", "descr": "PoleDesk: invio di script e sequenze, pulizia, comando di avvio, controllo",
             "livello": 0, "owner": "server", "cmd_previsto": "", "cmd_inviato": "", "avvisi": [], "dur_prev": None, "ha_eventi": True,
             "atteso": "tutti i comandi ricevuti e verificati (avvio: «Avvii», controllo: «piani in attesa: 1»)", "effettivo": "", "errore": "", "causa": ""}
        f = [c for c in pc_cmds if c.get("stato") == "fallito"]
        pend = [c for c in pc_cmds if c.get("stato") in ("preparato", "inviato")]
        r["inizio"] = min((c["fasi"].get("inviato") or c["fasi"].get("preparato") or 0) for c in pc_cmds) if pc_cmds else None
        r["fine"] = max((max(c["fasi"].values()) for c in pc_cmds), default=None) if not pend else None
        r["stato"] = "fallito" if f and not any(c.get("continua") for c in f) else "in esecuzione" if pend else "completato" if pc_cmds else "in attesa"
        r["effettivo"] = f"{sum(1 for c in pc_cmds if c.get('stato') in ('verificato', 'ricevuto'))}/{len(pc_cmds)} comandi riusciti"
        if f:
            r["errore"] = "; ".join(f"{c.get('comando', '')[:80]}: {c.get('esito', '')}" for c in f)
        rows.append(r)

    # 1. piano
    if ex and ex.get("tipo") == "programma" or "piano" in by:
        rp = row("piano", "Piano sulla colonnina", "processo che attende gli orari e lancia le sequenze in fila", 0,
                 atteso="processo del piano attivo fino all'ultimo avvio", owner="piano")
        if vivo["piano"]:
            rp["effettivo"] = f"processo {V['piano']['pid']} attivo"

    # 2. avvii e sequenze
    avvii = piano.get("avvii") or []
    if sched.get("AVVIO"):
        ore = [a.split(" ")[0] + " " + a.split(" ")[1] for a in sched["AVVIO"]]
        avvii = [{"ora": o, "seq": (avvii[i]["seq"] if i < len(avvii) else (avvii[-1]["seq"] if avvii else []))} for i, o in enumerate(ore)]
    passi = piano.get("passi") or {}
    if not avvii and evs:      # esecuzione senza piano noto (es. avviata da un'altra postazione): dagli eventi
        ks = sorted({int(re.match(r"A(\d+)", e["step"]).group(1)) for e in evs if re.match(r"A\d+", e["step"])})
        for k in ks:
            ss = sorted({int(x) for x in re.findall(r"^A%d\.S(\d+)" % k, "\n".join(e["step"] for e in evs), re.M)})
            avvii.append({"ora": "", "seq": [f"S{j}" for j in ss]})
    cursor = None
    for k, av in enumerate(avvii, 1):
        Ak = f"A{k}"
        try:
            t_av = datetime.strptime(av["ora"], "%Y-%m-%d %H:%M").timestamp() if av.get("ora") else None
        except ValueError:
            t_av = None
        ra = row(Ak, f"Avvio {k}" + (f" · {av['ora'][8:10]}/{av['ora'][5:7]} {av['ora'][11:]}" if av.get("ora") else ""),
                 ", ".join(av["seq"]), 0, atteso=("avvio alle " + av["ora"][11:]) if av.get("ora") else "", owner="piano")
        ra["prev_inizio"] = t_av
        cursor = max(t_av or 0, cursor or 0) or None
        for j, sn in enumerate(av["seq"], 1):
            S = f"{Ak}.S{j}"
            ps = passi.get(sn) or []
            first_av = k == 1 and j == 1
            if f"{S}.pausa" in by or (j > 1 and piano.get("pausa_min") and not piano.get("parti_sempre_pausa0")):
                rr = row(f"{S}.pausa", "Pausa tra le sequenze", f"{piano.get('pausa_min', '?')} min senza comandi", 2,
                         atteso=f"attesa di {piano.get('pausa_min', '?')} min dalla fine della prova precedente", dur=(piano.get("pausa_min") or 0) * 60, owner="piano")
                m_ = re.search(r"fino alle (\d\d:\d\d)", " ".join(e["det"] for e in by.get(f"{S}.pausa", [])))
                if m_ and rr["inizio"]:
                    d0 = datetime.fromtimestamp(rr["inizio"])
                    fino = d0.replace(hour=int(m_.group(1)[:2]), minute=int(m_.group(1)[3:]), second=0)
                    if fino.timestamp() < rr["inizio"] - 60:
                        fino += timedelta(days=1)
                    rr["prev_fine"] = fino.timestamp()
                elif cursor:
                    rr["prev_inizio"], rr["prev_fine"] = cursor, cursor + (piano.get("pausa_min") or 0) * 60
                if rr.get("prev_fine"):
                    cursor = rr["prev_fine"]
            rs = row(S, f"Sequenza {sn}", f"{len(ps)} passi, {_dur(sum(p['s'] for p in ps))}" if ps else "", 1,
                     atteso="prova completata e valida", dur=sum(p["s"] for p in ps) or None)
            if rs["stato"] == "completato":
                rs["effettivo"] = rs["det"]
            rr = row(f"{S}.pre", "Condizioni di partenza", "pagina leggibile, spazio, accensione, stato colonnina", 2,
                     atteso="parti sempre: solo pagina e spazio" if piano.get("parti_sempre") else "tutte le condizioni rispettate", dur=5)
            if piano.get("base_min") or f"{S}.base" in by:
                rr = row(f"{S}.base", "Base stabile", f"AUX entro {piano.get('stable_tol', '?')} °C per {piano.get('base_min', '?')} min", 2,
                         atteso=f"base stabile entro {piano.get('pre_wait_min', '?')} min", dur=(piano.get("base_min") or 0) * 60)
                rr["max_s"] = (piano.get("pre_wait_min") or 60) * 60
            row(f"{S}.tc0", "Controllo termico automatico disattivato", "TempContEn a 0 e verifica (rilettura nel log di PS4GAux)", 2,
                prev_cmd="05;00;TempContEn;0", atteso="TempContEn letto 0", dur=5)
            t_seq = (rs["inizio"] or None)
            p1 = next((e["t"] for e in by.get(f"{S}.P1", []) if e["stato"] == "avviato"), None)
            cur = p1 or ((cursor + (piano.get("base_min") or 0) * 60) if cursor else None)
            for i, p in enumerate(ps or [None] * len([x for x in by if x.startswith(S + ".P")]), 1):
                nm = p["nome"] if p else f"Passo {i}"
                h, fn = (p["h"], p["f"]) if p else (None, None)
                rp = row(f"{S}.P{i}", f"Passo {i}/{len(ps) or '?'} · {nm}", (p or {}).get("ruolo", ""), 3,
                         prev_cmd=f"07;0{fn};0{h};01;00;01" if p else "",
                         atteso=(f"scaldiglia {'ON' if h else 'OFF'} · ventola {'ON' if fn else 'OFF'} per {_dur(p['s'])}" if p else ""),
                         dur=(p or {}).get("s"))
                rp["h"], rp["f"] = h, fn
                fe = _kv(next((e["det"] for e in by.get(rp["id"], []) if e["stato"] == "avviato"), ""), "fine")
                rp["prev_inizio"] = rp["inizio"] or cur
                rp["prev_fine"] = int(fe) if fe.isdigit() else ((rp["prev_inizio"] + p["s"]) if (rp["prev_inizio"] and p) else None)
                cur = rp["fine"] or rp["prev_fine"]
            row(f"{S}.fine", "Chiusura e ripristino", "uscite spente, TempContEn = 1 e verifica; esito della prova", 2,
                prev_cmd="07;00;00;01;00;01 · 05;00;TempContEn;1", atteso="uscite spente, TempContEn = 1 verificato", dur=10)
            cursor = cur or cursor
            if t_seq and rs["fine"]:
                cursor = rs["fine"]

    # 3. continua
    if cont or (ex and ex.get("tipo") == "continua") or (piano and piano.get("continua")):
        rc = {"id": "continua", "nome": "Acquisizione continua", "descr": f"un campione ogni {cont.get('OGNI', '?')} s fino al {cont.get('FINO', '?')}",
              "livello": 0, "owner": "continua", "cmd_previsto": "", "cmd_inviato": "", "avvisi": [], "dur_prev": None, "ha_eventi": True,
              "atteso": "processo attivo, nessun comando alla colonnina", "effettivo": "", "errore": "", "causa": "",
              "inizio": None, "fine": None, "stato": "in esecuzione" if vivo["continua"] else ("interrotto" if cont else "completato")}
        if vivo["continua"]:
            rc["effettivo"] = f"processo {V['continua']['pid']} attivo"
        rows.append(rc)

    # 4. vivacità dei processi, blocchi, dati fermi
    csv_age = (now - int(m["MON_CSV_T"])) if m.get("MON_CSV_T") else None
    batt = int((F.get("battito") or {}).get("_", 0) or 0)
    batt_age = (now - batt) if batt else None
    test_alive = vivo["prova"]
    plan_alive = vivo["piano"]
    unreach = bool(res.get("err")) or stale
    dead = {}
    if not unreach and m.get("MON_VECCHIO") is None:
        if sched.get("PID") and not plan_alive:
            dead["piano"] = _death(m, "piano", sched, batt)
        if (inc or F.get("lock")) and not test_alive:
            dead["prova"] = _death(m, "prova", inc or {"PID": F.get("lock", {}).get("_", "")}, int(m.get("MON_CSV_T") or 0) or None)
        if cont.get("PID") and not vivo["continua"]:
            dead["continua"] = _death(m, "continua", cont, None)
    for r in rows:
        if r["stato"] != "in esecuzione":
            continue
        if unreach:
            r["stato"] = "non verificabile"
            r["causa"] = "colonnina non raggiungibile: stato reale sconosciuto" + (f" (ultimo stato letto alle {_hms(res.get('ultimo_t'))})" if res.get("ultimo_t") else "")
            continue
        if r["owner"] == "server":
            continue
        own_alive = test_alive if r["owner"] == "prova" else plan_alive if r["owner"] == "piano" else vivo["continua"]
        if r["owner"] == "prova" and not test_alive and not inc and not F.get("lock"):
            # prova finita ma l'evento di chiusura manca: chiusa di colpo (TERM non arrivato a finish)
            r["stato"], r["errore"] = "interrotto", "il processo della prova non c'è più e non ha scritto la chiusura"
            continue
        if not own_alive:
            r["stato"] = "interrotto"
            d = dead.get(r["owner"])
            r["errore"] = "processo terminato senza chiusura"
            r["causa"] = (d[1] if d else "")
            continue
        el = now - (r["inizio"] or now)
        lim = None
        if r.get("prev_fine"):
            lim = r["prev_fine"] + 60
        elif r.get("max_s"):
            lim = (r["inizio"] or now) + r["max_s"] + 300
        elif r.get("dur_prev"):
            lim = (r["inizio"] or now) + r["dur_prev"] + 120
        if r["owner"] == "prova" and r["livello"] >= 2 and csv_age is not None and csv_age > max(60, 6 * INTERVAL):
            r["stato"], r["errore"] = "bloccato", f"nessun campione nuovo da {_dur(csv_age)} (il file dati non cresce)"
        elif r["owner"] == "piano" and r["livello"] >= 2 and batt_age is not None and batt_age > 240:
            r["stato"], r["errore"] = "bloccato", f"il piano non dà segni di vita da {_dur(batt_age)}"
        elif lim and now > lim and r["livello"] >= 2:
            r["stato"], r["errore"] = "bloccato", f"durata superata: doveva finire alle {_hms(lim - 60 if r.get('prev_fine') else lim, True)}"
        if r["stato"] == "in esecuzione" and r["id"].rsplit(".", 1)[-1].startswith("P") and r.get("h") is not None:
            hh, ff = _onoff_s(m.get("PAG_HEATER")), _onoff_s(m.get("PAG_FAN"))
            r["effettivo"] = (f"ora: scaldiglia {m.get('PAG_HEATER', '?')} · ventola {m.get('PAG_FAN', '?')} · AUX {m.get('PAG_AUX', '?')} °C · "
                              f"LED {m.get('PAG_LED', '?')} A" if m.get("PAG_OK") == "1" else "pagina della colonnina non leggibile")
            r["uscite_ok"] = None if hh is None or ff is None else (hh == r["h"] and ff == r["f"])

    # stati a valle: passi mai iniziati di una sequenza fallita/interrotta (o di un piano terminato) → non eseguiti
    for r in rows:
        if r["stato"] == "in attesa" and r["owner"] != "server" and ("piano" in dead or (not plan_alive and not sched and not unreach
                                                                     and any(e["step"] == "piano" and e["stato"] in ("completato", "interrotto") for e in evs))):
            r["stato"] = "non eseguito"
    for r in rows:
        if r["livello"] >= 2 and r["stato"] == "in attesa":
            S = r["id"].rsplit(".", 1)[0]
            sr = next((x for x in rows if x["id"] == S), None)
            if sr and sr["stato"] in ("fallito", "interrotto", "completato"):
                r["stato"] = "non eseguito"
    for r in rows:
        if r["livello"] == 1 and r["stato"] == "in esecuzione" and any(x["stato"] == "interrotto" and x["id"].startswith(r["id"] + ".") for x in rows):
            r["stato"] = "interrotto"
            r["errore"] = next(x["errore"] for x in rows if x["stato"] == "interrotto" and x["id"].startswith(r["id"] + "."))

    # 5. anomalie
    if unreach:
        A("NON_RAGGIUNGIBILE", "errore" if ex and ex.get("attiva") else "avviso", (next((r["id"] for r in rows if r["stato"] == "non verificabile"), "")),
          "Colonnina non raggiungibile", res.get("err") or "",
          ["accertata" if res.get("err") else "sconosciuta", "il monitor non riesce a leggere la colonnina: " + (res.get("err") or "nessuna risposta")],
          [f"collegamento SSH: {res.get('err')}", f"ultima lettura riuscita: {_hms(res.get('ok_t') or res.get('ultimo_t'), True) or 'mai'}"],
          "Controlla rete/VPN e che la colonnina sia accesa. Il monitor riprova da solo; fino ad allora gli step in corso sono «non verificabili».")
    if m.get("MON_ASSENTE"):
        A("SCRIPT_ASSENTE", "avviso", "", "Script assente sulla colonnina", f"manca {'la cartella' if m['MON_ASSENTE'] == 'cartella' else 'prova_schermo.sh'}",
          ["accertata", "nessuna prova può girare: lo script non c'è"], [], "Avvia o Verifica inviano lo script.", "verifica")
    if m.get("MON_VECCHIO") is not None:
        A("SCRIPT_VECCHIO", "avviso", "", f"Script {m['MON_VECCHIO'] or 'vecchio'} senza monitor",
          "questa versione non scrive gli eventi: il monitor vede solo i file di stato" + (f" ({', '.join(m.get('MON_FILE', []))})" if m.get("MON_FILE") else ""),
          ["accertata", "la colonnina ha una versione dello script precedente alla 2.8.0"], [],
          "Avvia invia la versione nuova. Se c'è un piano in corso, lascialo finire o premi Ferma prima.", "")
    if ex and ex.get("tipo") == "programma":
        bad = [c for c in pc_cmds if c.get("stato") == "fallito"]
        for c in bad:
            A("COMANDO_FALLITO", "avviso" if c.get("continua") else "errore", "prep", f"Comando non riuscito: {c.get('comando', '')[:70]}",
              c.get("esito", ""), ["accertata", c.get("esito", "")], [f"exit {c.get('exit')}", *(c.get("uscita") or [])[-4:]],
              "Leggi l'uscita qui sotto; correggi e rilancia Avvia." if not c.get("continua") else "Passo che può fallire (prima pulizia): nessuna azione.")
        ok_avvio = any(c.get("stato") in ("verificato", "ricevuto") and "Avvii" in (c.get("atteso") or "") for c in pc_cmds)
        started = any(e["step"] == "piano" and e["stato"] in ("avviato", "completato", "interrotto") for e in evs)
        if ok_avvio and not started and not unreach and m.get("MON_VECCHIO") is None and time.time() - ex["creata"] > 60:
            A("AVVIO_NON_ESEGUITO", "errore", "piano", "Comando di avvio inviato ma piano non partito",
              "PoleDesk ha avuto risposta al comando di avvio, ma sulla colonnina non c'è l'evento «piano avviato» di questa esecuzione",
              ["probabile", "il processo del piano non è partito o è stato chiuso subito"],
              [f"file del piano: {'presente, esecuzione ' + sched.get('RUN', '?') if sched else 'assente'}", f"processo del piano: {'attivo' if plan_alive else 'assente'}"],
              "Premi Verifica, poi Avvia di nuovo.", "verifica")
    if sched.get("RUN") and eid and sched["RUN"] != eid:
        A("PIANO_DIVERSO", "avviso", "piano", "Sulla colonnina c'è un altro piano",
          f"piano registrato: esecuzione {sched['RUN']}; questa vista segue {eid}",
          ["accertata", "un Avvia successivo (anche da un'altra postazione) ha sostituito il piano"], [], "Apri l'esecuzione più recente o premi Stato.")
    for role, d in dead.items():
        kind, txt, ver, tag = d
        f_ = {"piano": sched, "prova": inc, "continua": cont}[role]
        step = next((r["id"] for r in sorted(rows, key=lambda x: -x["livello"]) if r["stato"] == "interrotto" and r["owner"] == role), role)
        pl = (m.get("PL") or [""])[-1]
        if role == "piano" and pl:
            ver = ver + [f"ultimo messaggio del piano: «{_ll(pl)[:120]}» (il log si ferma lì: incoerente con lo stato reale)"]
        az = {"riavvio": "Il riavvio cancella il piano: premi Ferma (ripristino) e poi Avvia.",
              "sessione": "Premi Ferma (ripristino) e poi Avvia. Il piano gira dentro il collegamento SSH di PoleDesk: con il monitor automatico attivo "
                          "il collegamento resta aperto; se PoleDesk si chiude o la rete cade, systemd chiude anche il piano.",
              "memoria": "Premi Ferma e poi Avvia; se si ripete, controlla la memoria libera della colonnina.",
              }.get(tag, "Premi Ferma (ripristina uscite e TempContEn) e poi Avvia.")
        A("PROCESSO_TERMINATO", "errore", step, {"piano": "Piano terminato inaspettatamente", "prova": "Prova terminata inaspettatamente",
                                                  "continua": "Acquisizione continua terminata inaspettatamente"}[role],
          f"processo {f_.get('PID', '?')} registrato ma non più attivo", [kind, txt], ver, az, "ferma")
    if (inc or F.get("lock")) and not test_alive and not unreach:
        A("PROVA_A_META", "errore", next((r["id"] for r in sorted(rows, key=lambda x: -x["livello"]) if r["stato"] == "interrotto" and r["owner"] == "prova"), ""),
          "Prova lasciata a metà: uscite e controllo termico da ripristinare",
          "la prova non ha fatto la chiusura: la scaldiglia o la ventola possono essere rimaste accese e TempContEn a 0",
          ["accertata", "file della prova in corso presente senza processo"], ["file .prova_in_corso / .lock presenti", "processo della prova assente"],
          "Premi Ferma: spegne le uscite, rimette TempContEn = 1 e lo verifica.", "ferma")
    elif F.get("tc_disattivo") and not test_alive and not unreach:
        A("TC_DISATTIVO", "errore", "", "Controllo termico automatico ancora disattivato",
          f"segnale .tc_disattivo presente ({F['tc_disattivo'].get('_', '')}) senza prova in corso",
          ["accertata", "una prova ha messo TempContEn a 0 e non risulta il ripristino"], [], "Premi Ferma (ripristino verificato).", "ferma")
    for r in rows:
        if r["stato"] == "bloccato":
            ver = []
            if csv_age is not None:
                ver.append(f"ultimo campione nel file dati: {_dur(csv_age)} fa")
            ver.append("pagina web della colonnina: " + ("leggibile" if m.get("PAG_OK") == "1" else "NON leggibile"))
            if m.get("MON_AUXLOG_T"):
                ver.append(f"log di PS4GAux aggiornato {_dur(now - int(m['MON_AUXLOG_T']))} fa")
            ver.append(f"processo {'della prova' if r['owner'] == 'prova' else 'del piano'}: attivo")
            cz = ["probabile", "la pagina web non risponde: la prova attende la lettura"] if m.get("PAG_OK") != "1" else ["sconosciuta", "processo vivo ma fermo"]
            A("BLOCCATO", "errore", r["id"], f"Step bloccato: {r['nome']}", r["errore"], cz, ver,
              "Aspetta un paio di letture; se resta bloccato premi Ferma (ripristino) e rilancia.", "ferma")
        if r.get("uscite_ok") is False and r["inizio"] and now - r["inizio"] > 30:
            A("USCITE_NON_CONFORMI", "avviso", r["id"], "Uscite diverse da quelle comandate",
              f"comandate: {r['atteso']}; lette ora: scaldiglia {m.get('PAG_HEATER')} · ventola {m.get('PAG_FAN')}",
              ["probabile", "controllo termico automatico tornato attivo o servizio AUX ripartito: lo script rinvia da solo TempContEn 0 e il comando del passo"],
              [f"pagina letta alle {_hms(now, True)}", f"rinvii già fatti in questo passo: {sum('uscite lette' in a for a in r['avvisi'])}"],
              "Nessuna azione: lo script corregge da solo. Se si ripete a ogni passo, la prova risulterà con avvisi.")
        for a in r["avvisi"]:
            if "barra LED" in a:
                A("LED_VARIATA", "avviso", r["id"], "Barra LED cambiata durante la prova", a,
                  ["accertata", "la corrente della barra LED è cambiata: scalda quanto la scaldiglia"], [], "La prova sarà non valida: va ripetuta con la barra LED ferma.")
        if r["id"].endswith(".tc0") and r["stato"] in ("fallito", "non verificabile"):
            A("TEMPCONTEN", "errore" if r["stato"] == "fallito" else "avviso", r["id"],
              "TempContEn non disattivabile" if r["stato"] == "fallito" else "TempContEn a 0 non verificato", r["det"],
              ["accertata", r["det"]], ["lettura dal log di PS4GAux eseguita dallo script durante la prova (il monitor non legge TempContEn)"],
              "Controlla il servizio PS4GAux della colonnina." if r["stato"] == "fallito" else "Nessuna azione: la prova continua.")
        if r["id"].endswith(".fine") and r["stato"] in ("fallito", "non verificabile"):
            A("RIPRISTINO", "errore" if r["stato"] == "fallito" else "avviso", r["id"],
              "Ripristino del controllo termico non riuscito" if r["stato"] == "fallito" else "Ripristino non verificato", r["det"],
              ["accertata", r["det"]], [], "Premi Ferma: ripete il ripristino con verifica." if r["stato"] == "fallito" else "Puoi premere Verifica per controllarlo.",
              "ferma" if r["stato"] == "fallito" else "verifica")
        if r["livello"] == 1 and r["stato"] == "fallito":
            mot = r["det"]
            sug = ("Aumenta l'attesa massima o la tolleranza della base, oppure usa «parti sempre»." if "base" in mot else
                   "La colonnina era in uso: riprova più tardi." if "USER STATUS" in mot else
                   "La colonnina è accesa da poco: aspetta o abbassa «accesa da almeno»." if "accesa da" in mot else
                   "Controlla la pagina web della colonnina." if "pagina" in mot else "Correggi la condizione e rilancia.")
            A("NON_PARTITA", "avviso", r["id"], f"{r['nome']} non partita", mot, ["accertata", mot], [], sug)
        if r["livello"] == 0 and r["stato"] == "saltato":
            A("SALTATO", "avviso", r["id"], f"{r['nome']} saltato", r["det"], ["accertata", r["det"]], [],
              "Allunga l'intervallo tra gli avvii o accorcia le pause.")
    np_ = sum(1 for p in m.get("PR", []) if p.split(" ")[1:2] == ["piano"])
    if np_ > 1:
        A("DOPPIONI", "errore", "piano", f"{np_} piani in attesa sulla stessa colonnina", "più processi del piano: partirebbero prove sovrapposte",
          ["accertata", "processi del piano doppi"], m.get("PR", []), "Premi Ferma e poi Avvia una volta sola.", "ferma")
    if vivo["guardiano"] and not test_alive and not unreach:
        A("GUARDIANO_ORFANO", "avviso", "", "Guardiano a tempo senza prova",
          f"processo {V['guardiano']['pid']} di una prova già chiusa", ["accertata", "guardiano rimasto da una prova chiusa di colpo"], [],
          "Innocuo (interviene solo sulla sua prova); Ferma lo chiude.", "ferma")
    if m.get("PAG_OK") == "0" and not unreach and (test_alive or plan_alive):
        A("PAGINA_MUTA", "errore" if test_alive else "avviso", "", "Pagina web della colonnina non leggibile",
          "lo script legge i dati dalla pagina locale: senza pagina la prova si interrompe dopo 30 s",
          ["accertata", "lettura della pagina locale fallita"], [], "Controlla il servizio web della colonnina.")
    if m.get("MON_DF_KB", "").isdigit() and int(m["MON_DF_KB"]) < 5000:
        A("SPAZIO", "avviso", "", "Spazio libero insufficiente", f"{m['MON_DF_KB']} kB liberi", ["accertata", "disco quasi pieno"], [],
          "Raccogli e valuta (cancella gli archivi scaricati) o libera spazio.", "raccogli")
    if abs(off) > 120 and not unreach:
        A("OROLOGIO", "avviso", "", "Orologio della colonnina diverso da quello del PC", f"differenza {off:+d} s",
          ["accertata", "gli orari degli avvii seguono l'orologio della colonnina"], [], "Nessuna azione; tienine conto negli orari.")
    if m.get("MON_VERSIONE") and m.get("MON_VERSIONE") != _camp_ver():
        A("SCRIPT_DIVERSO", "info", "", f"Script {m['MON_VERSIONE']} sulla colonnina, PoleDesk ha la {_camp_ver()}", "",
          ["accertata", "versione diversa"], [], "Il prossimo Avvia o Verifica invia quella di PoleDesk.")
    passo = (F.get("passo") or {}).get("_", "")
    cur_ev = next((r for r in rows if r["stato"] == "in esecuzione" and r["livello"] == 3), None)
    if passo and cur_ev and test_alive:
        n_ = re.search(r";(\d+)/", passo)
        if n_ and not cur_ev["id"].endswith(f".P{n_.group(1)}"):
            A("INCOERENZA", "avviso", cur_ev["id"], "Passo diverso tra file di stato ed eventi",
              f"file .passo: {passo}; eventi: {cur_ev['id']}", ["sconosciuta", ""], [], "Premi Stato tra qualche secondo.")
    # comandi di PoleDesk riusciti ma stato opposto (es. Ferma ok e processi ancora vivi)
    fer = [c for c in cmds if c.get("azione") == "pulisci" and c.get("stato") == "verificato"]
    if fer and (plan_alive or test_alive) and fer[-1]["fasi"].get("verificato", 0) > max([e["t"] for e in evs if e["step"] == "piano" and e["stato"] == "avviato"] or [0]):
        A("INCOERENZA", "errore", "", "Ferma riuscita ma processi ancora attivi",
          "il comando di pulizia ha risposto «Colonnina pulita» ma il monitor vede processi attivi", ["sconosciuta", ""],
          m.get("PR", []), "Premi Ferma di nuovo, poi Verifica.", "ferma")

    # 6. riepilogo (stessa fonte per Stato, monitor e pagina Campo)
    cur = next((r for r in rows if r["stato"] in ("in esecuzione", "bloccato") and r["livello"] == 3), None) or \
        next((r for r in rows if r["stato"] in ("in esecuzione", "bloccato") and r["livello"] == 2), None) or \
        next((r for r in rows if r["stato"] in ("in esecuzione", "bloccato") and r["livello"] <= 1 and r["id"] not in ("piano", "continua", "prep")), None)
    nxt = next((r for r in rows if r["livello"] == 0 and r["id"].startswith("A") and r["stato"] == "in attesa"), None)
    gravi = [a for a in anom if a["gravita"] == "errore"]
    if unreach:
        stato, det = "non raggiungibile", res.get("err", "")
    elif m.get("MON_VECCHIO") is not None:
        stato, det = "da verificare (script vecchio)", ("file di stato: " + ", ".join(m.get("MON_FILE", []))) if m.get("MON_FILE") else "nessun file di stato"
    elif m.get("MON_ASSENTE"):
        stato, det = "libera", "script assente"
    elif any(a["codice"] in ("PROCESSO_TERMINATO", "PROVA_A_META") for a in gravi):
        stato, det = "interrotta in modo anomalo", next(a["causa"][1] for a in gravi if a["codice"] in ("PROCESSO_TERMINATO", "PROVA_A_META"))
    elif cur and cur["stato"] == "bloccato":
        stato, det = "bloccata", f"{cur['nome']}: {cur['errore']}"
    elif cur and cur["id"].endswith(".pausa"):
        stato, det = "in pausa tra le sequenze", f"fino alle {_hms(cur.get('prev_fine'))}" if cur.get("prev_fine") else ""
    elif cur and cur["id"].endswith(".base"):
        stato, det = "in attesa della base stabile", f"da {_dur(now - (cur['inizio'] or now))}"
    elif cur:
        S = cur["id"].split(".P")[0] if ".P" in cur["id"] else cur["id"].rsplit(".", 1)[0]
        sr = next((r for r in rows if r["id"] == S), None)
        stato, det = "in corso", f"{(sr or {}).get('nome', '')} · {cur['nome']}" + (f" fino alle {_hms(cur.get('prev_fine'))}" if cur.get("prev_fine") else "")
    elif plan_alive and nxt:
        stato, det = "programmata", f"prossimo avvio {nxt['nome'].split('·')[-1].strip()}"
    elif plan_alive:
        stato, det = "programmata", ""
    else:
        last = [r for r in rows if r["livello"] == 1 and r["stato"] in ("completato", "fallito", "interrotto")]
        lr = max(last, key=lambda r: r["fine"] or 0) if last else None
        if lr:
            es = lr["det"]
            stato = ("libera · ultima non partita" if lr["stato"] == "fallito" else "libera · ultima interrotta" if lr["stato"] == "interrotto"
                     else "libera · ultima non valida" if "non valida" in es else "libera · ultima completata")
            det = f"{lr['nome']} {_hms(lr['fine'])}: {es}"
        else:
            stato, det = ("libera", "") if m.get("MON_ORA") else ("sconosciuto", "")
        pr = next((r for r in rows if r["id"] == "piano"), None)
        if pr and pr["stato"] == "interrotto" and (not lr or (pr.get("fine") or now) >= (lr["fine"] or 0)):
            stato = "libera · piano interrotto"
            hc = next((h["causa"][1] for h in ((ex or {}).get("anomalie") or {}).values() if h.get("codice") == "PROCESSO_TERMINATO" and h.get("causa")), "")
            det = (hc or pr.get("errore") or pr.get("det") or "") + (f" — {det}" if det else "")
    passi_tot = [r for r in rows if r["livello"] == 3]
    fatti = [r for r in passi_tot if r["stato"] == "completato"]
    cont_txt = (f"acquisizione continua attiva: ogni {cont.get('OGNI')} s fino al {cont.get('FINO')}" if vivo["continua"] else
                "acquisizione continua interrotta" if cont else "acquisizione continua spenta")
    for r in rows:
        t0 = r["inizio"] or None
        t1 = r["fine"] or (now if r["stato"] in ("in esecuzione", "bloccato") else None)
        r["trascorso"] = _dur(t1 - t0) if t0 and t1 else ""
    # storico delle anomalie (prima vista, ancora presente o risolta)
    hist = []
    if ex:
        with _EX_LOCK:
            ex2 = ex_load(ex["id"]) or ex
            H = ex2.setdefault("anomalie", {})
            seen = set()
            for a in anom:
                k = f"{a['codice']}|{a['step']}"
                seen.add(k)
                h = H.get(k) or {"prima": time.time()}
                h.update(a, ultima=time.time(), attiva=True)
                H[k] = h
                a["prima"] = h["prima"]
            for k, h in H.items():
                if k not in seen and h.get("attiva") and not unreach:
                    h["attiva"], h["risolta"] = False, time.time()
            hist = sorted([h for h in H.values() if not h.get("attiva")], key=lambda h: -h.get("risolta", 0))[:10]
            ex2["ultimo"] = {"t": time.time(), "stato": stato, "dettaglio": det}
            ex_save(ex2)
            ex = ex2
    return {"station_id": sid, "colonnina": st.get("name", ""), "esecuzione": eid, "attiva": bool(ex and ex.get("attiva")),
            "tipo": (ex or {}).get("tipo", ""), "creata": (ex or {}).get("creata"), "stato": stato, "dettaglio": det, "continua": cont_txt,
            "letto": res.get("t"), "ok_t": res.get("ok_t"), "errore": res.get("err", ""), "orologio": off, "uptime": m.get("MON_UPTIME"),
            "script": m.get("MON_VERSIONE") or m.get("MON_VECCHIO"), "corrente": cur and cur["id"],
            "avanzamento": [len(fatti), len(passi_tot)], "step": rows, "anomalie": anom, "risolte": hist,
            "comandi": [{**c, "fasi": {k: _hms(v, True) for k, v in c["fasi"].items()}} for c in cmds[-60:]],
            "pagina": {k[4:].lower(): m.get(k) for k in ("PAG_OK", "PAG_AUX", "PAG_TEMP", "PAG_HEATER", "PAG_FAN", "PAG_LED", "PAG_USER", "PAG_FAULT")},
            "processi": m.get("PR", []), "tecnico": {"eventi": [f"{_hms(e['t'], True)} {e['step']} {e['stato']} {e['det']}" for e in evs[-80:]],
                                                    "piano_log": m.get("PL", []), "prova_log": m.get("RL", []), "journal": m.get("JR", []) + m.get("JU", []),
                                                    "kernel": m.get("DM", []), "guardia": m.get("GL", [])}}


def _camp_ver():
    try:
        return _camp_script()[1]
    except ValueError:
        return "?"


def _mon_store_stato(sid, v):
    """Lo Stato delle pagine viene dal monitor: una sola fonte per badge, monitor e tabella del Campo."""
    with LOCK:
        d = load(F_SC_STATO, {})
        prev = d.get(sid) or {}
        d[sid] = {"stato": v["stato"], "dettaglio": v["dettaglio"], "ora": datetime.now().strftime("%Y-%m-%d %H:%M"), "colonnina": v["colonnina"],
                  "continua": v["continua"], "anomalie": sum(1 for a in v["anomalie"] if a["gravita"] in ("errore", "avviso")),
                  "gravi": sum(1 for a in v["anomalie"] if a["gravita"] == "errore"), "esecuzione": v["esecuzione"],
                  "passo": next((r["nome"] for r in v["step"] if r["id"] == v["corrente"]), ""), "fonte": "monitor"}
        if prev.get("stato") != v["stato"]:
            audit(station_by_id(sid) if any(s["id"] == sid for s in stations()) else None, "monitor: stato",
                  f"{v['colonnina']}: {prev.get('stato', '—')} → {v['stato']}" + (f" ({v['dettaglio'][:120]})" if v["dettaglio"] else ""), v["stato"] not in (
                      "interrotta in modo anomalo", "bloccata", "non raggiungibile"), v["esecuzione"])
        save(F_SC_STATO, d)


def sc_monitor(body):
    """Vista del monitor per le colonnine indicate (e per quelle con un'esecuzione attiva della pagina). aggiorna=True: legge adesso."""
    pg = body.get("pagina") or "lab"
    ids = list(dict.fromkeys(body.get("station_ids") or []))
    att = load(F_SC_ATTIVE, {})
    for sid, eid in att.items():
        ex = ex_load(eid)
        if ex and ex.get("pagina") == pg and sid not in ids:
            ids.append(sid)
    known = {s["id"] for s in stations()}
    ids = [i for i in ids if i in known][:200]
    if body.get("aggiorna"):
        with ThreadPoolExecutor(max_workers=max(1, min(20, len(ids) or 1))) as exr:
            views = list(exr.map(lambda s: sc_monitor_poll(s, body.get("esecuzione") if len(ids) == 1 else None), ids))
    else:
        views = []
        for sid in ids:
            res = MON_CACHE.get(sid)
            if res is None:
                views.append({"station_id": sid, "colonnina": (station_by_id(sid) or {}).get("name", ""), "stato": "", "mai_letto": True,
                              "esecuzione": att.get(sid, ""), "step": [], "anomalie": [], "risolte": [], "comandi": []})
            else:
                views.append(sc_monitor_view(sid, ex_load(att.get(sid)) if att.get(sid) else None, res))
    c = sc_cfg(pg)
    return {"colonnine": views, "monitor": {"attivo": c.get("monitor", True), "ogni_s": c.get("monitor_s")}, "ora": time.time()}


def sc_exec_list(body):
    sid = body.get("station_id")
    out = []
    for f in sorted(SC_EXEC.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:300]:
        if f.name == "attive.json":
            continue
        x = load(f, None)
        if x and (not sid or x.get("station_id") == sid):
            out.append({k: x.get(k) for k in ("id", "colonnina", "pagina", "tipo", "nome", "creata", "attiva", "chiusa", "esito_finale", "ultimo")})
    return {"esecuzioni": out[:100]}


def sc_monitor_loop():
    """Monitor automatico: legge le colonnine con un'esecuzione attiva all'intervallo impostato nella pagina (solo --monitor)."""
    last = {}
    while True:
        time.sleep(3)
        try:
            for sid, eid in list(load(F_SC_ATTIVE, {}).items()):
                ex = ex_load(eid)
                if not ex:
                    _ex_set_attiva(sid, None)
                    continue
                c = sc_cfg(ex.get("pagina") or "lab")
                if not c.get("monitor", True) or sid in MON_BUSY:
                    continue
                if time.time() - last.get(sid, 0) < max(10, int(c.get("monitor_s") or 30)):
                    continue
                last[sid] = time.time()
                MON_BUSY.add(sid)

                def go(s=sid, e=eid):
                    try:
                        sc_monitor_poll(s, e)
                    except Exception as err:
                        MON_CACHE.setdefault(s, {})["loop_err"] = describe_error(err)
                    finally:
                        MON_BUSY.discard(s)
                EXEC.submit(go)
        except Exception:
            pass


# ---------------- viste per le due pagine

_ORD = {"staccato (diagnostica)": 0, "staccato": 1, "sospetto": 2, "incerto": 3, "non valida": 4, "interrotta": 5, "non partita": 6,
        "solo indicatori": 7, "taratura": 8, "attaccato": 9}


def sc_mass_view():
    cp, prove, diag, stato = sc_campo(), load(F_SC_PROVE, {}), load(F_SC_DIAG, {}), load(F_SC_STATO, {})
    prove_unite()
    dec = _UNI["dec"] or {}
    sts = stations()
    idc = cp.get("id_cuid", {})
    by = {}
    for it in prove.values():
        if not it.get("test"):
            by.setdefault(it.get("cuid", ""), []).append(it)
    rows = []
    for x in cp["colonnine"]:
        s = x["seriale"]
        lk = (cp.get("link") or {}).get(s)
        st = next((x for x in sts if x["id"] == lk), None) if lk else None
        st = st or _match_station(s, sts, idc)
        tests = sorted(by.get(s, []), key=lambda i: i.get("inizio") or 0)
        notte = tests[-1]["notte"] if tests else ""
        tn = [i for i in tests if i["notte"] == notte]
        main = next((i for i in reversed(tn) if i.get("seq") in sc_soglie()["regole"]), tn[-1] if tn else None)
        dg = diag.get(s) or (diag.get(st["name"]) if st else None)
        es, conf, mot = (main or {}).get("esito", ""), (main or {}).get("confidenza", ""), (main or {}).get("motivo", "")
        if main and main.get("file") in dec:            # esito dal motore unico (stessa regola di Analisi e Conclusioni)
            dd = dec[main["file"]]["d"]
            es, conf, mot = dd["esito"], f"{dd['n_vere']}/{dd['n_calc']}" if dd["n_calc"] else "", " · ".join(dd["catena"])
        # la diagnostica è solo informativa: l'esito viene sempre dalla prova termica
        rows.append({**x, "station_id": st["id"] if st else "", "colonnina": st["name"] if st else "",
                     "display_ip": ((st or {}).get("vars") or {}).get("display_ip", ""),
                     "stato": stato.get(st["id"]) if st else None, "diagnosi": dg, "notte": notte,
                     "esito": es, "confidenza": conf, "motivo": mot, "prova": main,
                     "altre": [{k: i.get(k) for k in ("seq", "esito", "confidenza", "motivo", "ind", "file", "dir", "Text", "valida")} for i in tn if i is not main]})
    rows.sort(key=lambda r: (_ORD.get(r["esito"], 9), r["seriale"]))
    return {"colonnine": rows, "cfg": sc_cfg("mass"), "soglie": sc_soglie(), "sequenze": sc_sequenze(), "file_soglie": str(F_SC_SOGLIE)}


def sc_lab_view():
    prove = load(F_SC_PROVE, {})
    prove_unite()
    dec = _UNI["dec"] or {}
    for i in prove.values():
        u = dec.get(i.get("file"))
        if u:
            i.update(esito=u["d"]["esito"], confidenza=f"{u['d']['n_vere']}/{u['d']['n_calc']}" if u["d"]["n_calc"] else "", motivo=" · ".join(u["d"]["catena"]),
                     scattate=[c["condizione"] for c in u["d"]["condizioni"] if c.get("vera")], luogo=u["luogo"], tipo=u["tipo"])
    items = sorted((i for i in prove.values() if i.get("file")), key=lambda i: i.get("inizio") or 0, reverse=True)
    cont = sorted(({"dir": f"{p.parent.parent.name}/continua", "file": p.name, "cuid": p.parent.parent.name, "giorno": p.stem.rsplit("_", 1)[-1],
                    "kb": round(p.stat().st_size / 1024)} for p in SC_DATI.glob("*/continua/*.csv")), key=lambda x: (x["giorno"], x["cuid"]), reverse=True)[:200]
    return {"cfg": sc_cfg("lab"), "sequenze": sc_sequenze(), "soglie": sc_soglie(), "file_soglie": str(F_SC_SOGLIE), "continua": cont,
            "stato": load(F_SC_STATO, {}), "prove": [{k: i.get(k) for k in ("file", "dir", "cuid", "colonnina", "seq", "nome", "inizio", "notte", "test", "valida", "extra",
                                                                           "motivi_validita", "ind", "misure", "Text", "vento", "esito", "confidenza", "motivo", "scattate", "luogo", "tipo")}
                                                     for i in items[:600]]}


def _f(v, n=3):
    return "" if v is None else f"{v:.{n}f}".replace(".", ",")


def sc_mass_export():
    v = sc_mass_view()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    keys = ("I", "IA", "tau", "R", "rimbalzo", "In", "Q", "A", "salita", "C", "deriva", "deriva2")
    w.writerow(["seriale", "indirizzo", "latitudine", "longitudine", "colonnina", "notte", "esito", "confidenza", "motivi", "diagnostica",
                "sequenza", *keys, "Text", "file_dati", "altre_sequenze", "stato_verificato", "data_verifica", "fonte_verifica"])
    for r in v["colonnine"]:
        p = r.get("prova") or {}
        ind = p.get("ind") or {}
        altre = " | ".join(f"{a['seq']}: {a.get('esito', '')} " + " ".join(f"{k}={_f((a.get('ind') or {}).get(k))}" for k in keys[:7]) for a in r["altre"])
        ver = r.get("verificato") or {}
        w.writerow([r["seriale"], r.get("indirizzo", ""), _f(r.get("lat"), 6), _f(r.get("lon"), 6), r["colonnina"], r["notte"], r["esito"], r["confidenza"],
                    r["motivo"], (r.get("diagnosi") or {}).get("esito", ""), p.get("seq", ""), *[_f(ind.get(k)) for k in keys], _f(p.get("Text"), 1),
                    f"{p.get('dir', '')}/{p.get('file', '')}" if p.get("file") else "", altre, ver.get("stato", ""), ver.get("data", ""), ver.get("fonte", "")])
    return buf.getvalue()


def sc_lab_export():
    v = sc_lab_view()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    keys = ("I", "IA", "tau", "R", "rimbalzo", "In", "Q", "A", "salita", "C", "deriva", "deriva2")
    w.writerow(["notte", "inizio", "colonnina", "seriale", "sequenza", "nome", "valida", "esito", "confidenza", *keys, "Text", "motivi", "file"])
    for i in v["prove"]:
        ind = i.get("ind") or {}
        w.writerow([i["notte"], datetime.fromtimestamp(i["inizio"]).strftime("%Y-%m-%d %H:%M") if i.get("inizio") else "", i["colonnina"], i["cuid"], i["seq"], i["nome"],
                    "sì" if i["valida"] else "no", i.get("esito", ""), i.get("confidenza", ""), *[_f(ind.get(k)) for k in keys], _f(i.get("Text"), 1),
                    "; ".join(i.get("motivi_validita") or []) or i.get("motivo", ""), i["file"]])
    return buf.getvalue()


def _camp_script():
    """prova_schermo.sh distribuito con PoleDesk (accanto a server.py), copiato nei file del pacchetto della campagna."""
    for cand in (Path(__file__).resolve().parent / "prova_schermo.sh", BASE / "prova_schermo.sh", DIST_FILES / "ps-installa" / "prova_schermo.sh"):
        if cand.exists():
            data = cand.read_bytes().replace(b"\r\n", b"\n")
            m = re.search(rb'^VERSIONE="([\d.]+)"', data, re.M)
            return data, (m.group(1).decode() if m else "?")
    raise ValueError("prova_schermo.sh non trovato: mettilo accanto a server.py")


# ---------------------------------------------------------------- HTTP

def route(method, path, q, body):
    if method == "GET":
        if path == "/api/state":
            cfg = settings()
            return {"settings": {**cfg, "default_password": "", "has_default_password": bool(cfg["default_password"])},
                    "stations": [public_station(s) for s in stations()],
                    "actions": load(F_ACTIONS, DEFAULT_ACTIONS), "probes": load(F_PROBES, DEFAULT_PROBES),
                    "procedures": procedures(), "aux": aux_cfg(), "sequences": sequences(), "profile": profile_cfg(), "profile_roles": PROF_ROLES, "profile_pending": load(F_PROF_PENDING, {}),
                    "connections": POOL.count(), "strict_host_keys": F_KNOWN.exists(),
                    "app": APP_NAME, "version": VERSION, "identity": POOL.ident,
                    "jumps": [public_jump(j) for j in jumps()],
                    "local_ips": local_ips()}
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[1])
            if not job:
                raise KeyError("job non trovato")
            with LOCK:
                return json.loads(json.dumps(job))
        if path.startswith("/api/proc/run/"):
            run = RUNS.get(path.rsplit("/", 1)[1])
            if not run:
                raise KeyError("esecuzione non trovata")
            return json.loads(json.dumps(run))
        if path == "/api/profile/runs":
            return {"now": time.time(), "runs": profile_runs()}
        if path == "/api/profile/history":
            return profile_history(q.get("all", ["0"])[0] == "1")
        if path == "/api/profile/campaigns":
            return {"campaigns": campaigns()}
        if path == "/api/prove/legenda":
            return esiti_legenda()
        if path == "/api/prove/perche":
            return prova_perche(q.get("file", [""])[0])
        if path == "/api/profile/series":
            return profile_series(q.get("name", [""])[0])
        if path == "/api/profile/status":
            return {"cuids": profile_status(), "refs": load(F_PROF_REF, {})}
        if path == "/api/seq/runs":
            with LOCK:
                runs = sorted(SEQRUNS.values(), key=lambda r: r["started"], reverse=True)
                return {"now": time.time(), "runs": json.loads(json.dumps(runs))}
        if path == "/api/web/data":
            return web_data(float(q.get("since", ["0"])[0]))
        if path == "/api/monitor/data":
            return mon_data(float(q.get("since", ["0"])[0]))
        if path in ("/api/sc/mass", "/api/sc/lab"):
            try:
                ver = _camp_script()[1]
            except ValueError:
                ver = ""
            kind = path.rsplit("/", 1)[1]
            runs = [r for r in dist_runs() if (DISTRUNS.get(r["id"]) or load(DIST_RUNS / f"{r['id']}.json", {})).get("kind", "").startswith(f"sc-{kind}:")]
            return {**(sc_mass_view() if kind == "mass" else sc_lab_view()), "script": ver, "runs": runs[:10]}
        if path == "/api/dist/state":
            return {"packages": packages(), "runs": dist_runs(), "folder": str(DIST_OUT)}
        if path.startswith("/api/dist/run/"):
            return dist_run(path.rsplit("/", 1)[1])
        if path == "/api/audit":
            n = int(q.get("lines", ["300"])[0])
            lines = deque(F_AUDIT.open(encoding="utf-8"), maxlen=n) if F_AUDIT.exists() else []
            return {"lines": [json.loads(l) for l in lines if l.strip()]}
    if method == "POST":
        if path == "/api/settings":
            cfg = load(F_SETTINGS, {})
            for k in DEFAULT_SETTINGS:
                if k in body and not (k == "default_password" and body[k] == ""):
                    cfg[k] = body[k]
            if body.get("default_password_clear"):
                cfg["default_password"] = ""
            save(F_SETTINGS, cfg)
            return {"ok": True, "note": "Parallelismo e porta valgono dal prossimo avvio"}
        if path == "/api/stations":
            return public_station(upsert_station(body))
        if path == "/api/stations/delete":
            save(F_STATIONS, [s for s in stations() if s["id"] not in body.get("ids", [])])
            for i in body.get("ids", []):
                POOL.drop(i)
            return {"ok": True}
        if path == "/api/stations/import":
            return import_csv(body.get("csv", ""))
        if path == "/api/actions":
            save(F_ACTIONS, body.get("actions", []))
            return {"ok": True}
        if path == "/api/probes":
            save(F_PROBES, body.get("probes", []))
            return {"ok": True}
        if path == "/api/jobs":
            return {"id": start_job(body)}
        if path.startswith("/api/fs/"):
            st, op, p = station_by_id(body["station_id"]), path.rsplit("/", 1)[1], body.get("path", "")
            try:
                if op == "list":
                    return {"path": p, "items": fs_list(st, p)}
                if op == "read":
                    return {"path": p, "content": fs_read(st, p)}
                if op == "write":
                    bak = fs_write(st, p, body.get("content", ""), body.get("backup", True), body.get("create_only", False))
                    audit(st, "scrittura file", p, True, bak)
                    return {"ok": True, "backup": bak}
                if op in ("mkdir", "delete"):
                    fs_simple(st, op, p)
                    audit(st, op, p, True)
                    return {"ok": True}
            except Exception as e:
                if op != "list" and op != "read":
                    audit(st, op, p, False, describe_error(e))
                raise RuntimeError(describe_error(e))
        if path == "/api/monitor/start":
            mon_start(body)
            return {"ok": True}
        if path == "/api/monitor/stop":
            mon_stop()
            return {"ok": True}
        if path == "/api/profile/config":
            cfg = load(F_PROFILE, {})
            for k in PROFILE_DEFAULT:
                if k in body:
                    cfg[k] = body[k]
            save(F_PROFILE, cfg)
            return {"ok": True}
        if path == "/api/profile/clear":
            with LOCK:
                for k in [k for k, r in PROFRUNS.items() if r["done"]]:
                    PROFRUNS.pop(k, None)
                    PROFCTRL.pop(k, None)
            return {"ok": True}
        if path == "/api/profile/recalc":
            return profile_recalc(body)
        if path == "/api/profile/import":
            return profile_import(body)
        if path == "/api/profile/edit":
            return prove_luogo_edit(body)
        if path == "/api/profile/campaign":
            return {"campaigns": campaign_save(body)}
        if path == "/api/profile/meteo":
            return _meteo(profile_cfg(), body.get("lat"), body.get("lon"))
        if path == "/api/profile/delete":
            return profile_delete(body)
        if path == "/api/profile/force":
            return profile_force(body)
        if path == "/api/profile/ref_reset":
            profile_ref_reset(body)
            return {"ok": True}
        if path == "/api/profile/restore":
            return profile_restore(body)
        if path == "/api/profile/start":
            return {"ids": profile_start(body)}
        if path == "/api/profile/stop":
            for k in ([body["id"]] if body.get("id") else [k for k, r in PROFRUNS.items() if not r["done"]]):
                if k in PROFCTRL:
                    PROFCTRL[k]["stop"].set()
            return {"ok": True}
        if path == "/api/jumps":
            return {"jumps": jumps_save(body)}
        if path == "/api/jumps/test":
            return jump_test(body)
        if path == "/api/net/list":
            return {"adapters": net_list()}
        if path == "/api/net/set":
            return {"adapter": net_set(body)}
        if path == "/api/aux/shell":
            return aux_shell(body)
        if path == "/api/sequences":
            save(F_SEQS, body.get("sequences", []))
            return {"ok": True}
        if path == "/api/seq/start":
            return {"ids": seq_start(body)}
        if path == "/api/seq/stop":
            seq_stop(body)
            return {"ok": True}
        if path == "/api/seq/next":
            seq_next(body)
            return {"ok": True}
        if path == "/api/seq/resume":
            return {"id": seq_resume(body)}
        if path == "/api/seq/dismiss":
            seq_dismiss(body)
            return {"ok": True}
        if path == "/api/aux/config":
            cfg = load(F_AUX, {})
            for k in DEFAULT_AUX:
                if k in body:
                    cfg[k] = body[k]
            save(F_AUX, cfg)
            return {"ok": True}
        if path == "/api/aux/send":
            return aux_send(body)
        if path == "/api/web/test":
            st = station_by_id(body.get("station_id", ""))
            cfg = {**aux_cfg(), **{k: body[k] for k in ("web_mode", "web_url") if k in body}}
            text, ms = web_fetch(st, cfg)
            vars_, fmt = parse_vars(text)
            return {"fmt": fmt, "ms": ms, "count": len(vars_), "vars": dict(list(vars_.items())[:60]), "raw": text[:2500]}
        if path == "/api/web/start":
            web_start(body)
            return {"ok": True}
        if path == "/api/web/stop":
            web_stop()
            return {"ok": True}
        if path == "/api/procedures":
            save(F_PROCS, body.get("procedures", []))
            return {"ok": True}
        if path == "/api/proc/start":
            return {"id": proc_start(body)}
        if path == "/api/proc/answer":
            proc_answer(body)
            return {"ok": True}
        if path == "/api/proc/abort":
            proc_abort(body)
            return {"ok": True}
        if path == "/api/sc/config":
            return {"cfg": sc_cfg_save(body.get("pagina"), body.get("cfg") or {})}
        if path == "/api/sc/soglie":
            return {"soglie": sc_soglie_save(body)}
        if path == "/api/sc/seq":
            return {"sequenze": sc_seq_save(body)}
        if path == "/api/sc/seq/import":
            return sc_seq_import(body)
        if path == "/api/sc/seq/delete":
            return {"sequenze": sc_seq_delete(body)}
        if path == "/api/sc/monitor":
            return sc_monitor(body)
        if path == "/api/sc/esecuzioni":
            return sc_exec_list(body)
        if path == "/api/sc/precheck":
            return sc_precheck(body)
        if path == "/api/sc/action":
            return {"id": sc_action(body)}
        if path == "/api/sc/campo":
            return sc_campo_set(body)
        if path == "/api/sc/link":
            return sc_link(body)
        if path == "/api/sc/verificato":
            return sc_verificato(body)
        if path == "/api/sc/reevaluate":
            return {"n": sc_reevaluate()}
        if path == "/api/sc/upload":
            out = []
            d = DIST_OUT / f"manuale_{datetime.now():%Y%m%d}"
            d.mkdir(parents=True, exist_ok=True)
            for f in body.get("files") or []:
                name = os.path.basename(f.get("name") or "")
                if not name.endswith((".tar.gz", ".tgz")):
                    raise ValueError(f"{name}: serve l'archivio .tar.gz creato con --archivio")
                p = d / name
                p.write_bytes(base64.b64decode(f.get("data") or ""))
                out += sc_eval_archive(p, {"name": "(file) " + name})
            return {"n": len(out)}
        if path == "/api/dist/package":
            return {"packages": pkg_save(body)}
        if path == "/api/dist/file":
            return {"package": pkg_file(body)}
        if path == "/api/dist/start":
            return {"id": dist_start(body)}
        if path == "/api/dist/stop":
            c = DISTCTRL.get(body.get("id", ""))
            if c:
                c["stop"].set()
            return {"ok": True}
        if path == "/api/dist/open":
            return dist_open(body.get("id", ""))
        if path == "/api/disconnect":
            POOL.drop_all()
            return {"ok": True}
    raise KeyError(f"percorso sconosciuto: {method} {path}")


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
        u = urlparse(self.path)
        if method == "GET" and u.path in ("/", "/index.html"):
            return self._send(200, (BASE / "console.html").read_bytes(), "text/html; charset=utf-8")
        if method == "GET" and u.path.startswith("/api/proc/report/"):
            run = RUNS.get(u.path.rsplit("/", 1)[1])
            if not run or not run.get("report_file"):
                return self._send(404, {"error": "rapporto non disponibile"})
            return self._send(200, (REPORTS_DIR / run["report_file"]).read_bytes(), "text/plain; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename={run['report_file']}"})
        if method == "GET" and u.path == "/api/profile/file":
            name = parse_qs(u.query).get("name", [""])[0]
            p = PROF_DIR / name
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.(csv|json)", name) or not p.exists():
                return self._send(404, {"error": "file non trovato"})
            return self._send(200, p.read_bytes(), "text/csv; charset=utf-8" if name.endswith(".csv") else "application/json",
                              {"Content-Disposition": f"attachment; filename={name}"})
        if method == "GET" and u.path in ("/api/sc/mass/export", "/api/sc/lab/export"):
            txt, nm = (sc_mass_export(), "prove_massive") if "/mass/" in u.path else (sc_lab_export(), "test_laboratorio")
            return self._send(200, txt.encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename={nm}_{datetime.now():%Y%m%d_%H%M}.csv"})
        if method == "GET" and u.path == "/api/sc/file":
            qd = parse_qs(u.query)
            dn, fn = qd.get("dir", [""])[0], qd.get("name", [""])[0]
            p = SC_DATI / dn / fn
            if not re.fullmatch(r"[A-Za-z0-9._-]+(/continua)?", dn) or not re.fullmatch(r"[A-Za-z0-9._-]+\.(csv|log|txt)", fn) or not p.is_file():
                return self._send(404, {"error": "file non trovato"})
            return self._send(200, p.read_bytes(), ("text/csv" if fn.endswith(".csv") else "text/plain") + "; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename={fn}"})
        if method == "GET" and u.path in ("/api/dist/export", "/api/dist/report", "/api/dist/zip"):
            i = parse_qs(u.query).get("id", [""])[0]
            try:
                if u.path.endswith("export"):
                    d = pkg_export(i)
                    return self._send(200, json.dumps(d, ensure_ascii=False, indent=1).encode("utf-8"), "application/json",
                                      {"Content-Disposition": f"attachment; filename=pacchetto_{_safe_name(d['name'])}.json"})
                if u.path.endswith("report"):
                    return self._send(200, dist_report(i).encode("utf-8-sig"), "text/csv; charset=utf-8",
                                      {"Content-Disposition": f"attachment; filename=rapporto_{i}.csv"})
                return self._send(200, dist_zip(i), "application/zip", {"Content-Disposition": f"attachment; filename=raccolta_{i}.zip"})
            except KeyError as e:
                return self._send(404, {"error": str(e).strip("'\"")})
        if method == "GET" and u.path == "/api/web/export":
            text, name = web_export()
            return self._send(200, text.encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": f"attachment; filename={name}"})
        if method == "GET" and u.path == "/api/stations/export":
            return self._send(200, export_csv().encode("utf-8-sig"), "text/csv; charset=utf-8",
                              {"Content-Disposition": "attachment; filename=colonnine.csv"})
        try:
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            self._send(200, route(method, u.path, parse_qs(u.query), body))
        except KeyError as e:
            self._send(404, {"error": str(e).strip("'\"")})
        except Exception as e:
            self._send(400, {"error": str(e)})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        # accetta solo richieste dalla pagina locale
        origin = self.headers.get("Origin", "")
        if origin and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
            return self._send(403, {"error": "origine non consentita"})
        self._handle("POST")


def reaper():
    while True:
        time.sleep(60)
        if not MON["running"]:
            POOL.reap()


def main():
    port = int(settings()["port"])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=sc_monitor_loop, daemon=True).start()
    seq_restore()
    url = f"http://127.0.0.1:{port}"
    print(f"{APP_NAME} {VERSION} attivo su {url}  (Ctrl+C per chiudere)")
    print(f"Dati in {DATA}")
    if FROZEN:
        print("Lascia aperta questa finestra finché usi PoleDesk; chiudila per uscire.")
    if not PARAMIKO_VER.startswith("3."):
        print(f"Attenzione: paramiko {PARAMIKO_VER} non supporta le chiavi SSH delle colonnine più vecchie; avvia con avvia.bat per installare la 3.x.")
    if not F_KNOWN.exists():
        print("Attenzione: chiavi host non verificate. Per la flotta crea data/known_hosts.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mon_stop()
        web_stop()
        POOL.drop_all()


if __name__ == "__main__":
    main()
