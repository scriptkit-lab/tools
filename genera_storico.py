"""
Crea uno storico finto di 30 giorni (un giro ogni 8 ore) per le colonnine del simulatore, così il
cruscotto e gli andamenti hanno dati da mostrare. Scrive SOLO nella cartella dati indicata da
MANUT21_DATA (la demo usa %USERPROFILE%\\Manut21\\demo, separata dai dati veri).

Scenari: supercap in calo (18087), segnale modem che peggiora fino alla disconnessione (18085),
schermo guasto da 5 giorni (18082), scheda AUX da 2 giorni (18084), MCB aperto da 12 ore (18083),
contattore da 9 giorni (18086), CPU che scalda sempre di più (18088), colonnina "sana" che non
eroga energia da 6 giorni (18081), colonnina irraggiungibile da sempre (18099).
"""
import datetime as dt
import json
import math
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import server       # noqa: E402
import simulatore   # noqa: E402

DAYS, STEP_H = 30, 8
random.seed(7)


def fmt(x, unit, nd=2):
    return f"{x:.{nd}f} {unit}"


def scenario(port, vals, d):
    """d = giorni prima di adesso"""
    j = math.sin(d * 2.1 + port) * 0.6
    vals["TEMPERATURE"] = fmt(41.5 + 4 * math.sin(d * 6.28) + j, "c")
    vals["AUX TEMPERATURE"] = fmt(36 + 5 * math.sin(d * 6.28) + j, "C")
    if port != 18088:
        vals["CPU-TEMPERATURE"] = fmt(62 + 3 * math.sin(d * 6.28) + j, "c")
    else:
        vals["CPU-TEMPERATURE"] = fmt(78.4 - 0.41 * d + j * 0.5, "c")
    rate = {18081: 38, 18082: 25, 18083: 44, 18084: 31, 18085: 52, 18086: 0, 18087: 29, 18088: 47}.get(port, 30) * 1000
    for n in "01":
        k = f"METER {n} TOTAL ACTIVE ENERGY"
        cur = server.num(vals[k])
        share = 0.55 if n == "0" else 0.45
        dd = max(0.0, d - 6) if port == 18081 else d
        vals[k] = f"{int(cur - rate * share * dd)} wh"
    if port == 18087:
        vals["SUPERCAP VOLTAGE"] = fmt(11.92 + 0.028 * d + random.uniform(-0.01, 0.01), "v")
        if d >= 1:
            vals["RFID CONNECTION"] = "CONNECTED"
            vals["ANTITAMPER OK STATUS"] = "CLOSED"
    if port == 18085:
        if d >= 2:
            vals["MODEM STATUS"] = "CONNECTED"
            vals["PPP0 ADDRESS"] = "10.59.3.41"
            csq = min(26, round(13 + (d - 2) * 0.45 + random.uniform(-1, 1)))
            vals["MODEM SIGNAL QUALITY"] = f"CSQ={csq} PWR={-113 + 2 * csq}dbm"
    if port == 18082 and d >= 5:
        vals["DISPLAY STATUS"] = "CONNECTED"
    if port == 18084 and d >= 2:
        vals.update({"AUX FAULT": "00000000", "AUX SUPPLY 12V": "12.90 v", "AUX FAN POWER": "ON"})
    if port == 18083 and d >= 0.5:
        vals.update({"SOCKET 0 POWER BREAKER": "CLOSE", "EVSE TASK 0 STATUS": "IDLE", "EVSE TASK 0 END CHARGE RESULT": "EV_DISCONNECTED"})
    if port == 18086 and d >= 9:
        vals["SOCKET 1 POWER SWITCH"] = "OPEN"
    if port == 18088 and d > 0.2:
        vals.update({"EVSE TASK 0 STATUS": "IDLE", "SOCKET 0 POWER SWITCH": "OPEN", "SOCKET 0 PROXIMITY": "65535 Ohm",
                     "SOCKET 0 CONTROL PILOT": "11.90 v.", "METER 0 CURRENT L1": "0.00 A"})


DEMO_CODES = {"DISPLAY": ("DEMO-DSP-001", "Display completo con cornice"), "CAVO DISPLAY": ("DEMO-CBL-014", "Cablaggio display"),
              "SCHEDA AUX TESTA": ("DEMO-AUX-020", "Scheda PS4GAUX testa"), "VENTOLA TESTA": ("DEMO-FAN-031", "Ventola testa 12 V"),
              "MAGNETOTERMICO PRESA 0": ("DEMO-MCB-040", "Magnetotermico 40 A 4P"), "MAGNETOTERMICO PRESA 1": ("DEMO-MCB-040", "Magnetotermico 40 A 4P"),
              "CONTATTORE PRESA 0": ("DEMO-CTT-052", "Contattore 4P 40 A"), "CONTATTORE PRESA 1": ("DEMO-CTT-052", "Contattore 4P 40 A"),
              "MODEM": ("DEMO-MDM-060", "Modem 4G"), "ANTENNA MODEM": ("DEMO-ANT-061", "Antenna 4G con cavo"),
              "LETTORE RFID": ("DEMO-RFD-070", "Lettore RFID"), "ANTITAMPER": ("DEMO-TMP-080", "Microinterruttore antitamper"),
              "SUPERCAP": ("DEMO-SCP-090", "Supercondensatore backup")}


def demo_parts():
    """Nella demo i ricambi hanno codici d'esempio (DEMO-...), se il file non ne ha già."""
    rows = server.read_csv(server.F_PARTS)
    if any(r.get("codice") for r in rows):
        return
    have = {r.get("componente", "").upper(): r for r in rows}
    for comp, (code, desc) in DEMO_CODES.items():
        r = have.get(comp) or {"componente": comp}
        r.update(codice=code, descrizione=desc)
        if comp not in have:
            rows.append(r)
    server.write_csv(server.F_PARTS, server.PART_COLS, [[r.get(c, "") for c in server.PART_COLS] for r in rows])


def main():
    server.ensure_files()
    demo_parts()
    sts = [s for s in server.stations() if s.get("porta_web", "").isdigit()]
    if not sts:
        print("Elenco colonnine vuoto: copia prima colonnine_prova.csv nella cartella dati.")
        return
    now = dt.datetime.now().replace(second=0, microsecond=0)
    n_runs = DAYS * 24 // STEP_H
    for i in range(n_runs, 0, -1):
        t = now - dt.timedelta(hours=STEP_H * i) + dt.timedelta(minutes=random.randint(-20, 20))
        d = (now - t).total_seconds() / 86400
        rundir = server.RUNS_DIR / t.strftime("%Y%m%d_%H%M%S")
        rundir.mkdir(parents=True, exist_ok=True)
        for st in sts:
            port = int(st["porta_web"])
            base = {"id": st["id"], "nome": st.get("nome", ""), "ip": st["ip"], "porta_web": st["porta_web"], "layout": [], "errore": "",
                    "titolo": "MASTER", "secondi": 0.2, "ora": t.strftime("%H:%M:%S")}
            if port not in simulatore.VARIANTS:
                vals = {"_PAGINA": "NON RAGGIUNGIBILE", "_CUID_DIVERSO": "NO"}
                cuid = ""
                base["errore"] = "connessione rifiutata"
            else:
                vals, _, _ = server.parse_page(simulatore.page(port).decode("latin-1"))
                scenario(port, vals, d)
                vals["DATE/TIME LINUX"] = t.strftime("%d.%m.%Y %H:%M:%S")
                vals["_PAGINA"] = "OK"
                cuid = vals.get("CUID", "")
                vals["_CUID_DIVERSO"] = "SI" if st.get("cuid_atteso") and cuid != st["cuid_atteso"] else "NO"
            esito, anomalie, segni = server.evaluate(vals)
            res = {**base, "cuid": cuid, "esito": esito, "anomalie": anomalie, "segni": segni, "valori": vals}
            key = f"{cuid or 'senza-cuid'}_{st['ip']}_{port}".replace(":", "_")
            (rundir / f"{key}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    print(f"Storico di prova creato: {n_runs} giri per {len(sts)} colonnine in {server.RUNS_DIR}")


if __name__ == "__main__":
    main()
