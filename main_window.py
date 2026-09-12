"""
main_window.py
~~~~~~~~~~~~~~
Hauptfenster der Anwendung: Eingabefelder, Buttons,
ProgressBar, Log-Fenster und Tray-Integration.

Mehrsprachigkeit läuft über `language_plugin.language_manager.LanguageManager`
(Singleton importiert aus `i18n.py`, identisches Prinzip wie in den anderen
Pandora®-Tools). Die Sprache wird unter Einstellungen -> Allgemein
umgeschaltet, wirkt sofort auf die gesamte Oberfläche (Retranslate ohne
Neustart) und wird in ``~/.pandora_http_file_server.json`` gespeichert.
"""

import os
import re
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton,
    QTextEdit, QProgressBar, QFileDialog, QMessageBox,
    QSystemTrayIcon, QMenu, QGroupBox, QApplication,
    QDialog, QDialogButtonBox, QComboBox, QTabWidget,
    QRadioButton, QButtonGroup,
)
from PyQt6.QtGui import QAction, QIcon, QCloseEvent, QFont
from PyQt6.QtCore import Qt

from server_worker import HttpServerWorker
from i18n import lm, config, save_config

APP_TITLE = "Pandora® Http File Server"

# Einfache IPv4-Validierung
IPV4_REGEX = re.compile(
    r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$"
)


# ------------------------------------------------------------------
# Einstellungsdialog: Einstellungen -> Allgemein -> Sprachauswahl
# ------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, main_window: "MainWindow", parent=None):
        super().__init__(parent)
        self._main_window = main_window
        self.setWindowTitle(lm.tr("settings.dialog.title"))
        self.setMinimumWidth(420)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, stretch=1)

        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)

        lang_row = QHBoxLayout()
        self.language_caption = QLabel(lm.tr("settings.general.language_label"))
        lang_row.addWidget(self.language_caption)

        self.language_combo = QComboBox()
        self._populate_languages()
        lang_row.addWidget(self.language_combo, stretch=1)
        general_layout.addLayout(lang_row)

        self.language_hint = QLabel(lm.tr("settings.general.language_hint"))
        self.language_hint.setWordWrap(True)
        self.language_hint.setStyleSheet("color: #94a3b8; font-style: italic; font-size: 11px;")
        general_layout.addWidget(self.language_hint)
        general_layout.addStretch(1)

        self.tabs.addTab(general_tab, lm.tr("settings.general.tab"))

        # ---- Tab: Netzwerk (Standard-Serverbindung) ----
        network_tab = QWidget()
        network_layout = QVBoxLayout(network_tab)

        self.network_hint = QLabel(lm.tr("settings.network.hint"))
        self.network_hint.setWordWrap(True)
        self.network_hint.setStyleSheet("color: #94a3b8; font-style: italic; font-size: 11px;")
        network_layout.addWidget(self.network_hint)

        self.bind_group = QButtonGroup(self)
        self.bind_all_radio = QRadioButton(lm.tr("settings.network.bind_all"))
        self.bind_local_radio = QRadioButton(lm.tr("settings.network.bind_local"))
        self.bind_group.addButton(self.bind_all_radio)
        self.bind_group.addButton(self.bind_local_radio)
        network_layout.addWidget(self.bind_all_radio)
        network_layout.addWidget(self.bind_local_radio)
        network_layout.addStretch(1)

        # Aktuellen Wert aus dem Hauptfenster übernehmen. Weicht die
        # aktuell eingetragene IP von beiden Presets ab (z. B. eine
        # manuell eingegebene Adresse), bleibt bewusst keiner der
        # beiden Radiobuttons markiert - so wird beim Bestätigen nichts
        # ungefragt überschrieben.
        current_ip = self._main_window.ip_edit.text().strip()
        if current_ip == "0.0.0.0":
            self.bind_all_radio.setChecked(True)
        elif current_ip == "127.0.0.1":
            self.bind_local_radio.setChecked(True)

        self.tabs.addTab(network_tab, lm.tr("settings.network.tab"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.ok_button.setText(lm.tr("common.ok"))
        self.cancel_button.setText(lm.tr("common.cancel"))
        root.addWidget(buttons)

    def _populate_languages(self):
        self.language_combo.clear()
        languages = lm.available_languages()
        for code, meta in sorted(languages.items(), key=lambda item: item[1]["name"]):
            flag = meta.get("flag", "")
            display = f"{flag}  {meta['name']}".strip()
            self.language_combo.addItem(display, userData=code)
            if code == lm.current_language:
                self.language_combo.setCurrentIndex(self.language_combo.count() - 1)

    def _on_accept(self):
        code = self.language_combo.currentData()
        if code and code != lm.current_language:
            lm.set_language(code)
            config["language"] = code
            save_config(config)
            self._main_window.retranslate_ui()
            meta = lm.available_languages().get(code, {})
            self._main_window._log(
                lm.tr("log.language_changed", name=meta.get("name", code)), "info"
            )

        new_host = None
        if self.bind_all_radio.isChecked():
            new_host = "0.0.0.0"
        elif self.bind_local_radio.isChecked():
            new_host = "127.0.0.1"

        if new_host and new_host != self._main_window.ip_edit.text().strip():
            self._main_window.ip_edit.setText(new_host)
            config["default_bind_host"] = new_host
            save_config(config)
            self._main_window._log(
                lm.tr("log.bind_changed", host=new_host), "info"
            )

        self.accept()


class MainWindow(QMainWindow):
    """Hauptfenster der Pandora® Http File Server GUI."""

    def __init__(self) -> None:
        super().__init__()
        self.worker: HttpServerWorker | None = None
        self._setup_window()
        self._build_ui()
        self._build_menu()
        self._setup_tray()
        self._apply_styles()

    # ------------------------------------------------------------------
    # Fenster-Setup
    # ------------------------------------------------------------------
    def _setup_window(self) -> None:
        self.setWindowTitle(APP_TITLE)
        self.resize(620, 560)
        self.setMinimumSize(520, 480)

    # ------------------------------------------------------------------
    # UI-Aufbau
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Titel (Markenname - bleibt unübersetzt)
        title = QLabel(f"📦  {APP_TITLE}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size:20px; font-weight:700; padding:8px;")
        root.addWidget(title)

        # ---- Gruppe: Verzeichnis ----
        self.dir_group = QGroupBox(lm.tr("ui.dir_group"))
        dir_lay = QHBoxLayout(self.dir_group)
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText(lm.tr("ui.dir_placeholder"))
        self.dir_edit.setText(os.path.expanduser("~"))
        self.dir_edit.setReadOnly(True)
        self.btn_browse = QPushButton(lm.tr("ui.browse_btn"))
        self.btn_browse.clicked.connect(self._browse_dir)
        dir_lay.addWidget(self.dir_edit)
        dir_lay.addWidget(self.btn_browse)
        root.addWidget(self.dir_group)

        # ---- Gruppe: Netzwerk ----
        self.net_group = QGroupBox(lm.tr("ui.net_group"))
        net_lay = QHBoxLayout(self.net_group)

        self.ip_label = QLabel(lm.tr("ui.ip_label"))
        net_lay.addWidget(self.ip_label)
        self.ip_edit = QLineEdit(config.get("default_bind_host", "0.0.0.0"))
        self.ip_edit.setMaxLength(15)
        net_lay.addWidget(self.ip_edit, 2)

        self.port_label = QLabel(lm.tr("ui.port_label"))
        net_lay.addWidget(self.port_label)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(8000)
        net_lay.addWidget(self.port_spin, 1)
        root.addWidget(self.net_group)

        # ---- Progress ----
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        root.addWidget(self.progress)

        # ---- Buttons ----
        btn_lay = QHBoxLayout()
        self.btn_start = QPushButton(lm.tr("ui.start_btn"))
        self.btn_stop = QPushButton(lm.tr("ui.stop_btn"))
        self.btn_stop.setEnabled(False)
        self.btn_start.clicked.connect(self._start_server)
        self.btn_stop.clicked.connect(self._stop_server)
        btn_lay.addWidget(self.btn_start)
        btn_lay.addWidget(self.btn_stop)
        btn_lay.addStretch()
        root.addLayout(btn_lay)

        # ---- Log ----
        self.log_group = QGroupBox(lm.tr("ui.log_group"))
        log_lay = QVBoxLayout(self.log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 10))
        log_lay.addWidget(self.log_view)
        root.addWidget(self.log_group, 1)

        self._log(lm.tr("log.ready"), "info")

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
        self.settings_menu = menu_bar.addMenu(lm.tr("menu.settings"))
        self.general_action = self.settings_menu.addAction(lm.tr("menu.settings.general"))
        self.general_action.triggered.connect(self._open_settings_dialog)

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self, parent=self)
        dialog.exec()

    def retranslate_ui(self) -> None:
        """Aktualisiert alle sichtbaren Texte der Oberfläche nach einem
        Sprachwechsel, ohne dass die Anwendung neu gestartet werden muss.
        Bereits geschriebene Log-Zeilen bleiben in der Sprache, in der sie
        entstanden sind - nur zukünftige Ausgaben nutzen die neue Sprache."""
        self.settings_menu.setTitle(lm.tr("menu.settings"))
        self.general_action.setText(lm.tr("menu.settings.general"))

        self.dir_group.setTitle(lm.tr("ui.dir_group"))
        self.dir_edit.setPlaceholderText(lm.tr("ui.dir_placeholder"))
        self.btn_browse.setText(lm.tr("ui.browse_btn"))
        self.net_group.setTitle(lm.tr("ui.net_group"))
        self.ip_label.setText(lm.tr("ui.ip_label"))
        self.port_label.setText(lm.tr("ui.port_label"))
        self.btn_start.setText(lm.tr("ui.start_btn"))
        self.btn_stop.setText(lm.tr("ui.stop_btn"))
        self.log_group.setTitle(lm.tr("ui.log_group"))

        if hasattr(self, "tray"):
            self.tray_show_action.setText(lm.tr("tray.show_action"))
            self.tray_quit_action.setText(lm.tr("tray.quit_action"))

    # ------------------------------------------------------------------
    # Styling (modernes Dark Theme via QSS)
    # ------------------------------------------------------------------
    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background:#202733; }
            QMenuBar { background:#202733; color:#e6edf3; border-bottom:1px solid #35404d; }
            QMenuBar::item:selected { background:#35404d; color:#6ee7b7; }
            QMenu { background:#2a333f; color:#e6edf3; border:1px solid #35404d; }
            QMenu::item:selected { background:#35404d; color:#6ee7b7; }
            QGroupBox {
                border:1px solid #35404d; border-radius:8px;
                margin-top:12px; padding:14px 10px 10px 10px;
                font-weight:600; color:#cbd5e1;
            }
            QGroupBox::title {
                subcontrol-origin: margin; left:12px; padding:0 6px;
                color:#6ee7b7;
            }
            QLabel { color:#e6edf3; }
            QLineEdit, QSpinBox {
                background:#0f141a; color:#e6edf3;
                border:1px solid #35404d; border-radius:6px;
                padding:6px 10px;
            }
            QLineEdit:focus, QSpinBox:focus { border-color:#60a5fa; }
            QPushButton {
                background:#2a333f; color:#e6edf3;
                border:1px solid #35404d; border-radius:6px;
                padding:8px 18px; font-weight:600;
            }
            QPushButton:hover:!disabled { background:#35404d; }
            QPushButton#btn_start { background:#16a34a; border-color:#16a34a; }
            QPushButton#btn_start:hover:!disabled { background:#15803d; }
            QPushButton#btn_stop  { background:#dc2626; border-color:#dc2626; }
            QPushButton#btn_stop:hover:!disabled { background:#b91c1c; }
            QPushButton:disabled  { color:#64748b; }
            QTextEdit {
                background:#0b0f14; color:#e6edf3;
                border:1px solid #35404d; border-radius:6px;
                padding:8px;
            }
            QProgressBar {
                background:#0f141a; border:1px solid #35404d;
                border-radius:4px; height:10px;
            }
            QProgressBar::chunk {
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 transparent, stop:0.5 #22c55e, stop:1 transparent);
                border-radius:4px;
            }
        """)
        self.btn_start.setObjectName("btn_start")
        self.btn_stop.setObjectName("btn_stop")

    # ------------------------------------------------------------------
    # Hilfsfunktionen
    # ------------------------------------------------------------------
    def _log(self, text: str, level: str = "info") -> None:
        colors = {
            "info": "#93c5fd", "ok": "#86efac",
            "warn": "#fde68a", "err": "#fca5a5",
        }
        color = colors.get(level, "#cbd5e1")
        self.log_view.append(f'<span style="color:{color}">{text}</span>')

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, lm.tr("dialog.browse_dir_title"), self.dir_edit.text()
        )
        if path:
            self.dir_edit.setText(path)

    def _validate(self) -> bool:
        ip = self.ip_edit.text().strip()
        if not IPV4_REGEX.match(ip):
            QMessageBox.warning(
                self,
                lm.tr("dialog.invalid_input_title"),
                lm.tr("dialog.invalid_ip_text", ip=ip),
            )
            return False
        if not os.path.isdir(self.dir_edit.text()):
            QMessageBox.warning(
                self,
                lm.tr("dialog.missing_dir_title"),
                lm.tr("dialog.missing_dir_text"),
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Server-Steuerung
    # ------------------------------------------------------------------
    def _start_server(self) -> None:
        if not self._validate():
            return
        if self.worker and self.worker.isRunning():
            return

        host = self.ip_edit.text().strip()
        port = self.port_spin.value()
        directory = self.dir_edit.text()

        self.worker = HttpServerWorker(host, port, directory)
        self.worker.log_signal.connect(lambda t: self._log(t, "info"))
        self.worker.started_signal.connect(self._on_started)
        self.worker.stopped_signal.connect(self._on_stopped)
        self.worker.error_signal.connect(lambda e: self._log(e, "err"))
        self.worker.start()

    def _stop_server(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
            self._log(lm.tr("log.stopped_manual"), "warn")

    def _on_started(self) -> None:
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.ip_edit.setEnabled(False)
        self.port_spin.setEnabled(False)
        self.btn_browse.setEnabled(False)
        self.progress.setRange(0, 0)  # Marquee / unbestimmt
        self._log(lm.tr("log.active"), "ok")

    def _on_stopped(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.ip_edit.setEnabled(True)
        self.port_spin.setEnabled(True)
        self.btn_browse.setEnabled(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

    # ------------------------------------------------------------------
    # System-Tray (beim Schließen in den Tray minimieren)
    # ------------------------------------------------------------------
    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        # Einfaches Standard-Icon (kein externes File nötig)
        icon = self.style().standardIcon(
            self.style().StandardPixmap.SP_ComputerIcon
        )
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(APP_TITLE)

        menu = QMenu()
        self.tray_show_action = QAction(lm.tr("tray.show_action"), self)
        self.tray_show_action.triggered.connect(self._show_from_tray)
        self.tray_quit_action = QAction(lm.tr("tray.quit_action"), self)
        self.tray_quit_action.triggered.connect(self._real_quit)

        menu.addAction(self.tray_show_action)
        menu.addSeparator()
        menu.addAction(self.tray_quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Schließen → in den Tray minimieren (nicht beenden)."""
        if hasattr(self, "tray") and self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                APP_TITLE,
                lm.tr("tray.minimized_message"),
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )
        else:
            self._real_quit()

    def _real_quit(self) -> None:
        """Wirkliches Beenden inkl. Server-Shutdown."""
        self._stop_server()
        QApplication.quit()
