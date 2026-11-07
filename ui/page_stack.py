"""The stack of menu pages, sized by the page that is open (not by the tallest page of all).

A QStackedWidget asks every page how small it can be and takes the biggest answer. One tall page
(for example Customers, about 1025 px high) then made the whole window taller than a laptop screen,
so the top of the window was cut off. Here the stack asks only the open page, and MainWindow puts
the stack inside a scroll area, so a page that is bigger than the window scrolls instead of
pushing the window off the screen.
"""

from PySide6.QtWidgets import QStackedWidget


class CurrentPageStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def minimumSizeHint(self):
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()

    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()