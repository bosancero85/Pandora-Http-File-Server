"""
server_worker.py
~~~~~~~~~~~~~~~~
QThread-basierte Worker-Klasse, die den Python-HTTP-Server
im Hintergrund startet und per Signal mit der GUI kommuniziert.
"""

import os
import sys
import signal
import subprocess
from PyQt6.QtCore import QThread, pyqtSignal

from i18n import lm


class HttpServerWorker(QThread):
    """Führt 'python -m http.server' in einem separaten Thread aus."""

    # Signals für die GUI-Kommunikation
    log_signal = pyqtSignal(str)        # Log-Zeilen
    started_signal = pyqtSignal()       # Server läuft
    stopped_signal = pyqtSignal()       # Server beendet
    error_signal = pyqtSignal(str)      # Fehler aufgetreten

    def __init__(self, host: str, port: int, directory: str, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.directory = directory
        self.process: subprocess.Popen | None = None
        self._is_running = False

    def run(self) -> None:
        """Wird beim Start des Threads aufgerufen (nicht-blockierend für die GUI)."""
        # Python-Interpreter plattformabhängig bestimmen
        python_exe = sys.executable or ("python" if os.name == "nt" else "python3")

        # Eigenes Web-UI-Server-Skript (Sidebar-Ordnerbaum + Datei-Explorer-
        # Ansicht) statt des nackten "python -m http.server". Liegt im
        # selben Verzeichnis wie dieses Modul.
        script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "pandora_web_ui_server.py"
        )

        cmd = [
            python_exe, script_path,
            "--host", self.host,
            "--port", str(self.port),
            "--directory", self.directory,
        ]

        self.log_signal.emit(lm.tr("worker.log.start", cmd=" ".join(cmd)))

        try:
            # Prozess starten, stdout/stderr kombinieren, ungepuffert
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,                 # Zeilenpuffer
                cwd=self.directory,
            )
            self._is_running = True
            self.started_signal.emit()
            self.log_signal.emit(
                lm.tr("worker.log.running", host=self.host, port=self.port)
            )

            # Ausgabe zeilenweise lesen und an GUI senden
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if not self._is_running:
                    break
                self.log_signal.emit(line.rstrip())

            self.process.wait()

        except Exception as exc:
            self.error_signal.emit(lm.tr("worker.log.error", error=exc))
        finally:
            self._is_running = False
            self.stopped_signal.emit()
            self.log_signal.emit(lm.tr("worker.log.stopped"))

    def stop(self) -> None:
        """Beendet den Server-Prozess sauber."""
        self._is_running = False
        if self.process and self.process.poll() is None:
            try:
                # Unter Windows direkt terminieren, unter Unix SIGTERM
                if os.name == "nt":
                    self.process.terminate()
                else:
                    self.process.send_signal(signal.SIGTERM)
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()  # Hard-Fallback
            self.log_signal.emit(lm.tr("worker.log.terminated"))

