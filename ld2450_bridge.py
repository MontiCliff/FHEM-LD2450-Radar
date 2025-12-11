#!/usr/bin/env python3
"""
LD2450 BLE Sniffer - Optimiert für Echtzeit-Übertragung mit MQTT.
"""

import asyncio
from bleak import BleakClient
import struct
import sys
import signal
import json
import time
import subprocess
import paho.mqtt.client as mqtt
import argparse

# --- MQTT KONFIGURATION (WICHTIG ANPASSEN!) ---
MQTT_BROKER_HOST = "192.168.69.32" 
MQTT_BROKER_PORT = 1888
CLIENT_ID = "MOSQUITTO_CLIENT" # Eindeutige ID
# --- ENDE MQTT KONFIGURATION ---

# --- Globale Variablen, die von FHEM überschrieben werden ---
ADDR = "c9:ae:ad:8d:1d:07"
fhem_host = "192.168.69.32"
fhem_port = "8083"
device = "Radar" 

# Topic-Struktur (wird in setup_arguments() initialisiert)
MQTT_READING_TOPIC = "---"
MQTT_STATE_TOPIC = "---"

# Steuer-Flag für den Graceful Shutdown
stop_requested = False

# --- Konstanten ---
UUID_NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
UUID_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"
START_CMD = bytes([0xAA, 0x55, 0x03, 0x00, 0x01])
target_id = "target"

# Zähler für den Bluetooth-Neustart
RECONNECT_ATTEMPTS = 0
MAX_RECONNECT_ATTEMPTS = 5

# --- MQTT SETUP ---
#mqtt_client = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311)
# Erstellen Sie den Client mit API V2 und setzen Sie die Client ID nachträglich.
mqtt_client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
mqtt_client.client_id = CLIENT_ID

# Optional: Setzen Sie das Protokoll, falls Sie es explizit benötigen
# mqtt_client.protocol = mqtt.MQTTv311

# Optional, aber gut: Aktivieren Sie das interne Logging
mqtt_client.enable_logger()

def on_connect(client, userdata, flags, reasoncode, properties):
    if reasoncode.is_failure:
        print(f"MQTT Verbindung fehlgeschlagen: {reasoncode}")
        sys.exit(1)
    else:
        print("MQTT Verbindung erfolgreich.")

def on_disconnect(client, userdata, disconnect_flags, reasoncode, properties):
    print(f"MQTT Disconnect: Reason {reasoncode}")
    
    if reasoncode.is_failure: # Bequemere V2-Methode
        print(f"Fehler beim Trennen der Verbindung: {reasoncode}")

mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect

def publish_mqtt_state(state):
    if not MQTT_STATE_TOPIC: return
    try:
        # qos=1 mit retain=True, damit FHEM den letzten Status nach einem Neustart sieht
        mqtt_client.publish(MQTT_STATE_TOPIC, state, qos=1, retain=True) 
        print(f"MQTT Status: {state} an Topic {MQTT_STATE_TOPIC} gesendet.")
    except Exception as e:
        print(f"WARNUNG: Fehler beim Senden des Status '{state}': {e}")


def publish_zero_readings():
    """Publiziert eine Nachricht, die alle relevanten Readings auf 0/false setzt, 
    wenn das Skript beendet wird."""
    if not MQTT_READING_TOPIC: return
    
    zero_payload = {}
    for t in range(1, 4):
        zero_payload[f"{target_id}{t}_x"] = 0.0
        zero_payload[f"{target_id}{t}_y"] = 0.0
        zero_payload[f"{target_id}{t}_speed"] = 0
        
    zero_payload["movement"] = "false"
    zero_payload["targetspresent"] = 0

    json_payload = json.dumps(zero_payload)
    
    try:
        # QOS=1, um die Zustellung zu garantieren. Retain=False, da es kein dauerhafter Wert ist.
        mqtt_client.publish(MQTT_READING_TOPIC, json_payload, qos=1, retain=False)
        print(f"MQTT: Alle Readings auf Null gesetzt an Topic: {MQTT_READING_TOPIC}")
    except Exception as e:
        print(f"WARNUNG: Fehler beim Senden des Null-Payloads: {e}")

def setup_arguments():
    global ADDR, fhem_host, fhem_port, MQTT_BROKER_HOST, MQTT_READING_TOPIC, MQTT_STATE_TOPIC

    parser = argparse.ArgumentParser()
    parser.add_argument('--mac', required=True, help='MAC Address of the LD2450 device')
    parser.add_argument('--fhemip', required=True, help='IP of the FHEM server')
    parser.add_argument('--fhemport', required=True, type=int, help='Port of the FHEMWEB instance (e.g., 8083)')

    if(len(sys.argv) < 3):
        print(f"Nicht genügend Argumente")
    else:
        args = parser.parse_args()
        ADDR = args.mac
        fhem_host = args.fhemip
        fhem_port = args.fhemport
        MQTT_BROKER_HOST = args.fhemip

    # Initialisiere die MQTT Topics basierend auf dem FHEM Device Namen (z.B. Radar -> fhem/radar/...)
    topic_base = device.lower().replace('-', '_')
    MQTT_READING_TOPIC = f"fhem/{topic_base}/readings"
    MQTT_STATE_TOPIC = f"fhem/{topic_base}/state"

def convert_cooridnate(byte1, byte2):
    """Konvertiert 16-Bit Sign-Magnitude in einen vorzeichenbehafteten Dezimalwert (mm)."""
    try:
        int_byte1 = int(byte1)
        int_byte2 = int(byte2)
        koordinaten_wert = (int_byte2 << 8) | int_byte1

        VORZEICHEN_MASKE = 0x8000
        ABSOLUTWERT_MASKE = 0x7FFF
        
        ist_positiv = (koordinaten_wert & VORZEICHEN_MASKE) != 0
        absolutwert_mm = koordinaten_wert & ABSOLUTWERT_MASKE

        if ist_positiv:
            koordinate_mm = absolutwert_mm
        else:
            koordinate_mm = -absolutwert_mm
            
        return koordinate_mm

    except Exception as e:
        print(f"FEHLER bei der Konvertierung der Koordinate: {e}")
        return 0

def parse_and_publish_targets(rec_buf):
    """Extrahiert Targets, sammelt Readings in JSON und publiziert via MQTT."""
    index = 0
    movement = False
    t_count = 0
    payload = {}

    for t in range(3):
        x1, x2, y1, y2, s1, s2 = rec_buf[4+index:10+index]
        
        x_mm = convert_cooridnate(x1, x2)
        s_mm_s = convert_cooridnate(s1, s2)
        y_mm = convert_cooridnate(y1, y2)
        
        x_m = x_mm / 1000
        y_m = y_mm / 1000
        
        fhem_x = round(x_m * -1, 3) 
        fhem_y = round(y_m, 3) 
        fhem_speed = s_mm_s

        target_name = f"{target_id}{t+1}"
        
        payload[f"{target_name}_x"] = fhem_y
        payload[f"{target_name}_y"] = fhem_x
        payload[f"{target_name}_speed"] = fhem_speed
        
        if fhem_x != 0 or fhem_y != 0:
            movement = True
            t_count += 1
            
        index += 8
    
    payload["movement"] = "true" if movement else "false"
    payload["targetspresent"] = t_count

    json_payload = json.dumps(payload)
    
    try:
        mqtt_client.publish(MQTT_READING_TOPIC, json_payload, qos=0, retain=False)
    except Exception as e:
        print(f"WARNUNG: Fehler beim Publizieren der MQTT-Nachricht: {e}")

    return


def restart_bluetooth_service():
    global RECONNECT_ATTEMPTS
    RECONNECT_ATTEMPTS = 0
    print("\n!!! KRITISCHER FEHLER !!! Versuche 'bluetooth' Dienst neu zu starten...")
    command = ['systemctl', 'restart', 'bluetooth']
    
    if 'subprocess' not in sys.modules:
        import subprocess

    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print("Bluetooth Neustart erfolgreich ausgelöst.")
        time.sleep(10)
        return True
    except Exception as e:
        print(f"FEHLER beim Neustart des Bluetooth-Dienstes: {e}")
        return False


def signal_handler(signum, frame):
    global stop_requested
    print(f"\nSignal {signum} empfangen. Starte Graceful Shutdown...")
    stop_requested = True


async def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    setup_arguments()
    
    try:
        mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        mqtt_client.loop_start() 
    except Exception as e:
        print(f"Kritischer Fehler: Konnte keine Verbindung zum MQTT Broker herstellen: {e}")
        sys.exit(1)


    global RECONNECT_ATTEMPTS, stop_requested
    publish_mqtt_state("connecting")
    
    while not stop_requested:
        try:
            await asyncio.sleep(1)
            
            async with BleakClient(ADDR) as client:
                print("BLE Connected.")
                RECONNECT_ATTEMPTS = 0

                await client.write_gatt_char(UUID_WRITE, START_CMD)
                await asyncio.sleep(0.2)

                def cb(sender, data):
                    parse_and_publish_targets(bytes(data))

                await client.start_notify(UUID_NOTIFY, cb)

                print(f"Sniffer running. Sende Readings an Topic: {MQTT_READING_TOPIC}")
                publish_mqtt_state("running")
                
                while client.is_connected and not stop_requested:
                    await asyncio.sleep(1)

        except Exception as e:
            if stop_requested: break
                
            RECONNECT_ATTEMPTS += 1
            print(f"Connection lost or error: {e}, attempt {RECONNECT_ATTEMPTS}/{MAX_RECONNECT_ATTEMPTS}.")
            publish_mqtt_state("reconnecting")
            
            if RECONNECT_ATTEMPTS >= MAX_RECONNECT_ATTEMPTS:
                restart_bluetooth_service()
                
            if RECONNECT_ATTEMPTS < MAX_RECONNECT_ATTEMPTS:
                await asyncio.sleep(5)

    print("Hauptschleife beendet. Trenne MQTT-Verbindung. Skript wird beendet.")
    publish_mqtt_state("disconnected")
    
    # Readings vor dem Beenden auf Null setzen
    publish_zero_readings() 
    
    mqtt_client.loop_stop()
    mqtt_client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        print("Unhandled exception in main():")
        traceback.print_exc()
