#!/usr/bin/env python3
"""
LD2450 BLE Sniffer - Empfängt Konfiguration über Kommandozeilenargumente von FHEM.
- Optimiert: HTTP-Requests an FHEM werden nun gebündelt (Batch) und
  asynchron (in einem separaten Thread) ausgeführt, um die BLE-Verarbeitung nicht zu blockieren.

Aufrufsignatur (von FHEM):
1. sys.argv[1]: MAC-Adresse
2. sys.argv[2]: FHEM-Host-IP (z.B. 192.168.69.32)
3. sys.argv[3]: FHEM-Port (z.B. 8083)
4. sys.argv[4]: FHEM-Device-Name (z.B. Radar)
"""

import asyncio
from bleak import BleakClient
import struct
import requests
import sys
import urllib.parse
import signal
# Wir verwenden functools.partial für die saubere Übergabe der Synchronen Funktion an den Executor
from functools import partial

# --- Globale Variablen, die von FHEM überschrieben werden ---
ADDR = "C9:AE:AD:8D:1D:07" # Standardwert (mit Doppelpunkten)
fhem_host = "127.0.0.1"    # Standardwert
fhem_port = "8083"         # Standardwert
device = "Radar"           # Standardwert

# Steuer-Flag für den Graceful Shutdown
stop_requested = False 

# --- Konstanten ---
UUID_NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
UUID_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"
START_CMD = bytes([0xAA, 0x55, 0x03, 0x00, 0x01])  # start streaming
target_id = "target"

# Globale Basis-URL für FHEM (wird in main() initialisiert)
FHEM_URL_BASE = "" 

# Zähler für den Bluetooth-Neustart
RECONNECT_ATTEMPTS = 0
MAX_RECONNECT_ATTEMPTS = 5

def setup_arguments():
    """Liest die Kommandozeilenargumente, die von FHEM übergeben werden."""
    global ADDR, fhem_host, fhem_port, device, FHEM_URL_BASE
    
    # sys.argv[0] ist der Skriptname, wir erwarten 4 weitere Argumente
    if len(sys.argv) < 5:
        full_host = f"{fhem_host}:{fhem_port}"
        print("FEHLER: Nicht genügend Argumente von FHEM übergeben.")
        print(f"Verwende Standardwerte: ADDR={ADDR}, HOST={full_host}, DEVICE={device}")
    else:
        # 1. MAC-Adresse (colon-free von FHEM)
        addr_colon_free = sys.argv[1]
        # Füge Doppelpunkte für BleakClient hinzu und überschreibe ADDR
        ADDR = ':'.join(a + b for a, b in zip(addr_colon_free[::2], addr_colon_free[1::2]))
        
        # 2. Host (IP)
        fhem_host = sys.argv[2]
        
        # 3. Port
        fhem_port = sys.argv[3]
        
        # 4. FHEM Device Name
        device = sys.argv[4]

        print(f"Konfiguration erfolgreich geladen:")
        print(f"  BLE ADDR: {ADDR}")
        print(f"  FHEM Host: http://{fhem_host}:{fhem_port}")
        print(f"  FHEM Device: {device}")

    # Setze die globale FHEM-URL-Basis
    FHEM_URL_BASE = f"http://{fhem_host}:{fhem_port}/fhem?cmd="

def send_fhem_command_sync(command):
    """
    Synchroner Wrapper für requests.get.
    Dieser läuft im Hintergrund-Thread.
    """
    try:
        # Verwende urllib.parse.quote, um den Befehl sicher für die URL zu kodieren
        encoded_cmd = urllib.parse.quote(command)
        url = FHEM_URL_BASE + encoded_cmd
        
        # Sendet den Batch-Befehl an FHEM
        requests.get(url, timeout=5)
    except requests.exceptions.RequestException as req_e:
        # Protokolliere Fehler in der Konsole des Hintergrundprozesses
        print(f"FEHLER beim Senden an FHEM (Batch): {req_e}")

def convert_cooridnate(byte1, byte2):
    """
    Konvertiert zwei Bytes (16-Bit Vorzeichen-Betrag) in einen vorzeichenbehafteten Dezimalwert (mm).
    Annahme: byte1 ist das LSB (niedrigstwertiges Byte), byte2 ist das HSB (höchstwertiges Byte).

    :param byte1: Das niedrigstwertige Byte (LSB).
    :param byte2: Das höchstwertige Byte (HSB) - enthält das Vorzeichen-Bit.
    :return: Die Koordinate als Integer in mm.
    """
    # Sicherstellen, dass die Eingaben Integer sind
    if isinstance(byte1, str):
        int_byte1 = int(byte1, 16)
    else:
        int_byte1 = int(byte1)

    if isinstance(byte2, str):
        int_byte2 = int(byte2, 16)
    else:
        int_byte2 = int(byte2)

    # 1. Die 16-Bit-Zahl im Little-Endian-Format zusammensetzen:
    # HSB (byte2) << 8 | LSB (byte1)
    koordinaten_wert = (int_byte2 << 8) | int_byte1
    # Bits der Zahl: 15 14 13 12 11 10 9 8 7 6 5 4 3 2 1 0
    # Wert:          S B B  B  B  B  B B B B B B B B B B
    #                 ^--- HSB (byte2) ---^ ^--- LSB (byte1) ---^

    # 2. Das Vorzeichen-Bit (Bit 15) extrahieren
    VORZEICHEN_MASKE = 0x8000 # Binär 1000 0000 0000 0000
    
    # ist_positiv: Bit 1 = positiv, Bit 0 = negativ
    ist_positiv = (koordinaten_wert & VORZEICHEN_MASKE) != 0 # Angepasst auf Ihre Definition (1 = positiv)
    
    # 3. Den Absolutwert (Bits 0 bis 14) extrahieren
    ABSOLUTWERT_MASKE = 0x7FFF # Binär 0111 1111 1111 1111
    
    absolutwert_mm = koordinaten_wert & ABSOLUTWERT_MASKE

    # 4. Endgültige Koordinate bestimmen
    if ist_positiv:
        koordinate_mm = absolutwert_mm
    else:
        koordinate_mm = -absolutwert_mm

    return koordinate_mm

def parse_radar_targets_and_batch_command(rec_buf, loop):
    """
    Extrahiert die Targets, erstellt einen einzigen Batch-Befehl
    und startet die asynchrone Ausführung des Sendevorgangs.
    """
    index = 0
    commands = []
    movement = False

    for t in range(3):
        # Entpackt 4 H-Werte (unsigned short, 2 Byte)
        x1 = rec_buf[4 + index]
        x2 = rec_buf[5 + index]

        y1 = rec_buf[6 + index]
        y2 = rec_buf[7 + index]

        s1 = rec_buf[8 + index]
        s2 = rec_buf[9 + index]

        x = convert_cooridnate(x1, x2)
        s = convert_cooridnate(s1, s2)
        y = convert_cooridnate(y1, y2)

        if x != 0 or y != 0:
         movement = True

        # Erstelle die einzelnen setreading Befehle
        target_name = f"{target_id}{t+1}"
        commands.append(f"setreading {device} {target_name}_x {x}")
        commands.append(f"setreading {device} {target_name}_y {y}")
        commands.append(f"setreading {device} {target_name}_speed {s}")
             
        index += 8
    
    # Füge alle Befehle mit Semikolon getrennt zusammen (FHEM Batch-Modus)
    # bewegt sich was oder nix
    if(movement == True):
     commands.append(f"setreading {device} movement true")
    else:
     commands.append(f"setreading {device} movement false")

    batch_command = "; ".join(commands)

    # Starte den synchronen Sendevorgang in einem separaten Thread
    # Wir verwenden loop.run_in_executor anstelle von asyncio.to_thread, da
    # asyncio.to_thread in manchen Python 3.7/3.8 Umgebungen nicht verfügbar ist.
    # partial ist wichtig, um die Argumente korrekt zu übergeben.
    loop.run_in_executor(None, partial(send_fhem_command_sync, batch_command))

    # WICHTIG: Die Funktion kehrt sofort zurück, ohne auf die Netzwerk-I/O zu warten.
    return


def restart_bluetooth_service():
    """
    Versucht, den Bluetooth-Dienst neu zu starten.
    Ruft 'systemctl restart bluetooth' OHNE SUDO auf. (Polkit-Konfiguration nötig)
    """
    global RECONNECT_ATTEMPTS
    
    RECONNECT_ATTEMPTS = 0
    
    print("\n!!! KRITISCHER FEHLER !!! Versuche 'bluetooth' Dienst neu zu starten...")
    command = ['systemctl', 'restart', 'bluetooth']
    
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print("Bluetooth Neustart erfolgreich ausgelöst.")
        print(f"stdout: {result.stdout.strip()}")
        time.sleep(10) 
        print("Wartezeit nach Neustart beendet. Versuche BLE-Wiederverbindung...")
        return True
    except subprocess.CalledProcessError as e:
        print(f"FEHLER beim Neustart des Bluetooth-Dienstes (Code {e.returncode}):")
        print(f"stderr: {e.stderr.strip()}")
        return False
    except Exception as e:
        print(f"Unerwarteter Fehler bei subprocess.run: {e}")
        return False

def signal_handler(signum, frame):
    """Behandelt das SIGTERM-Signal, um die Hauptschleife sauber zu beenden."""
    global stop_requested
    print(f"\nSignal {signum} ({signal.Signals(signum).name}) empfangen. Starte Graceful Shutdown...")
    stop_requested = True

async def main():
    # Signal-Handler registrieren
    signal.signal(signal.SIGINT, signal_handler)  # Für Ctrl+C (lokales Testen)
    signal.signal(signal.SIGTERM, signal_handler) # Für FHEM-Stopp-Befehl

    setup_arguments()
    
    # Holen Sie die aktuelle Event-Loop
    current_loop = asyncio.get_running_loop()
    full_host = f"{fhem_host}:{fhem_port}"
    
    # Zustand an FHEM melden
    def update_fhem_state(state):
        try:
            requests.get(f"http://{full_host}/fhem?cmd=setreading+{device}+state+{state}", timeout=5)
        except requests.exceptions.RequestException as e:
            # OK, wenn das Skript läuft, FHEM aber nicht erreichbar ist
            print(f"Warnung: FHEM unter {full_host} nicht erreichbar ({state} Update)")


    # Versuche, den Start-Status zu senden (Synchron, da außerhalb des Hauptloops)
    try:
        requests.get(f"http://{full_host}/fhem?cmd=setreading+{device}+state+connecting", timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"Kritischer Fehler: FHEM ist unter {full_host} nicht erreichbar. Exit.")
        sys.exit(1)


    global RECONNECT_ATTEMPTS, stop_requested

    while not stop_requested:
        try:
            # Füge einen kleinen Delay ein, um Race Conditions mit dem FHEM-Start zu vermeiden.
            await asyncio.sleep(1) 
            
            async with BleakClient(ADDR) as client:
                print("BLE Connected.")
                RECONNECT_ATTEMPTS = 0

                # Sende Start-Streaming-Kommando
                await client.write_gatt_char(UUID_WRITE, START_CMD)
                await asyncio.sleep(0.2)

                # Notification Callback
                def cb(sender, data):
                    # Übergibt die Daten und die aktuelle Event-Loop zur asynchronen Ausführung
                    parse_radar_targets_and_batch_command(bytes(data), current_loop)

                await client.start_notify(UUID_NOTIFY, cb)

                print("Sniffer running, monitoring targets.")
                update_fhem_state("running")
                
                # Warte auf Unterbrechung
                while client.is_connected and not stop_requested:
                    await asyncio.sleep(1)

        except Exception as e:
            if stop_requested:
                print("Shutdown läuft. Ignoriere Verbindungsfehler.")
                break
                
            RECONNECT_ATTEMPTS += 1
            print(f"Connection lost or error: {e}, attempt {RECONNECT_ATTEMPTS}/{MAX_RECONNECT_ATTEMPTS}.")
            update_fhem_state("reconnecting")
            
            if RECONNECT_ATTEMPTS >= MAX_RECONNECT_ATTEMPTS:
                restart_bluetooth_service()
                
            if RECONNECT_ATTEMPTS < MAX_RECONNECT_ATTEMPTS:
                await asyncio.sleep(5) 

    print("Hauptschleife beendet. Skript wird beendet.")

# Starte das Hauptprogramm
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        # Hier fängt es Fehler ab, die NACH dem Signal-Handling geworfen werden
        if not stop_requested:
             print(f"Unerwarteter Fehler im Haupt-Loop: {e}")
             sys.exit(1)
        # Wenn stop_requested==True, ist es ein erwarteter Abbruch nach SIGTERM/SIGINT
        sys.exit(0)
