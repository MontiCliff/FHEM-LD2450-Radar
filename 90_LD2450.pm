# $Id: 90_LD2450.pm 2025-11-28 1.2.1 $
#
# Modul zur Steuerung des LD2450-Radarsensors über ein externes Python-Skript
# und Bluetooth Low Energy (BLE).
#
# V1.2.1: Vereinfacht die Konfiguration, indem der FHEM-Gerätename ($name)
#         direkt als viertes Argument übergeben wird, anstatt ein separates
#         'valueName'-Attribut zu verwenden.

package main;

use strict;
use warnings;

# --- Globale Initialisierung des Moduls ---
sub LD2450_Initialize($) {
    my ($hash) = @_;
    
    $hash->{DefFn}    = "LD2450_Define";
    $hash->{UndefFn}  = "LD2450_Undefine";
    $hash->{SetFn}    = "LD2450_Set";
    $hash->{AttrFn}   = "LD2450_Attr"; 
    
    # valueName entfernt, da $name des Geräts verwendet wird.
    $hash->{AttrList} = "scriptPath";
    
    return;
}

# --- DEFINE (Gerät erstellen, erwartet 4 Argumente wie in V1.0.4) ---
sub LD2450_Define($$) {
    my ($hash, $def) = @_;
    my $name = $hash->{NAME}; 
    
    my @a = split(/\s+/, $def);
    
    Log3 $name, 1, "$name: DEBUG - Gefundene Argumente (einschließlich 'define', 'Name', 'Modul'): " . scalar(@a);
    
    # Erwartet werden 4 Elemente: define, Name, LD2450, MAC_ADDRESS
    if (scalar(@a) != 3) {
        return "Usage: define <name> LD2450 <BLE_MAC_ADDRESS>. Example: define Radar LD2450 C9:AE:AD:8D:1D:07";
    }

    my $mac_address = $a[3]; 

    # MAC-Adresse im Attribut speichern (wird beim Start verwendet)
    main::CommandAttr(undef, "$name MACAddress $mac_address");

    # Initialisierung der Readings
    readingsBeginUpdate($hash);
    readingsBulkUpdate($hash, "state", "defined");
    readingsBulkUpdate($hash, "pid", "none");
    readingsEndUpdate($hash, 1);
    
    Log3 $name, 3, "$name: Defined. MAC Address set to $mac_address. Please set 'scriptPath' attributes.";
    
    return undef;
}

# --- UNDEFINE ---
sub LD2450_Undefine($$) {
    my ($hash, $arg) = @_;
    my $name = $hash->{NAME};
    LD2450_StopScript($hash);
    Log3 $name, 3, "$name: Undefine completed.";
    return undef;
}

# --- SET ---
sub LD2450_Set($$$) {
    my ($hash, $name, $cmd, @args) = @_;

    if ($cmd eq "start") {
        return LD2450_StartScript($hash);
    } elsif ($cmd eq "stop") {
        return LD2450_StopScript($hash);
    } elsif ($cmd eq "restart") {
        my $err = LD2450_StopScript($hash);
        return $err if defined $err;
        return LD2450_StartScript($hash);
    } elsif ($cmd eq "clear") {
        return LD2450_ClearReadings($hash);
    } else {
        if (scalar(@args) > 0) {
            return "Command $cmd does not take any arguments.";
        }
        return "Unknown argument $cmd choose one of start stop restart clear";
    }
}

# --- HILFSFUNKTIONEN ZUR PROZESSSTEUERUNG (unverändert) ---
sub LD2450_GetPID($) {
    my ($hash) = @_;
    my $pid = ReadingsVal($hash->{NAME}, "pid", "none");
    return ($pid ne "none" && $pid =~ /^\d+$/) ? $pid : undef;
}

sub LD2450_StopScript($) {
    my ($hash) = @_;
    my $name = $hash->{NAME};
    my $pid = LD2450_GetPID($hash);

    if (!$pid) {
        Log3 $name, 3, "$name: Script is not running (PID unknown).";
        return undef;
    }
    
    # Sendet SIGTERM (Graceful Shutdown)
    my $result = kill('TERM', $pid); 
    
    if ($result > 0) {
        Log3 $name, 3, "$name: Script (PID $pid) terminated successfully (SIGTERM).";
        
        readingsBeginUpdate($hash);
        readingsBulkUpdate($hash, "state", "stopped");
        readingsBulkUpdate($hash, "pid", "none");
        readingsEndUpdate($hash, 1);
        return undef;
    } else {
        # Wenn der Prozess nicht auf SIGTERM reagiert (unwahrscheinlich) oder bereits tot ist
        Log3 $name, 3, "$name: Error stopping script (PID $pid) or process already gone. Resetting PID.";
        readingsBeginUpdate($hash);
        readingsBulkUpdate($hash, "state", "stopped (PID reset)");
        readingsBulkUpdate($hash, "pid", "none");
        readingsEndUpdate($hash, 1);
        return "Warning: Could not kill process $pid, but PID was reset.";
    }
}

# --- FUNKTION: BEREINIGT ALLE READINGS UND SETZT AUF 0 ---
sub LD2450_ClearReadings($) {
    my ($hash) = @_;
    my $name = $hash->{NAME};
    
    Log3 $name, 3, "$name: Starting reset of dynamic readings to 0.";
    
    my $count = 0;
    
    readingsBeginUpdate($hash);
    
    foreach my $reading (keys %{$hash->{READINGS}}) {
        # Regex, um dynamische Readings zu erkennen (z.B. target1_x, target2_distance, maxEnergy, usw.)
        # Statische Readings wie state, pid, host, port werden ignoriert
        if ($reading =~ /^(target\d+_[xyz]|target\d+_(distance|power|status)|detectionDistance|movingDistance|stationaryDistance|maxDistance|minDistance|maxEnergy|minEnergy|distance|power)/i) {
            # Setze den Wert auf 0
            readingsBulkUpdate($hash, $reading, 0);
            $count++;
        }
    }
    
    # Setze die essentiellen Readings (state, pid) zurück auf den definierten Zustand
    my $mac_address = InternalVal($name, "MACAddress", "unknown");
    readingsBulkUpdate($hash, "state", "cleared");
    readingsBulkUpdate($hash, "pid", "none");
    readingsEndUpdate($hash, 1);
    
    Log3 $name, 3, "$name: Successfully reset $count dynamic readings to 0. State reset to 'defined'.";
    return undef;
}

# --- STARTSKRIPT (Jetzt mit FHEM Device Name $name) ---
sub LD2450_StartScript($) {
    my ($hash) = @_;
    my $name = $hash->{NAME}; # <--- Der FHEM-Gerätename (z.B. "Radar")

    if (LD2450_GetPID($hash)) {
        Log3 $name, 3, "$name: Script is already running (PID " . LD2450_GetPID($hash) . "). Use restart.";
        return "Script already running. Use 'set $name restart'";
    }

    # Lese ALLE Konfigurationswerte aus den Attributen
    my $addr = InternalVal($name, "DEF", ""); 
    my $script_path = AttrVal($name, "scriptPath", "");
    my $host = "127.0.0.1";
    my $port = 8083;
    if (defined($defs{"FHEMWEB"}) && defined($defs{"FHEMWEB"}->{PORT})) {
        $port = $defs{"FHEMWEB"}->{PORT};
    }
    
    # Sanity Check: Nur die 4 Attribute müssen gesetzt sein
    if (!$addr || !$script_path || !$host || !$port) {
        return "Error: All required attributes ('scriptPath', 'host', and 'port') must be set before starting the script.";
    }
    
    # Entferne Doppelpunkte für die Kommandozeile
    (my $addr_colon_free = $addr) =~ s/://g;
    
    # Das Kommando übergibt nun $name als 4. Argument an das Python-Skript
    my $cmd = "nohup python3 $script_path $addr_colon_free $host $port $name > /dev/null 2>&1 & echo \$!";
    
    Log3 $name, 3, "$name: Starting script with 4 arguments (MAC, Host, Port, FHEM_Device_Name): $cmd";

    my $new_pid = qx{$cmd};
    $new_pid =~ s/\s//g;
    
    if ($new_pid && $new_pid =~ /^\d+$/) {
        Log3 $name, 3, "$name: Script started successfully with PID $new_pid.";
        
        readingsBeginUpdate($hash);
        readingsBulkUpdate($hash, "state", "running");
        readingsBulkUpdate($hash, "pid", $new_pid);
        readingsBulkUpdate($hash, "host", $host);
        readingsBulkUpdate($hash, "port", $port);
        # $name ist implizit der Device Name
        readingsEndUpdate($hash, 1);
        return undef;
    } else {
        Log3 $name, 1, "$name: FAILED to start script. System command result: $new_pid";
        readingsBeginUpdate($hash);
        readingsBulkUpdate($hash, "state", "failed");
        readingsEndUpdate($hash, 1);
        return "Error starting script. Check scriptPath, Python installation, and permissions.";
    }
}

# --- BOILERPLATE-FUNKTIONEN ---
sub LD2450_Attr(@) {
    return;
}

1;
=head1 NAME
...
=cut
