"""Start the Cloth Shop Management app:  python -m app.main"""

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from app.paths import data_folder
from ui.controller import AppController
from ui.theme import apply_theme


def _setup_logging(folder) -> None:
    """Technical details go to a local log file. Never passwords, never customer data."""
    folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=folder / "app.log", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    folder = data_folder()
    try:
        _setup_logging(folder)
    except OSError:
        pass
    qt = QApplication(sys.argv)
    apply_theme(qt)
    try:
        window = AppController(folder)
    except Exception:
        logging.getLogger("clothshop").exception("E-START: could not start")
        QMessageBox.critical(None, "Cloth Shop", "The app could not start (error E-START).")
        return 1
    window.show()
    return qt.exec()


if __name__ == "__main__":
    sys.exit(main())
