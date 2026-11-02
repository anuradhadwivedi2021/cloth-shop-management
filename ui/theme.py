"""One look for the whole app: clean, large, readable (SRS: easy for a non-technical owner)."""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

PRIMARY = "#1F4E79"
PRIMARY_DARK = "#163A5A"
ACCENT_BG = "#EAF1F8"
BORDER = "#C9D3DD"
TEXT = "#1B2733"
MUTED = "#5B6B7A"
ERROR = "#B3261E"
OK = "#1E7B3A"

STYLE = f"""
* {{ font-family: "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif; font-size: 11pt; color: {TEXT}; }}
QWidget#page {{ background: #F5F7FA; }}
QLabel#title {{ font-size: 20pt; font-weight: 600; color: {PRIMARY}; }}
QLabel#subtitle {{ color: {MUTED}; }}
QLabel#error {{ color: {ERROR}; }}
QLabel#ok {{ color: {OK}; }}
QLabel#hint {{ color: {MUTED}; font-size: 9.5pt; }}
QFrame#card {{ background: white; border: 1px solid {BORDER}; border-radius: 10px; }}
QLineEdit, QSpinBox, QPlainTextEdit {{
    background: white; border: 1px solid {BORDER}; border-radius: 6px; padding: 7px 9px; min-height: 22px;
}}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border: 2px solid {PRIMARY}; }}
QPushButton {{
    background: white; border: 1px solid {BORDER}; border-radius: 6px; padding: 8px 18px; min-height: 22px;
}}
QPushButton:hover {{ background: {ACCENT_BG}; }}
QPushButton:disabled {{ color: #9AA7B3; background: #EEF1F4; }}
QPushButton#primary {{ background: {PRIMARY}; color: white; border: 1px solid {PRIMARY_DARK}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {PRIMARY_DARK}; }}
QPushButton#primary:disabled {{ background: #9FB4C8; color: white; border-color: #9FB4C8; }}
QPushButton#link {{ border: none; background: transparent; color: {PRIMARY}; text-decoration: underline; padding: 4px; }}
QFrame#topbar {{ background: {PRIMARY}; }}
QFrame#topbar QLabel {{ color: white; }}
QFrame#topbar QPushButton {{ background: transparent; color: white; border: 1px solid #7C9CBA; padding: 5px 14px; }}
QFrame#topbar QPushButton:hover {{ background: {PRIMARY_DARK}; }}
QListWidget#menu {{ background: white; border: none; border-right: 1px solid {BORDER}; outline: 0; }}
QListWidget#menu::item {{ padding: 11px 16px; }}
QListWidget#menu::item:selected {{ background: {ACCENT_BG}; color: {PRIMARY}; border-left: 4px solid {PRIMARY}; }}
QLabel#recovery {{
    font-family: "Consolas", "DejaVu Sans Mono", monospace; font-size: 17pt; font-weight: 600;
    background: #FFF8E1; border: 2px dashed #C99A06; border-radius: 8px; padding: 16px; letter-spacing: 1px;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 11))
    app.setStyleSheet(STYLE)
