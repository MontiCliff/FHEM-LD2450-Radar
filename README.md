Dies ist kein echtes FHEM-Modul. Mein erster Versuch war ein Modul aber das schreiben der Werte mit “setreading” hat FHEM überfordert (mehrmals pro Sekunde). Deshalb ist das jetzt rein über MQTT gelöst.

Bitte immer die vorgegebenen Ports benutzen 1888 / 9001 sonst wird das nix.
Wer sich auskennt kann natürlich Änderungen vornehmen auch z.Bsp. für mehrere LD2450 usw.
Dann müssen aber alle Dateien und FHEM-Devices überprüft werden.

Bevor man beginnt sollte man die Bluetooth ID des Gerätes auslesen.
Da ich den Radar Sensor einfach an ein Netzteil gehängt habe, ohne ESP oder …. geht das am
einfachsten mit dem Starten der originalen App (iOS, Android) und Verbindung über Bluetooth.
Dort dann auf jeden Fall auf Multi-Target stellen und !!! die neueste “Beta”-Firmware installieren.

Ganz wichtig: Das Gerät kann nur von einer Instanz abgefragt werden als immer in der App auf den Auswahlbildschirm zurückgehen und am besten noch Stromlos machen und neu starten.

Damit die Daten nicht gespiegelt werden ist die richtige Positionierung des LD2450 wichtig.
Die Stift(Stecker)leiste ist unten und die Bluetooth-Antenne zeigt nach oben.
(Diese Positionierung wird offiziell vorgegeben, funktionieren tut der LD2450 in jeder Richtung aber die Daten sind dann eben gespiegelt)

# Mosquitto
## installation

```
sudo apt update
sudo apt upgrade
sudo apt install mosquitto mosquitto-clients
```
## Konfiguration
Die Ports 1888 und 9001 dürfen nicht geändert werden
Die IP (192.xxx.xxx.xx) muss die IP des FHEM-Servers sein

```
sudo nano /etc/mosquitto/conf.d/custom.conf
```

Diese Zeilen in die Datei einfügen:

```custom.conf
#Mosquitto Client Listener (z.B. für FHEM)
Port 1888 (TCP)
listener 1888 192.xxx.xxx.xx
protocol mqtt
allow_anonymous true

#WebSocket Listener (z.B. für LD2450 Web-Client)
#Port 9001 (WebSockets)
listener 9001 192.xxx.xxx.xx
protocol websockets
allow_anonymous true

```


```language
sudo systemctl restart mosquitto
```

# Script
Datei ld2450_bridge.py herunterladen und auf dem FHEM-Server speichern, wo ist grundsätzlich egal

```language
sudo chmod 655 ld2450_bridge.py 
```
Den Pfad zu dieser Datei merken

# FHEM
## Mosquitto Client anlegen
Hier wieder die IP (192.xxx.xxx.xx)mit der FHEM-Server-IP ersetzen, der Port muß bleiben

```FHEM
defmod Mosquitto_Client MQTT2_CLIENT 192.xxx.xxx.xx:1888
attr Mosquitto_Client room Radar
```

## Mosquitto Device anlegen
Dies zeigt die Daten in FHEM an für weitere Verarbeitung.

```FHEM
defmod LD2450_Radar_1 MQTT2_DEVICE Mosquitto_Client
attr LD2450_Radar_1 jsonMap targetspresent:targets_count
attr LD2450_Radar_1 readingList fhem/radar/readings:.* { json2nameValue($EVENT) }
attr LD2450_Radar_1 room Radar
setstate LD2450_Radar_1 2025-12-07 15:48:13 IODev Mosquito_Client
```
## Bridge control
Dient zum starten/stoppen des ld2450_bridge.py scripts aus fhem heraus.
Der Prozess wird separat ausgeführt um FHEM nicht zu blockieren.
Unter ld2450MacAddre muss die Bluetooth MAC-Adresse des Radar gesetzt werden (findet man z.Bsp. in der Original App)
Unter scriptPath muß der Pfad zum Python script “ld2450_bridge.py” gesetzt werden
```FHEM
defmod LD2450_Bridge_Control dummy
attr LD2450_Bridge_Control userattr scriptPath ld2450MacAddr
attr LD2450_Bridge_Control ld2450MacAddr C9:AE:AD:8D:1D:07
attr LD2450_Bridge_Control room Radar
attr LD2450_Bridge_Control scriptPath /home/pi/ld2450_bridge.py
attr LD2450_Bridge_Control setList start:noArg stop:noArg
attr LD2450_Bridge_Control webCmd start:stop
```
### Jetzt kommt das Notify zum Starten
```FHEM
defmod LD2450_Script_Action_Start notify { LD2450_Bridge_Control_Start();; }
attr LD2450_Script_Action_Start room Radar
```

```FHEM
defmod LD2450_Script_Action_Stop notify { LD2450_Bridge_Control_Stop() }
attr LD2450_Script_Action_Stop room Radar
```
Jetzt noch die Kommandos in die 99_myUtils.md

```PERL
sub LD2450_Bridge_Control_Start()
{ 
  my $NAME = "LD2450_Bridge_Control";
  
  #1. Konfigurationswerte aus Attributen auslesen
  my $scriptPath = AttrVal($NAME, "scriptPath", "/usr/local/bin/default.py");
  my $macAddr    = AttrVal($NAME, "ld2450MacAddr", "00:00:00:00:00:00");
  my $fhemPort = InternalVal("FHEMWEB", "PORT", "8083"); 

  my $ip = qx(ip -4 addr show scope global);
  $ip =~ /inet (\d+\.\d+\.\d+\.\d+)/;
  my $fhemIP = $1;
 
  #2. Überprüfung: Läuft das Skript bereits?
  my $current_pid = ReadingsVal($NAME, "current_pid", "0");
  if ($current_pid =~ /^\d+$/ && $current_pid != 0) {
    Log 3, "LD2450 Script: Start abgebrochen, da PID $current_pid bereits läuft.";
    fhem("setreading $NAME script_status bereits aktiv (PID: $current_pid)");
    return;
  }

  #3. Den vollständigen Shell-Befehl zusammenstellen (ENTKOPPELT)
  #Hier verwenden wir nohup und eine separate Shell-Ausführung
  #Der Befehl wird in einer eigenen Shell ausgeführt und PID in eine Datei geschrieben
  my $logFile = "/tmp/ld2450_script_start.log";
  my $pidFile = "/tmp/ld2450_bridge.pid";
  
  #Die Argumente für das Python-Skript
  my $args = "--mac $macAddr --fhemip $fhemIP --fhemport $fhemPort";

 #Der Kommando-String:
  my $cmd = "nohup /usr/bin/python3 $scriptPath $args > $logFile 2>&1 & echo \$! > $pidFile";

  #4. Den Befehl nicht-blockierend ausführen
  #Achtung: Wir verwenden system() und NICHT qx()
  system($cmd);
  
  #5. Kurz warten, um sicherzustellen, dass die PID geschrieben wurde
  #Dies ist notwendig, da das Skript im Hintergrund startet.
  sleep 1;
  
  #6. PID aus der Datei lesen (blockiert kurz, ist aber sicher)
  my $pid = qx(cat $pidFile);
  chomp($pid);

  #7. Readings setzen
  fhem("setreading $NAME script_status gestartet (IP: $fhemIP:$fhemPort)");
  fhem("setreading $NAME current_pid $pid");
  }

sub LD2450_Bridge_Control_Stop()
{
  my $NAME = "LD2450_Bridge_Control";

  my $pid_to_kill = ReadingsVal($NAME, "current_pid", "");
  my $pidFile = "/tmp/ld2450_bridge.pid";
  
  if ($pid_to_kill =~ /^\d+$/ && $pid_to_kill != 0) { 
    # Prozess beenden
    my $result = qx(kill $pid_to_kill 2>&1);
    
    # PID-Datei löschen
    qx(rm -f $pidFile); 
    
    fhem("deletereading $NAME current_pid");
    fhem("setreading $NAME script_status gestoppt");
    Log 3, "LD2450 Script: PID $pid_to_kill beendet. Resultat: $result";
  } else {
    Log 3, "LD2450 Script Stop: Keine gültige PID gefunden zum Stoppen.";
  }
}
```

Grundsätzlich läuft jetzt schon alles und
nach dem Starten sollte das FHEM device “LD2450_Radar_1” schon die Werte anzeigen. (Falls die Werte gespiegelt sind bitte am Anfang die Hinweise beachten)

Wer jetzt noch eine grafische Darstellung haben möchte benutzt die Datei Index.html, diese wird am einfachsten auch auf den FHEM server geladen und mit Angabe des Pfades im Internet Browser aufgerufen. Wer will kann diese HTML auch in FHEM einbinden …

Das Projekt ist frei verfügbar auf Github. Wem was nicht passt kopieren, ändern, gut is!
Ich habe leider keine Zeit/Lust dort die Tickets zu lesen und zu bearbeiten da diese Lösung für mich erstmal funktioniert.
