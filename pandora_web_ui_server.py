"""
pandora_web_ui_server.py
~~~~~~~~~~~~~~~~~~~~~~~~
Eigenständiger HTTP-Server auf Basis von ``http.server``, der als
Drop-in-Ersatz für ``python -m http.server`` dient, aber statt der
nackten Standard-Verzeichnisliste eine vollständige Web-Oberfläche im
Stil eines Datei-Explorers ausliefert:

    - Links: ein aufklappbarer Ordnerbaum (lazy-loaded per AJAX)
    - Rechts: Kachel-/Listenansicht der aktuellen Verzeichnisinhalte
      mit Icons, Dateigröße, Änderungsdatum, Sortierung und Live-Filter
    - Drag & Drop-Upload: Dateien vom Client-PC direkt in den Browser
      ziehen, um sie in das aktuell angezeigte Verzeichnis hochzuladen

Die eigentliche Dateiauslieferung (Download, Range-Requests, MIME-Type-
Erkennung) bleibt 1:1 die von ``http.server.SimpleHTTPRequestHandler``
geerbte Standard-Logik inkl. deren eingebautem Pfad-Traversal-Schutz
(``translate_path``). Es wird ausschließlich ``list_directory()``
überschrieben und um einen JSON-Endpunkt für den Ordnerbaum sowie einen
``POST``-Endpunkt für den Datei-Upload (multipart/form-data, per
Hand geparst - ohne das veraltete ``cgi``-Modul) ergänzt.

Wird von ``server_worker.py`` als Subprozess gestartet, genau wie
vorher ``python -m http.server`` - Kommandozeilen-Argumente:

    pandora_web_ui_server.py --host 0.0.0.0 --port 8000 --directory /pfad
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import sys
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "Pandora® Http File Server"

# ---------------------------------------------------------------------------
# Icon-Zuordnung nach Dateiendung (Emoji, damit keine externen Assets nötig
# sind - der Server bleibt vollständig eigenständig / single-file).
# ---------------------------------------------------------------------------
_ICON_MAP: dict[str, str] = {}


def _register(icon: str, *extensions: str) -> None:
    for ext in extensions:
        _ICON_MAP[ext] = icon


_register("🖼️", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tiff")
_register("🎵", ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma")
_register("🎬", ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".m4v")
_register("🗜️", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz")
_register("💻", ".py", ".js", ".ts", ".html", ".htm", ".css", ".c", ".cpp", ".h",
           ".java", ".go", ".rs", ".php", ".sh", ".json", ".xml", ".yaml", ".yml", ".sql")
_register("📄", ".pdf", ".doc", ".docx", ".odt", ".txt", ".md", ".rtf", ".log")
_register("📊", ".xls", ".xlsx", ".csv", ".ods")
_register("📽️", ".ppt", ".pptx", ".odp")
_register("⚙️", ".exe", ".msi", ".deb", ".rpm", ".apk", ".bin", ".appimage")
_ICON_FALLBACK = "📎"
_ICON_DIR = "📁"


def _icon_for(name: str, is_dir: bool) -> str:
    if is_dir:
        return _ICON_DIR
    ext = os.path.splitext(name)[1].lower()
    return _ICON_MAP.get(ext, _ICON_FALLBACK)


def _human_size(num_bytes: int) -> str:
    """Formatiert Byte-Werte menschenlesbar (KB/MB/GB)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _human_date(mtime: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime))


class PandoraExplorerHandler(SimpleHTTPRequestHandler):
    """Erweiterter Handler: Explorer-UI statt nackter Verzeichnisliste."""

    server_version = f"{APP_NAME}/1.0"

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802 (Signatur durch Basisklasse vorgegeben)
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if "pandora_children" in query:
            self._serve_children_json(parsed.path)
            return
        super().do_GET()

    # ------------------------------------------------------------------
    # Datei-Upload (Drag & Drop vom Client-Browser)
    # ------------------------------------------------------------------
    #: Sicherheitslimit pro Upload-Anfrage (Summe aller enthaltenen
    #: Dateien). Verhindert, dass eine einzelne Anfrage den Server-
    #: Prozess durch übermäßigen Speicherbedarf lahmlegt.
    MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        target_dir = self.translate_path(parsed.path)

        if not os.path.isdir(target_dir):
            self._json_response(404, {"error": "Zielordner nicht gefunden."})
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._json_response(400, {"error": "Erwartet wird multipart/form-data."})
            return

        boundary = self._extract_boundary(content_type)
        if not boundary:
            self._json_response(400, {"error": "Boundary im Content-Type fehlt."})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_response(400, {"error": "Ungültige Content-Length."})
            return

        if length <= 0:
            self._json_response(400, {"error": "Leere Anfrage."})
            return
        if length > self.MAX_UPLOAD_BYTES:
            self._json_response(
                413, {"error": f"Upload zu groß (Limit {_human_size(self.MAX_UPLOAD_BYTES)} pro Anfrage)."}
            )
            return

        body = self.rfile.read(length)

        try:
            files = self._parse_multipart_files(body, boundary)
        except ValueError as exc:
            self._json_response(400, {"error": f"Multipart-Daten fehlerhaft: {exc}"})
            return

        saved, skipped, errors = [], [], []
        for raw_name, data in files:
            # Nur den reinen Dateinamen übernehmen - schützt zusätzlich zum
            # ererbten translate_path()-Schutz auch gegen Pfadanteile
            # innerhalb des Dateinamens selbst (z. B. "../../evil.sh").
            safe_name = os.path.basename(raw_name.replace("\\", "/")).strip()
            if not safe_name or safe_name in (".", ".."):
                skipped.append(raw_name or "(unbenannt)")
                continue
            try:
                dest_path = self._unique_destination(target_dir, safe_name)
                with open(dest_path, "wb") as fh:
                    fh.write(data)
                saved.append(os.path.basename(dest_path))
            except OSError as exc:
                errors.append(f"{safe_name}: {exc}")

        if saved and not errors and not skipped:
            status = 200
        elif saved:
            status = 207  # Multi-Status: teilweiser Erfolg
        else:
            status = 400
        self._json_response(status, {"saved": saved, "skipped": skipped, "errors": errors})

    def _extract_boundary(self, content_type: str) -> bytes | None:
        for part in content_type.split(";")[1:]:
            part = part.strip()
            if part.startswith("boundary="):
                value = part[len("boundary="):]
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                return value.encode("utf-8")
        return None

    def _parse_multipart_files(self, body: bytes, boundary: bytes):
        """Zerlegt einen multipart/form-data-Body in (Dateiname, Bytes)-Paare.

        Bewusst als schlanker Hand-Parser umgesetzt statt über das seit
        Python 3.13 entfernte ``cgi``-Modul, damit das Skript ohne externe
        Abhängigkeiten und versionsunabhängig lauffähig bleibt. Formularfelder
        ohne ``filename`` (also keine echten Dateien) werden ignoriert.
        """
        delimiter = b"--" + boundary
        if delimiter not in body:
            raise ValueError("Boundary im Body nicht gefunden")

        segments = body.split(delimiter)
        results = []
        for segment in segments:
            if not segment or segment in (b"--\r\n", b"--", b"\r\n"):
                continue
            segment = segment[2:] if segment[:2] == b"\r\n" else segment
            if segment.startswith(b"--"):
                continue  # abschließender Delimiter der gesamten Anfrage

            header_end = segment.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            header_text = segment[:header_end].decode("utf-8", errors="replace")
            data = segment[header_end + 4:]
            if data.endswith(b"\r\n"):
                data = data[:-2]

            filename = None
            for line in header_text.split("\r\n"):
                lower = line.lower()
                if lower.startswith("content-disposition") and "filename=" in lower:
                    marker = "filename=\""
                    start = lower.find(marker)
                    if start == -1:
                        continue
                    start += len(marker)
                    end = line.find('"', start)
                    if end == -1:
                        continue
                    filename = line[start:end]

            if filename:
                results.append((filename, data))
        return results

    def _unique_destination(self, directory: str, name: str) -> str:
        """Verhindert stilles Überschreiben: hängt bei Namenskollision
        ``(1)``, ``(2)``, … vor die Dateiendung an (wie im Explorer)."""
        dest = os.path.join(directory, name)
        if not os.path.exists(dest):
            return dest
        base, ext = os.path.splitext(name)
        counter = 1
        while True:
            candidate = os.path.join(directory, f"{base} ({counter}){ext}")
            if not os.path.exists(candidate):
                return candidate
            counter += 1

    def _json_response(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # JSON-API für den lazy-geladenen Ordnerbaum
    # ------------------------------------------------------------------
    def _serve_children_json(self, url_path: str) -> None:
        fs_path = self.translate_path(url_path)  # nutzt den ererbten Traversal-Schutz
        try:
            children = []
            with os.scandir(fs_path) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            children.append(entry.name)
                    except OSError:
                        continue
            children.sort(key=str.lower)
            payload = json.dumps(children).encode("utf-8")
        except OSError:
            self.send_error(404, "Verzeichnis nicht gefunden")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ------------------------------------------------------------------
    # Verzeichnisansicht: komplett eigene Explorer-Oberfläche
    # ------------------------------------------------------------------
    def list_directory(self, path):  # noqa: A003 - Signatur durch Basisklasse vorgegeben
        try:
            entries = list(os.scandir(path))
        except OSError:
            self.send_error(404, "Kein Zugriff auf dieses Verzeichnis")
            return None

        entries = [e for e in entries if not e.name.startswith(".")]

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sort_key = query.get("sort", ["name"])[0]
        order = query.get("order", ["asc"])[0]
        reverse = order == "desc"

        def sort_value(entry: os.DirEntry):
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                is_dir, stat = False, None
            if sort_key == "size":
                return (0 if is_dir else 1, stat.st_size if stat else 0, entry.name.lower())
            if sort_key == "date":
                return (stat.st_mtime if stat else 0, entry.name.lower())
            return (entry.name.lower(),)

        # Ordner immer zuerst, außer bei explizit gewählter Größen-/Datumssortierung
        # (dort zählt der reine Wert, Ordner werden dann wie bei Größe=0 einsortiert).
        if sort_key in ("name",):
            entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), sort_value(e)))
        else:
            entries.sort(key=sort_value)
        if reverse:
            entries.reverse()

        url_path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        html_bytes = self._render_page(path, entries, url_path, sort_key, order).encode(
            "utf-8", errors="surrogateescape"
        )

        buffer = io.BytesIO(html_bytes)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        return buffer

    # ------------------------------------------------------------------
    # HTML-Rendering
    # ------------------------------------------------------------------
    def _render_page(self, fs_path, entries, url_path, sort_key, order) -> str:
        rel_dir = os.path.relpath(fs_path, self.directory).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""

        breadcrumb_html = self._render_breadcrumb(rel_dir)
        tree_html = self._render_tree(rel_dir)
        cards_html, total_files, total_dirs, total_size = self._render_entries(entries, url_path)
        up_link = self._build_up_link(url_path)

        return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(APP_NAME)} — /{html.escape(rel_dir)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="app" id="pandoraApp" data-current-dir="{html.escape(url_path)}">
  <aside class="sidebar">
    <div class="brand">📦 <span>Pandora<sup>®</sup></span></div>
    <div class="tree-scroll">
      <ul class="tree">{tree_html}</ul>
    </div>
  </aside>
  <main class="content">
    <header class="toolbar">
      <div class="breadcrumb">{breadcrumb_html}</div>
      <div class="tools">
        <input id="filterInput" type="text" placeholder="🔍 Filtern…" oninput="pandoraFilter()">
        <button class="view-btn" id="btnGrid" onclick="pandoraSetView('grid')" title="Kachelansicht">▦</button>
        <button class="view-btn" id="btnList" onclick="pandoraSetView('list')" title="Listenansicht">☰</button>
      </div>
    </header>
    <div class="sort-row">
      Sortieren:
      {self._render_sort_link("Name", "name", sort_key, order, url_path)}
      {self._render_sort_link("Größe", "size", sort_key, order, url_path)}
      {self._render_sort_link("Datum", "date", sort_key, order, url_path)}
      <span class="upload-hint">⬇️ Dateien per Drag &amp; Drop hier ablegen zum Hochladen</span>
    </div>
    {up_link}
    <div id="explorer" class="explorer grid">
      {cards_html}
    </div>
    <footer class="statusbar">
      {total_dirs} Ordner · {total_files} Dateien · {_human_size(total_size)} gesamt
    </footer>
  </main>
  <div id="dropOverlay" class="drop-overlay">
    <div class="drop-box">📥 Dateien hier ablegen<br><span>Upload nach /{html.escape(rel_dir)}</span></div>
  </div>
  <div id="uploadPanel" class="upload-panel"></div>
</div>
<script>{_JS}</script>
</body>
</html>
"""

    def _render_sort_link(self, label, key, active_key, active_order, url_path) -> str:
        next_order = "asc"
        arrow = ""
        if key == active_key:
            next_order = "desc" if active_order == "asc" else "asc"
            arrow = " ▲" if active_order == "asc" else " ▼"
        href = f"{url_path}?sort={key}&order={next_order}"
        css_class = "sort-link active" if key == active_key else "sort-link"
        return f'<a class="{css_class}" href="{html.escape(href)}">{html.escape(label)}{arrow}</a>'

    def _build_up_link(self, url_path: str) -> str:
        if url_path in ("/", ""):
            return ""
        trimmed = url_path.rstrip("/")
        parent = trimmed.rsplit("/", 1)[0] + "/"
        return f'<a class="up-link" href="{html.escape(parent)}">⬅ Übergeordneter Ordner</a>'

    def _render_breadcrumb(self, rel_dir: str) -> str:
        parts = [p for p in rel_dir.split("/") if p]
        crumbs = ['<a href="/">🏠 Basis</a>']
        acc = ""
        for part in parts:
            acc += part + "/"
            href = "/" + urllib.parse.quote(acc)
            crumbs.append(f'<a href="{html.escape(href)}">{html.escape(part)}</a>')
        return " <span class='sep'>/</span> ".join(crumbs)

    def _render_entries(self, entries, url_path: str):
        cards = []
        total_files = total_dirs = total_size = 0

        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue

            name = entry.name
            display_name = name + ("/" if is_dir else "")
            href_name = urllib.parse.quote(name, errors="surrogatepass") + ("/" if is_dir else "")
            href = url_path.rstrip("/") + "/" + href_name if url_path != "/" else "/" + href_name

            icon = _icon_for(name, is_dir)
            size_label = "—" if is_dir else _human_size(stat.st_size)
            date_label = _human_date(stat.st_mtime)

            if is_dir:
                total_dirs += 1
            else:
                total_files += 1
                total_size += stat.st_size

            cards.append(f"""
            <a class="entry {'is-dir' if is_dir else 'is-file'}" href="{html.escape(href)}"
               data-name="{html.escape(display_name.lower())}">
              <div class="entry-icon">{icon}</div>
              <div class="entry-name" title="{html.escape(display_name)}">{html.escape(display_name)}</div>
              <div class="entry-meta">
                <span class="entry-size">{html.escape(size_label)}</span>
                <span class="entry-date">{html.escape(date_label)}</span>
              </div>
            </a>""")

        if not cards:
            cards.append('<div class="empty-hint">📭 Dieser Ordner ist leer.</div>')

        return "".join(cards), total_files, total_dirs, total_size

    # ------------------------------------------------------------------
    # Ordnerbaum (Sidebar) - Wurzel + Ahnenkette aufgeklappt, Rest lazy
    # ------------------------------------------------------------------
    def _render_tree(self, active_rel: str) -> str:
        ancestors = set()
        acc = ""
        ancestors.add("")
        if active_rel:
            for part in active_rel.split("/"):
                acc = f"{acc}/{part}" if acc else part
                ancestors.add(acc)
        return self._render_tree_node("", ancestors, active_rel)

    def _render_tree_node(self, rel_dir: str, ancestors: set, active_rel: str) -> str:
        abs_dir = self.directory if rel_dir == "" else os.path.join(self.directory, rel_dir)
        try:
            subdirs = sorted(
                (e.name for e in os.scandir(abs_dir)
                 if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)),
                key=str.lower,
            )
        except OSError:
            subdirs = []

        items = []
        for name in subdirs:
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            is_active = child_rel == active_rel
            is_ancestor = child_rel in ancestors
            href = "/" + urllib.parse.quote(child_rel) + "/"
            css = "tree-link"
            if is_active:
                css += " active"

            if is_ancestor:
                children_html = self._render_tree_node(child_rel, ancestors, active_rel)
                items.append(f"""
                <li class="tree-node open" data-path="{html.escape(child_rel)}" data-loaded="true">
                  <span class="toggle" onclick="pandoraToggle(this)">▾</span>
                  <a class="{css}" href="{html.escape(href)}">📁 {html.escape(name)}</a>
                  <ul class="tree-children">{children_html}</ul>
                </li>""")
            else:
                items.append(f"""
                <li class="tree-node" data-path="{html.escape(child_rel)}" data-loaded="false">
                  <span class="toggle" onclick="pandoraToggle(this)">▸</span>
                  <a class="{css}" href="{html.escape(href)}">📁 {html.escape(name)}</a>
                  <ul class="tree-children"></ul>
                </li>""")

        if rel_dir == "":
            root_href = "/"
            root_active = "active" if active_rel == "" else ""
            inner = "".join(items)
            return f"""
            <li class="tree-node open" data-path="" data-loaded="true">
              <span class="toggle" onclick="pandoraToggle(this)">▾</span>
              <a class="tree-link {root_active}" href="{root_href}">🏠 Basis</a>
              <ul class="tree-children">{inner}</ul>
            </li>"""

        return "".join(items)

    # ------------------------------------------------------------------
    # Logging: identisches Format wie das originale http.server-Modul,
    # damit server_worker.py die Zeilen weiterhin unverändert ins
    # GUI-Log durchreicht.
    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("%s - - [%s] %s\n" % (
            self.address_string(), self.log_date_time_string(), fmt % args
        ))


_CSS = """
:root {
    --bg: #120b0d;
    --bg-alt: #1b1114;
    --panel: #201316;
    --border: #3a1b21;
    --text: #f1e4e6;
    --text-dim: #b98d93;
    --accent: #ff2d55;
    --accent-dim: #7a1830;
    --glow: 0 0 10px rgba(255, 45, 85, 0.35);
}
* { box-sizing: border-box; }
body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "Segoe UI", Consolas, sans-serif; font-size: 14px;
}
.app { display: flex; height: 100vh; }
.sidebar {
    width: 280px; min-width: 220px; background: var(--bg-alt);
    border-right: 1px solid var(--border); display: flex; flex-direction: column;
}
.brand {
    padding: 16px; font-size: 18px; font-weight: 700; color: var(--accent);
    border-bottom: 1px solid var(--border); text-shadow: var(--glow);
}
.brand sup { font-size: 11px; }
.tree-scroll { overflow-y: auto; flex: 1; padding: 8px 6px; }
ul.tree, ul.tree-children { list-style: none; margin: 0; padding-left: 14px; }
ul.tree { padding-left: 0; }
.tree-node { margin: 2px 0; }
.tree-children { display: none; }
.tree-node.open > .tree-children { display: block; }
.toggle { display: inline-block; width: 14px; cursor: pointer; color: var(--text-dim); user-select: none; }
.tree-link {
    color: var(--text); text-decoration: none; padding: 2px 4px; border-radius: 4px;
}
.tree-link:hover { background: var(--accent-dim); }
.tree-link.active { background: var(--accent); color: #fff; font-weight: 600; }
.content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.toolbar {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 20px; border-bottom: 1px solid var(--border); background: var(--bg-alt);
    gap: 12px; flex-wrap: wrap;
}
.breadcrumb a { color: var(--text-dim); text-decoration: none; }
.breadcrumb a:hover { color: var(--accent); }
.breadcrumb .sep { color: var(--border); }
.tools { display: flex; gap: 8px; align-items: center; }
#filterInput {
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    padding: 6px 10px; border-radius: 6px; outline: none; width: 200px;
}
#filterInput:focus { border-color: var(--accent); }
.view-btn {
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 6px 10px; cursor: pointer;
}
.view-btn:hover { border-color: var(--accent); color: var(--accent); }
.sort-row { padding: 8px 20px; font-size: 12px; color: var(--text-dim); display: flex; gap: 14px; }
.sort-link { color: var(--text-dim); text-decoration: none; }
.sort-link.active { color: var(--accent); font-weight: 600; }
.up-link {
    margin: 0 20px 8px 20px; display: inline-block; color: var(--text-dim);
    text-decoration: none; font-size: 13px;
}
.up-link:hover { color: var(--accent); }
.explorer { flex: 1; overflow-y: auto; padding: 16px 20px; }
.explorer.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 14px; }
.explorer.list { display: flex; flex-direction: column; gap: 2px; }
.entry {
    color: var(--text); text-decoration: none; background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; padding: 12px 8px;
    display: flex; flex-direction: column; align-items: center; text-align: center;
    transition: border-color .15s, transform .1s;
}
.entry:hover { border-color: var(--accent); box-shadow: var(--glow); }
.entry.is-dir { color: #ffd7de; }
.entry-icon { font-size: 30px; margin-bottom: 6px; }
.entry-name { font-size: 12px; word-break: break-word; max-width: 100%; }
.entry-meta { font-size: 10px; color: var(--text-dim); margin-top: 4px; display: flex; gap: 6px; }
.explorer.list .entry {
    flex-direction: row; justify-content: flex-start; text-align: left; padding: 8px 12px; gap: 12px;
}
.explorer.list .entry-icon { font-size: 18px; margin: 0; }
.explorer.list .entry-name { flex: 1; }
.explorer.list .entry-meta { flex-direction: row; gap: 16px; min-width: 220px; justify-content: flex-end; }
.empty-hint { color: var(--text-dim); padding: 40px; text-align: center; width: 100%; }
.statusbar {
    border-top: 1px solid var(--border); background: var(--bg-alt);
    padding: 8px 20px; font-size: 12px; color: var(--text-dim);
}
.upload-hint { margin-left: auto; color: var(--text-dim); font-size: 11px; opacity: .8; }

/* Drag&Drop-Overlay: liegt über der gesamten App, wird nur während des
   Ziehens von Dateien über das Fenster sichtbar. */
.drop-overlay {
    position: fixed; inset: 0; z-index: 50; display: none;
    align-items: center; justify-content: center;
    background: rgba(18, 11, 13, 0.88);
    border: 3px dashed var(--accent);
}
.drop-overlay.active { display: flex; }
.drop-box {
    text-align: center; font-size: 22px; color: var(--text);
    padding: 40px 60px; border-radius: 12px; background: var(--panel);
    box-shadow: var(--glow);
}
.drop-box span { font-size: 13px; color: var(--text-dim); }

/* Upload-Fortschritts-Panel unten rechts */
.upload-panel {
    position: fixed; right: 20px; bottom: 20px; z-index: 60;
    display: flex; flex-direction: column; gap: 8px; max-width: 320px;
}
.upload-item {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; font-size: 12px; color: var(--text);
}
.upload-item .upload-name { display: flex; justify-content: space-between; margin-bottom: 6px; word-break: break-all; }
.upload-item .upload-bar-track {
    background: var(--bg); border-radius: 4px; height: 6px; overflow: hidden;
}
.upload-item .upload-bar-fill {
    background: var(--accent); height: 100%; width: 0%; transition: width .15s;
}
.upload-item.done .upload-bar-fill { background: #4ade80; width: 100%; }
.upload-item.error .upload-bar-fill { background: #f87171; width: 100%; }
.upload-item .upload-status { margin-top: 4px; color: var(--text-dim); font-size: 11px; }
"""

_JS = """
function pandoraSetView(mode) {
    document.getElementById('explorer').className = 'explorer ' + mode;
    localStorage.setItem('pandora_view', mode);
}
(function () {
    var saved = localStorage.getItem('pandora_view');
    if (saved) { document.getElementById('explorer').className = 'explorer ' + saved; }
})();

function pandoraFilter() {
    var term = document.getElementById('filterInput').value.trim().toLowerCase();
    var entries = document.querySelectorAll('#explorer .entry');
    entries.forEach(function (el) {
        var name = el.getAttribute('data-name') || '';
        el.style.display = (!term || name.indexOf(term) !== -1) ? '' : 'none';
    });
}

function pandoraToggle(span) {
    var li = span.parentElement;
    var loaded = li.getAttribute('data-loaded') === 'true';
    var isOpen = li.classList.contains('open');

    if (isOpen) {
        li.classList.remove('open');
        span.textContent = '▸';
        return;
    }

    if (loaded) {
        li.classList.add('open');
        span.textContent = '▾';
        return;
    }

    var path = li.getAttribute('data-path');
    var url = '/' + path + (path ? '/' : '') + '?pandora_children=1';
    fetch(url).then(function (r) { return r.json(); }).then(function (names) {
        var ul = li.querySelector('.tree-children');
        ul.innerHTML = '';
        names.forEach(function (name) {
            var childPath = path ? (path + '/' + name) : name;
            var childHref = '/' + encodeURIComponent(childPath).replace(/%2F/g, '/') + '/';
            var childLi = document.createElement('li');
            childLi.className = 'tree-node';
            childLi.setAttribute('data-path', childPath);
            childLi.setAttribute('data-loaded', 'false');
            childLi.innerHTML =
                '<span class="toggle" onclick="pandoraToggle(this)">▸</span>' +
                '<a class="tree-link" href="' + childHref + '">📁 ' + name + '</a>' +
                '<ul class="tree-children"></ul>';
            ul.appendChild(childLi);
        });
        li.setAttribute('data-loaded', 'true');
        li.classList.add('open');
        span.textContent = '▾';
    }).catch(function () {
        span.textContent = '▸';
    });
}

/* ------------------------------------------------------------------
 * Drag & Drop-Upload: Dateien vom Client-PC in den aktuell angezeigten
 * Ordner ziehen. Ordner-Drops werden erkannt und mit Hinweis
 * übersprungen (nur einzelne Dateien werden unterstützt).
 * ------------------------------------------------------------------ */
(function () {
    var appEl = document.getElementById('pandoraApp');
    var overlay = document.getElementById('dropOverlay');
    var panel = document.getElementById('uploadPanel');
    if (!appEl || !overlay || !panel) { return; }

    var currentDir = appEl.getAttribute('data-current-dir') || '/';
    var dragCounter = 0;
    var pendingUploads = 0;
    var anySuccess = false;

    function hasFiles(e) {
        return e.dataTransfer && e.dataTransfer.types &&
            Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') !== -1;
    }

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(function (evt) {
        window.addEventListener(evt, function (e) { e.preventDefault(); e.stopPropagation(); }, false);
    });

    window.addEventListener('dragenter', function (e) {
        if (!hasFiles(e)) { return; }
        dragCounter++;
        overlay.classList.add('active');
    });
    window.addEventListener('dragleave', function () {
        dragCounter = Math.max(0, dragCounter - 1);
        if (dragCounter === 0) { overlay.classList.remove('active'); }
    });
    window.addEventListener('drop', function (e) {
        dragCounter = 0;
        overlay.classList.remove('active');
        if (hasFiles(e)) { handleDrop(e.dataTransfer); }
    });

    function handleDrop(dataTransfer) {
        var files = [];
        var rejectedFolders = 0;

        if (dataTransfer.items && dataTransfer.items.length && dataTransfer.items[0].webkitGetAsEntry) {
            for (var i = 0; i < dataTransfer.items.length; i++) {
                var entry = dataTransfer.items[i].webkitGetAsEntry ? dataTransfer.items[i].webkitGetAsEntry() : null;
                if (entry && entry.isDirectory) {
                    rejectedFolders++;
                } else {
                    var f = dataTransfer.items[i].getAsFile ? dataTransfer.items[i].getAsFile() : null;
                    if (f) { files.push(f); }
                }
            }
        } else if (dataTransfer.files) {
            for (var j = 0; j < dataTransfer.files.length; j++) { files.push(dataTransfer.files[j]); }
        }

        if (rejectedFolders > 0) {
            pandoraNotify('⚠️ ' + rejectedFolders + ' Ordner übersprungen – nur einzelne Dateien werden unterstützt.', true);
        }
        files.forEach(uploadFile);
    }

    function uploadFile(file) {
        pendingUploads++;

        var item = document.createElement('div');
        item.className = 'upload-item';
        item.innerHTML =
            '<div class="upload-name"><span></span><span class="upload-pct">0%</span></div>' +
            '<div class="upload-bar-track"><div class="upload-bar-fill"></div></div>' +
            '<div class="upload-status">Wird hochgeladen…</div>';
        item.querySelector('.upload-name span').textContent = file.name;
        panel.appendChild(item);

        var fill = item.querySelector('.upload-bar-fill');
        var pct = item.querySelector('.upload-pct');
        var status = item.querySelector('.upload-status');

        var formData = new FormData();
        formData.append('files', file, file.name);

        var xhr = new XMLHttpRequest();
        xhr.open('POST', currentDir, true);
        xhr.upload.onprogress = function (e) {
            if (e.lengthComputable) {
                var percent = Math.round((e.loaded / e.total) * 100);
                fill.style.width = percent + '%';
                pct.textContent = percent + '%';
            }
        };
        xhr.onload = function () {
            var result = {};
            try { result = JSON.parse(xhr.responseText); } catch (err) { /* ignore */ }
            var ok = (xhr.status === 200 || xhr.status === 207) && result.saved && result.saved.length;
            if (ok) {
                item.classList.add('done');
                status.textContent = '✅ Hochgeladen als ' + result.saved[0];
                anySuccess = true;
            } else {
                item.classList.add('error');
                var msg = (result.errors && result.errors[0]) || result.error || ('Fehler ' + xhr.status);
                status.textContent = '❌ ' + msg;
            }
            finishOne();
        };
        xhr.onerror = function () {
            item.classList.add('error');
            status.textContent = '❌ Netzwerkfehler beim Upload';
            finishOne();
        };
        xhr.send(formData);
    }

    function finishOne() {
        pendingUploads--;
        if (pendingUploads <= 0 && anySuccess) {
            setTimeout(function () { location.reload(); }, 700);
        }
    }

    function pandoraNotify(text, isError) {
        var item = document.createElement('div');
        item.className = 'upload-item' + (isError ? ' error' : '');
        var status = document.createElement('div');
        status.className = 'upload-status';
        status.textContent = text;
        item.appendChild(status);
        panel.appendChild(item);
        setTimeout(function () { item.remove(); }, 6000);
    }
})();
"""


def _build_server(host: str, port: int, directory: str) -> ThreadingHTTPServer:
    handler = partial(PandoraExplorerHandler, directory=directory)
    return ThreadingHTTPServer((host, port), handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - Web-UI-Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--directory", default=os.getcwd())
    args = parser.parse_args()

    directory = os.path.abspath(args.directory)
    httpd = _build_server(args.host, args.port, directory)

    # Ausgabe im selben Stil wie das originale ``python -m http.server``,
    # damit bestehende Log-Auswertung/Erwartungen unverändert bleiben.
    print(f"Serving HTTP on {args.host} port {args.port} "
          f"(http://{args.host}:{args.port}/) - Verzeichnis: {directory}")
    sys.stdout.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
