"""Screens plug in here, so several people can build different screens without touching each other's files.

To add or replace a screen, create ONE file  ui/screens/<any_name>.py  containing:

    MENU_TEXT = "New Sale / Billing"          # exactly one of the English menu texts in ui/main_window.py MENU

    def build(conn, session, folder) -> QWidget:
        ...                                    # return the finished screen

That is all. MainWindow finds the file by itself: no edit to main_window.py is needed, and a screen file
overrides the built-in page for that menu entry. If `build` or the import raises an error, only that page
shows an error message; every other screen keeps working.

A screen may also define   refresh(self)   on its widget; it is called each time the page is opened.
"""

import importlib
import logging
import pkgutil

log = logging.getLogger(__name__)

PACKAGE = "ui.screens"


def discover(package=None):
    """Find the screen files. Returns (screens, problems).

    screens:  {menu text: module}
    problems: texts describing files that are broken, have no MENU_TEXT/build, or claim a menu entry
              another file already has. (The first file in alphabetical order wins.)
    """
    name = package or PACKAGE
    screens, problems = {}, []
    try:
        pkg = importlib.import_module(name)
    except Exception as exc:  # no screens folder at all is fine
        return {}, [f"{name}: cannot be imported ({type(exc).__name__})"]
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        full = f"{name}.{info.name}"
        try:
            module = importlib.import_module(full)
        except Exception as exc:
            log.exception("Screen file %s could not be imported", full)
            problems.append(f"{info.name}: import failed ({type(exc).__name__})")
            continue
        menu_text, build = getattr(module, "MENU_TEXT", None), getattr(module, "build", None)
        if not isinstance(menu_text, str) or not callable(build):
            problems.append(f"{info.name}: needs MENU_TEXT (text) and build(conn, session, folder)")
            continue
        if menu_text in screens:
            problems.append(f"{info.name}: menu '{menu_text}' is already taken by {screens[menu_text].__name__}")
            continue
        screens[menu_text] = module
    return screens, problems


def check_menu(screens, valid_texts) -> list:
    """Problems for screen files whose MENU_TEXT is not a real menu entry."""
    return [f"{m.__name__}: '{text}' is not in the main menu" for text, m in screens.items() if text not in valid_texts]


def build_screen(text, conn, session, folder, *, package=None):
    """The plug-in screen for this menu entry, or None when there is no file for it."""
    screens, problems = discover(package)
    for problem in problems:
        log.warning("Screen plug-in problem: %s", problem)
    module = screens.get(text)
    return module.build(conn, session, folder) if module else None
