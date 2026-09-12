# -*- coding: utf-8 -*-
"""
language_manager.py
--------------------
Lädt die JSON-Sprachpakete aus ``language_packs/`` und stellt die
Mehrsprachigkeit des Pandora® Script Editors bereit - identisches Prinzip
wie im Pandora® AI Social Buddy, damit beide Tools dieselbe
Sprachpaket-Struktur teilen und sich Übersetzungen leicht zwischen ihnen
austauschen lassen.

Jedes Sprachpaket ist eine flache JSON-Datei mit einem ``_meta``-Block
(Sprachcode, Anzeigename, Flagge) und beliebig vielen ``"schluessel.pfad":
"Text"``-Einträgen. Neue Sprachen: einfach weitere ``<code>_<LAND>/<code>.json``
Datei in den Ordner legen - sie erscheint automatisch im Sprache-Menü,
ohne dass Code angepasst werden muss.

Fehlt ein Schlüssel in der aktiven Sprache, wird zuerst in der
Fallback-Sprache (Standard: Deutsch) und andernfalls beim Schlüssel selbst
nachgeschaut, damit die Oberfläche nie mit einer Exception abstürzt.
"""

import json
import os
from typing import Dict, Optional

LANGUAGE_PACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "language_packs")


class LanguageManager:
    """Zentrale Verwaltung aller Sprachpakete des Script Editors."""

    def __init__(self, packs_dir: Optional[str] = None, default_language: str = "de"):
        self.packs_dir = packs_dir or LANGUAGE_PACKS_DIR
        self.default_language = default_language
        self._current_code = default_language
        self._packs: Dict[str, dict] = {}
        self._meta: Dict[str, dict] = {}

        self.reload_available()

        if default_language not in self._packs and self._packs:
            self._current_code = next(iter(self._packs))

    # ------------------------------------------------------------------
    # Laden
    # ------------------------------------------------------------------
    def reload_available(self) -> None:
        """Scannt den language_packs-Ordner neu nach *.json Dateien.

        Unterstützt zwei Layouts, damit neue Sprachen einfach per Ordner
        ODER per einzelner Datei ergänzt werden können:
          - flach:       language_packs/de.json
          - je Ordner:   language_packs/de_DE/de.json
        Beide Varianten werden rekursiv (eine Ebene tief) eingelesen."""
        self._packs.clear()
        self._meta.clear()

        if not os.path.isdir(self.packs_dir):
            os.makedirs(self.packs_dir, exist_ok=True)
            return

        json_paths = []
        for entry in sorted(os.listdir(self.packs_dir)):
            full = os.path.join(self.packs_dir, entry)
            if os.path.isfile(full) and entry.endswith(".json"):
                json_paths.append(full)
            elif os.path.isdir(full):
                for filename in sorted(os.listdir(full)):
                    if filename.endswith(".json"):
                        json_paths.append(os.path.join(full, filename))

        for path in json_paths:
            filename = os.path.basename(path)
            fallback_code = os.path.splitext(filename)[0]
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue

            meta = data.get("_meta", {})
            code = meta.get("code", fallback_code)
            self._packs[code] = data
            self._meta[code] = {
                "code": code,
                "name": meta.get("name", code.upper()),
                "flag": meta.get("flag", ""),
            }

    def available_languages(self) -> Dict[str, dict]:
        """Liefert ``{"de": {"code": "de", "name": "Deutsch", "flag": "..."}}``."""
        return dict(self._meta)

    # ------------------------------------------------------------------
    # Aktive Sprache
    # ------------------------------------------------------------------
    @property
    def current_language(self) -> str:
        return self._current_code

    def set_language(self, code: str) -> bool:
        """Wechselt die aktive Sprache. Gibt False zurück, falls das
        Sprachpaket nicht existiert (Sprache bleibt dann unverändert)."""
        if code not in self._packs:
            return False
        self._current_code = code
        return True

    # ------------------------------------------------------------------
    # Übersetzen
    # ------------------------------------------------------------------
    def tr(self, key: str, **kwargs) -> str:
        """Übersetzt einen Schlüssel wie ``"menu.file"`` in den aktuell
        aktiven Sprachtext. Platzhalter im Text (``{app}``) werden per
        ``str.format`` mit ``kwargs`` ersetzt. Fehlt der Schlüssel
        komplett, wird der Schlüssel selbst als sichtbarer Platzhalter
        zurückgegeben."""
        text = self._lookup(key)
        if text is None:
            return key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError):
                return text
        return text

    def _lookup(self, key: str) -> Optional[str]:
        pack = self._packs.get(self._current_code, {})
        if key in pack:
            return pack[key]

        fallback = self._packs.get(self.default_language, {})
        if key in fallback:
            return fallback[key]

        for pack in self._packs.values():
            if key in pack:
                return pack[key]
        return None

    # Kurzform, damit Code `lm("menu.file")` statt `lm.tr("menu.file")`
    # schreiben kann, falls gewünscht.
    __call__ = tr
