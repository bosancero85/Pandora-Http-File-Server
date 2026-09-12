"""
main.py
~~~~~~~
Entry-Point der Anwendung.
Startet die QApplication und zeigt das Hauptfenster.
"""

import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Pandora® Http File Server")
    app.setOrganizationName("Pandora")
    app.setStyle("Fusion")  # Konsistentes Look & Feel auf Windows + Linux

    # Moderne Standard-Schrift
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
