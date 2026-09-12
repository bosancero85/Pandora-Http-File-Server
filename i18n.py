"""
i18n.py
~~~~~~~
Zentrale Sprachverwaltung des Pandora® Http File Server.

Ausgelagert in ein eigenes, sehr kleines Modul (statt direkt in
main_window.py), damit sowohl main_window.py als auch server_worker.py
denselben LanguageManager-Singleton importieren können, ohne einen
zirkulären Import zu erzeugen (main_window importiert HttpServerWorker
aus server_worker).

Mehrsprachigkeit läuft über `language_plugin.language_manager.LanguageManager`
(identisches Prinzip wie in den anderen Pandora®-Tools). Die Sprache wird
unter Einstellungen -> Allgemein umgeschaltet und in
``~/.pandora_http_file_server.json`` gespeichert.
"""

import json
import os

from language_plugin.language_manager import LanguageManager

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".pandora_http_file_server.json")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


config = load_config()

# Zentrale Sprachverwaltung - Fallback ist immer Deutsch, damit fehlende
# Übersetzungen in anderen Sprachpaketen die Oberfläche nie mit einem
# leeren/kaputten Text zurücklassen.
lm = LanguageManager(default_language="de")
lm.set_language(config.get("language", "de"))
