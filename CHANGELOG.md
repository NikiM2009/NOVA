# Änderungen

## Fehlgeschlagene oder lokal fehlende Instanzen

- Ursprüngliche Installationsfehler werden dauerhaft gespeichert und direkt an der Instanz angezeigt. Start und Neustart verdecken sie nicht mehr mit „Instanz nicht gefunden“.
- Vollständige Agenten-Heartbeats erkennen fehlende Instanzen und zeigen das aktive Datenverzeichnis; laufende Installationen werden dabei berücksichtigt.
- „Installation wiederholen“ verwendet dieselbe ID, erhält vorhandene JARs und Konfigurationen und kann eine verlorene lokale Zuordnung wiederherstellen. Fehlende JARs werden weiterhin über verifizierte Downloads installiert.
- Regressionstests prüfen fehlgeschlagene Velocity-Downloads, wiederholte Installationen, verlorene Zuordnungen, Dateierhalt und bestehende Datenbanken.

## Windows-Agent: Laufwerksmetriken

- Pfade werden vor `psutil.disk_usage` mit `os.fspath` in Strings umgewandelt, da das Windows-Backend keine `WindowsPath`-Objekte akzeptiert.
- Ein Regressionstest startet einen bereits gekoppelten Agenten mit der strikten Windows-Pfadgrenze, prüft den Heartbeat und bestätigt, dass die gespeicherte Kopplung erhalten bleibt.

## Korrektur des Agentenstarts

- Erreichbarkeit und Betriebsmodus der Zentrale werden vor der Kopplungscode-Abfrage geprüft.
- Verbindungs-, TLS-, Proxy- und Kopplungsfehler erhalten verständliche Meldungen; der Agentenstart und sein Installer erzeugen bei diesen Fehlern keinen doppelten Traceback mehr.
- Anleitung für Zentrale und Agent auf demselben Rechner sowie auf getrennten Geräten ergänzt.
- Regressionstests für den Verbindungsaufbau und Python 3.13 in der Linux-/Windows-CI ergänzt.

## 0.1.0

- Python-Zentrale mit Anmeldung, SQLite-Persistenz und einmaliger Agentenkopplung.
- Ausgehende Agentenverbindung, dauerhafte Aufträge, Ergebnisrückmeldungen und Java-Prozessverwaltung.
- Dunkelblau-violettes Dashboard mit Spieler-/Leistungswerten, interaktiver Netzwerkkarte und animierten Verbindungen.
- Paper-/Velocity-Installationsabläufe, Versionsauswahl, SHA-256-Downloadprüfung und Modern Forwarding.
- Konsole, Dateibrowser, UTF-8-Editor, Uploads, lokale Backups und wiederkehrende Routinen.
- Schutz gegen Pfadausbrüche und Symlinks, einschließlich Netzwerk-Konfigurationsdateien und Sicherungszielen.
- Neu angelegte Dateien überschreiben vorhandene Inhalte nicht; Editor und bestätigte Uploads können weiterhin speichern.
- Ein-Befehl-Installer, festgeschriebene Python-Abhängigkeiten mit Paket-Hashes sowie Linux-/Windows-Betriebsanleitungen.
- Getrennter Demomodus, automatisierte API-/Agententests, JVM-Integrationstest und Browser-Smoke-Test.
- GitHub-Actions-Matrix für Linux/Windows mit Python 3.11/3.12 und Java 21.

Bekannte Grenzen und die Schritte für den realen Minecraft-Betrieb stehen in der README. Das Projekt enthält noch kein Rollenmodell, automatisches Java-Provisioning oder containerbasierte Ressourcenisolation.
