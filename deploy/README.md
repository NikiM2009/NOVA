# Betrieb als Dienst

Diese Vorlagen werden nicht automatisch aktiviert. Zuerst interaktiv installieren und testen; Pfade, Benutzer, Datenverzeichnisse und URL anpassen.

## Linux

Dedizierten Benutzer `nova` anlegen, NOVA beispielsweise nach `/opt/NOVA` installieren und `/var/lib/nova/central` bzw. `/var/lib/nova/agent` diesem Benutzer zuordnen. Python-Venv mit `python install.py --no-start` vorbereiten. Das Dienstkonto muss Java starten und seine Datenverzeichnisse schreiben dürfen.

Zentrale zunächst als Dienstkonto starten, damit das Initialpasswort sicher aufgezeichnet werden kann:

```sh
sudo -u nova /opt/NOVA/.venv/bin/python -m nova serve --data /var/lib/nova/central
```

Agent vor Aktivierung interaktiv als Dienstkonto mit denselben Parametern koppeln:

```sh
sudo -u nova /opt/NOVA/.venv/bin/python -m nova agent --url https://nova.example.com --address 10.0.0.11 --data /var/lib/nova/agent
```

Beenden, passende Unit nach `/etc/systemd/system/` kopieren, dann beispielsweise:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now nova-central
sudo journalctl -u nova-central -f
```

Agent: `nova-agent` statt `nova-central`. Die Zentrale bindet in der Vorlage nur an Loopback; HTTPS-Reverse-Proxy separat einrichten. Ein Agentenstopp stoppt alle verwalteten Java-Prozesse. Keine zweite Agenteninstanz mit gleichem Datenverzeichnis starten.

## Windows

Nach `py install.py --no-start` das Panel oder den Agenten manuell mit dem gewünschten **absoluten Datenverzeichnis** initialisieren. Beispiel:

```powershell
.venv\Scripts\python.exe -m nova serve --data C:\NOVAData\central
# Oder Agent koppeln:
.venv\Scripts\python.exe -m nova agent --url https://nova.example.com --address 10.0.0.12 --data C:\NOVAData\agent
```

Danach aus PowerShell (mit zum Anlegen einer Benutzeraufgabe nötigen Rechten):

```powershell
.\deploy\register-windows-task.ps1 -Mode central -DataDirectory C:\NOVAData\central
# Oder:
.\deploy\register-windows-task.ps1 -Mode agent -CentralUrl https://nova.example.com -DataDirectory C:\NOVAData\agent
Start-ScheduledTask -TaskName NOVA-agent
```

Keine Execution-Policy global abschalten; Skript vor Ausführung prüfen und die lokale Skriptausführungsrichtlinie des Rechners beachten. Die Aufgabe läuft im aktuellen, angemeldeten Benutzerkontext ohne erhöhte Rechte. Bei Abmeldung kann sie beendet werden. Für einen vor der Anmeldung verfügbaren Serverbetrieb einen geeigneten Dienst unter einem dedizierten Konto über die Windows-Administration einrichten.

Zugriff auf Datenverzeichnisse per ACL auf das Dienstkonto und Administratoren beschränken. Keine Geheimnisse als Aufgabenargumente speichern.
