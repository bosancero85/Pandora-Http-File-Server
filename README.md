# Pandora® Http File Server

Grafische Oberfläche (PyQt6) für einen eigenen, im Hintergrund laufenden
HTTP-Datei-Server. Kompatibel mit **Windows** und **Linux**.

## ✨ Features

- 📁 Verzeichnis per Dialog auswählen
- 🌐 Freie Wahl von IPv4-Adresse und Port (validiert)
- ▶️ Start / ■ Stop mit Live-Statusanzeige
- 📜 Echtzeit-Log-Ausgabe der Server-Anfragen
- 🔄 Animierter Marquee-Progress wenn der Server läuft
- 🗂️ Beim Schließen in den System-Tray minimieren
- 🧵 Non-blocking via QThread + subprocess
- 🗺️ **Web-Oberfläche im Datei-Explorer-Stil** (statt nackter Verzeichnisliste):
  aufklappbarer Ordnerbaum links, Kachel-/Listenansicht rechts mit
  Datei-Icons, Größe, Änderungsdatum, Sortierung, Breadcrumb und Live-Filter
  (Dark-Red-Cyberpunk-Theme, siehe `pandora_web_ui_server.py`)
- ⚙️ **Einstellungen → Netzwerk:** Standard-Serverbindung per Radiobutton
  wählbar - „Alle Netzwerke (0.0.0.0)“ oder „Nur dieser Rechner (127.0.0.1)“;
  wird gespeichert und beim nächsten Start automatisch vorausgefüllt (eine
  manuell im Hauptfenster eingetragene IP bleibt davon unberührt)

## 🚀 Installation

```bash
pip install -r requirements.txt
```

## ▶️ Starten

```bash
python main.py
```

## 📐 Architektur

```
main.py                    → Entry-Point
main_window.py             → GUI (PyQt6)
server_worker.py           → QThread + subprocess (startet pandora_web_ui_server.py)
pandora_web_ui_server.py   → eigenständiger HTTP-Server mit Explorer-Web-UI
```

Die Trennung von GUI und Worker-Logik sorgt dafür, dass die
Oberfläche während des Serverbetriebs stets reaktionsfähig bleibt.
`server_worker.py` startet `pandora_web_ui_server.py` als Subprozess -
exakt wie zuvor `python -m http.server`, nur mit eigenem Handler statt
dem Standardmodul. Downloads, Range-Requests und MIME-Erkennung laufen
weiterhin über die geerbte `SimpleHTTPRequestHandler`-Logik inkl. deren
eingebautem Schutz vor Pfad-Traversal (`../`).

## 📄 Lizenz

MIT License
