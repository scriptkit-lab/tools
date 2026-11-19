"""
Colonnine 2.1 finte per provare Manutenzione 2.1 senza colonnine vere.

Avvia 8 pagine di stato su 127.0.0.1, porte 18081-18088, impaginate come la pagina
"Informazioni Diagnostiche" vera (rilievo del 11/11/2026), una sana e le altre con guasti tipici.
Solo HTTP: i controlli SSH risulteranno non riusciti.
Uso: python simulatore.py   poi in Impostazioni carica le colonnine di prova (vedi LEGGIMI).
"""
import datetime as dt
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VALUES = {
    "CP VERS": "29.1.19", "CM_VERS": "EIGCU_EMMS", "RVE_USR_VERS": "RVE-PS-4G V0.14", "RVE_AUX_VERS": "RVE-PS-4G-AUX V0.17",
    "RVE_RFID_VERS": "RVE-RFID V 2.42", "DISPLAY_VERS": "01.44#001", "DISPLAY STATUS": "CONNECTED",
    "LANGUAGE.INI": "it de US es fr hu ru ro no UK pl he ca", "CUID": "", "ACTIVE FLAG": "ACTIVE", "PPP0 ADDRESS": "10.59.3.33",
    "MODEM STATUS": "CONNECTED", "MODEM SIGNAL QUALITY": "CSQ=24 PWR=-68dbm", "DATE/TIME LINUX": "", "AUX CONNECTION": "CONNECTED",
    "FTDI START COUNTER": "1", "FTDI LAST START": "11/11/2026-11:42:11", "RFID CONNECTION": "CONNECTED", "BLUETOOTH CONNECTION": "DISABLED",
    "SUPERCAP VOLTAGE": "12.76 v", "TEMPERATURE": "41.53 c", "AC POWER": "ONLINE", "CPU-TEMPERATURE": "61.93 c", "CPU-FREQ": "198.00 Mhz",
    "ANTITAMPER OK STATUS": "CLOSED", "MAIN POWER VOLTAGE": "24.96 v", "SEC POWER VOLTAGE": "13.49 v", "AUX SUPPLY 5V": "4.90 v",
    "AUX SUPPLY 12V": "12.91 v", "AUX SUPPLY USB": "4.84 v", "AUX TEMPERATURE": "35.98 C", "AUX LEDS CURRENT": "3.21 A", "AUX LUX": "269",
    "AUX RFID POWER": "ON", "AUX FAN POWER": "ON", "AUX HEATER POWER": "OFF", "AUX PANEL POWER": "ON", "AUX FAULT": "00000000",
    "FREE VALUE": "FREE VALUE", "USER STATUS": "IDLE",
}
TOP = [("CP VERS", "CM_VERS"), ("RVE_USR_VERS", "RVE_AUX_VERS"), ("RVE_RFID_VERS", "DISPLAY_VERS"), ("DISPLAY STATUS", "LANGUAGE.INI"),
       ("CUID", "ACTIVE FLAG"), ("PPP0 ADDRESS", "MODEM STATUS"), ("MODEM SIGNAL QUALITY", "DATE/TIME LINUX"), ("AUX CONNECTION", "FTDI START COUNTER"),
       ("FTDI LAST START", "RFID CONNECTION"), ("BLUETOOTH CONNECTION", "SUPERCAP VOLTAGE"), ("TEMPERATURE", "AC POWER"),
       ("CPU-TEMPERATURE", "CPU-FREQ"), ("ANTITAMPER OK STATUS", "MAIN POWER VOLTAGE"), ("SEC POWER VOLTAGE", "AUX SUPPLY 5V"),
       ("AUX SUPPLY 12V", "AUX SUPPLY USB"), ("AUX TEMPERATURE", "AUX LEDS CURRENT"), ("AUX LUX", "AUX RFID POWER"),
       ("AUX FAN POWER", "AUX HEATER POWER"), ("AUX PANEL POWER", "AUX FAULT"), ("FREE VALUE", "USER STATUS")]
SOCK = [("EVSE TASK {n} STATUS", "IDLE"), ("EVSE TASK {n} GROUP", ""), ("EVSE TASK {n} END CHARGE RESULT", "INIT_RESET"),
        ("EVSE TASK {n} SOCKET IN USE", "NONE"), ("EVSE TASK {n} TEMPERATURE", "41.52 c"), ("SOCKET TYPE {n}", "UNKNOWN"),
        ("SOCKET {n} PROXIMITY", "65535 Ohm"), ("SOCKET {n} CONTROL PILOT", "11.90 v."), ("SOCKET {n} PWM STATUS", ""),
        ("SOCKET {n} POWER SWITCH", "OPEN"), ("SOCKET {n} POWER BREAKER", "CLOSE"), ("SOCKET {n} DIFF.BREAKER", "CLOSE"),
        ("METER {n} TOTAL ACTIVE ENERGY", "171605 wh"), ("METER {n} TOTAL REACTIVE ENERGY", "264 wh"), ("METER {n} ISTANT POWER SUPPLY", "0 w"),
        ("METER {n} SESSION ACTIVE ENERGY", "0 wh"), ("METER {n} SESSION REACTIVE ENERGY", "0 wh"), ("METER {n} CURRENT L1", "0.00 A"),
        ("EVSE {n} MAX CURRENT", "0.00 A"), ("EVSE {n} CHARGE CURRENT", "0.00 A"), ("EVSE {n} PHASE NUMBER", "0"), ("EVSE {n} CP SIMPLIFIED", "0"),
        ("EVSE {n} BOOKING TIME", "0"), ("EVSE {n} BOOKING ID", ""), ("EVSE {n} BOOKING RFID_ID", ""), ("EVSE {n} BLE BOOKING", ""),
        ("EVSE {n} BLE CLIENT_ID", "")]

VARIANTS = {
    18081: ("Sana", {}),
    18082: ("Schermo scollegato", {"DISPLAY STATUS": "DISCONNECTED"}),
    18083: ("MCB presa 0 aperto", {"SOCKET 0 POWER BREAKER": "OPEN", "EVSE TASK 0 STATUS": "FAULTED", "EVSE TASK 0 END CHARGE RESULT": "MCB_OPEN"}),
    18084: ("Scheda AUX in guasto", {"AUX FAULT": "00000004", "AUX SUPPLY 12V": "10.80 v", "AUX FAN POWER": "OFF"}),
    18085: ("Modem e segnale", {"MODEM STATUS": "DISCONNECTED", "MODEM SIGNAL QUALITY": "CSQ=99 PWR=0dbm", "PPP0 ADDRESS": "-"}),
    18086: ("Contattore incollato presa 1", {"SOCKET 1 POWER SWITCH": "CLOSE"}),
    18087: ("RFID e antitamper", {"RFID CONNECTION": "DISCONNECTED", "ANTITAMPER OK STATUS": "OPEN", "SUPERCAP VOLTAGE": "11.92 v"}),
    18088: ("In carica sulla presa 0", {"EVSE TASK 0 STATUS": "CHARGING", "SOCKET 0 POWER SWITCH": "CLOSE", "SOCKET 0 PROXIMITY": "220 Ohm",
                                       "SOCKET 0 CONTROL PILOT": "6.02 v.", "SOCKET 0 PWM STATUS": "ON", "METER 0 CURRENT L1": "31.40 A",
                                       "METER 0 ISTANT POWER SUPPLY": "21650 w", "EVSE 0 CHARGE CURRENT": "31.40 A", "EVSE 0 MAX CURRENT": "32.00 A",
                                       "EVSE 0 PHASE NUMBER": "3", "SOCKET TYPE 0": "TYPE2 32A", "CPU-TEMPERATURE": "78.40 c"}),
}


def page(port):
    name, over = VARIANTS[port]
    v = dict(VALUES)
    for k, val in SOCK:
        for n in "01":
            v[k.replace("{n}", n)] = val
    v["CUID"] = f"N.22XPMOCK{port:08d}"
    v["DATE/TIME LINUX"] = dt.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    v.update(over)
    rows = [(a, b) for a, b in TOP] + [(k.replace("{n}", "0"), k.replace("{n}", "1")) for k, _ in SOCK]
    body = "".join(f"<tr><td>{a}</td><td>{v[a]}</td><td>{b}</td><td>{v[b]}</td></tr>" for a, b in rows)
    return (f"<html><body><center><h2>Informazioni Diagnostiche</h2><h2><a href='slave.html'>MASTER</a></h2></center><!-- simulatore: {name} -->"
            f"<table><tr><th>Descrizione</th><th>Valore</th><th>Descrizione</th><th>Valore</th></tr>{body}</table></body></html>").encode("latin-1", "replace")


def serve(port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = page(port)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    for p in VARIANTS:
        threading.Thread(target=serve, args=(p,), daemon=True).start()
        print(f"127.0.0.1:{p}  {VARIANTS[p][0]}")
    print("Colonnine finte attive. Ctrl+C per chiudere.")
    threading.Event().wait()
