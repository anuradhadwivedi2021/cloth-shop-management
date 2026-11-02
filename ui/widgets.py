"""Small reusable pieces shared by the screens."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)


class PasswordField(QWidget):
    """Password box with a Show / Hide button (useful when typing a long password)."""

    textChanged = Signal(str)
    returnPressed = Signal()

    def __init__(self, placeholder="", parent=None):
        super().__init__(parent)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText(placeholder)
        self.edit.textChanged.connect(self.textChanged)
        self.edit.returnPressed.connect(self.returnPressed)
        self.toggle = QPushButton("Show")
        self.toggle.setCheckable(True)
        self.toggle.setFocusPolicy(Qt.NoFocus)
        self.toggle.setFixedWidth(70)
        self.toggle.toggled.connect(self._toggled)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.edit, 1)
        row.addWidget(self.toggle)

    def _toggled(self, shown: bool) -> None:
        self.edit.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)
        self.toggle.setText("Hide" if shown else "Show")

    def text(self) -> str:
        return self.edit.text()

    def setText(self, value: str) -> None:
        self.edit.setText(value)

    def clear(self) -> None:
        self.edit.clear()
        self.toggle.setChecked(False)

    def setFocus(self, *args):
        self.edit.setFocus(*args)


def make_card(max_width=520) -> tuple:
    """A white rounded panel. Returns (frame, layout)."""
    frame = QFrame()
    frame.setObjectName("card")
    frame.setFixedWidth(max_width)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(32, 28, 32, 28)
    layout.setSpacing(14)
    return frame, layout


def centered(widget: QWidget) -> QWidget:
    """Page that keeps `widget` in the middle of the window."""
    page = QWidget()
    page.setObjectName("page")
    row = QHBoxLayout(page)
    row.addStretch(1)
    column = QVBoxLayout()
    column.addStretch(1)
    column.addWidget(widget)
    column.addStretch(1)
    row.addLayout(column)
    row.addStretch(1)
    return page


def label(text="", name=None, wrap=True) -> QLabel:
    item = QLabel(text)
    item.setWordWrap(wrap)
    if name:
        item.setObjectName(name)
    return item


def form() -> QFormLayout:
    layout = QFormLayout()
    layout.setLabelAlignment(Qt.AlignLeft)
    layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
    layout.setVerticalSpacing(12)
    return layout
