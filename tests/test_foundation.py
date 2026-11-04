"""The foundation that lets several people add screens, migrations and translations without clashing."""

import gc
import os
import sys
import textwrap
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from core import shop  # noqa: E402
from core.settings import set_setting  # noqa: E402
from db import schema  # noqa: E402
from ui import i18n, main_window as mw, print_service, screens  # noqa: E402
from ui.i18n import tr  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_APP = QApplication.instance() or QApplication([])


def _dispose(widget):
    widget.setParent(None)
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _APP.processEvents()


@pytest.fixture(autouse=True)
def english_again():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


@pytest.fixture(scope="session")
def qapp():
    yield _APP
    for w in QApplication.topLevelWidgets():
        _dispose(w)
    gc.collect()


def make_package(tmp_path, name, files):
    """A throw-away Python package on disk: {file name: source}."""
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for fname, source in files.items():
        (pkg / fname).write_text(textwrap.dedent(source))
    sys.path.insert(0, str(tmp_path))
    return name


@pytest.fixture
def pkg_cleanup():
    before = set(sys.modules)
    path_before = list(sys.path)
    yield
    for m in set(sys.modules) - before:
        sys.modules.pop(m, None)
    sys.path[:] = path_before


@pytest.fixture
def window_for(tmp_path, qapp):
    made = []

    def build():
        folder = tmp_path / "shop"
        conn, session, _ = shop.setup_new_shop(folder, "owner", "owner-password", pin="1234")
        made.append(conn)
        return conn, session, folder

    yield build
    for conn in made:
        conn.close()


# ================================================================ screens plug-in

def test_a_screen_file_is_found_by_its_menu_text(tmp_path, pkg_cleanup):
    name = make_package(tmp_path, "fake_screens_a", {
        "billing.py": 'MENU_TEXT = "New Sale / Billing"\ndef build(conn, session, folder):\n    return "BILLING"\n',
        "products.py": 'MENU_TEXT = "Products"\ndef build(conn, session, folder):\n    return "PRODUCTS"\n',
    })
    found, problems = screens.discover(name)
    assert problems == [] and sorted(found) == ["New Sale / Billing", "Products"]
    assert screens.build_screen("Products", None, None, None, package=name) == "PRODUCTS"
    assert screens.build_screen("Reports", None, None, None, package=name) is None  # no file = no plug-in


def test_broken_duplicate_and_unnamed_screen_files_are_reported_not_fatal(tmp_path, pkg_cleanup):
    name = make_package(tmp_path, "fake_screens_b", {
        "a_good.py": 'MENU_TEXT = "Products"\ndef build(c, s, f):\n    return 1\n',
        "b_dup.py": 'MENU_TEXT = "Products"\ndef build(c, s, f):\n    return 2\n',
        "c_crash.py": 'import not_a_real_module_xyz\nMENU_TEXT = "Reports"\ndef build(c, s, f):\n    return 3\n',
        "d_nameless.py": "def build(c, s, f):\n    return 4\n",
        "_private.py": 'MENU_TEXT = "Settings"\ndef build(c, s, f):\n    return 5\n',
    })
    found, problems = screens.discover(name)
    assert list(found) == ["Products"] and found["Products"].__name__.endswith("a_good")  # first file wins
    text = " | ".join(problems)
    assert "b_dup" in text and "already taken" in text
    assert "c_crash" in text and "import failed" in text
    assert "d_nameless" in text and "needs MENU_TEXT" in text
    assert "_private" not in text  # files starting with _ are ignored


def test_check_menu_flags_a_text_that_is_not_in_the_menu(tmp_path, pkg_cleanup):
    name = make_package(tmp_path, "fake_screens_c", {
        "typo.py": 'MENU_TEXT = "Billing"\ndef build(c, s, f):\n    return 1\n',
    })
    found, _ = screens.discover(name)
    bad = screens.check_menu(found, [t for t, _ in mw.MENU])
    assert len(bad) == 1 and "'Billing' is not in the main menu" in bad[0]


def test_the_real_screens_folder_has_no_problems():
    found, problems = screens.discover()
    assert problems == []
    assert screens.check_menu(found, [t for t, _ in mw.MENU]) == []


def test_main_window_uses_a_plugged_in_screen_and_isolates_a_crashing_one(tmp_path, pkg_cleanup, window_for, monkeypatch):
    name = make_package(tmp_path, "fake_screens_d", {
        "products.py": """
            from PySide6.QtWidgets import QLabel
            MENU_TEXT = "Products"
            def build(conn, session, folder):
                return QLabel("PLUGGED PRODUCTS")
        """,
        "reports.py": """
            MENU_TEXT = "Reports"
            def build(conn, session, folder):
                raise RuntimeError("secret detail")
        """,
    })
    monkeypatch.setattr(screens, "PACKAGE", name)
    conn, session, folder = window_for()
    window = mw.MainWindow(conn, session, {"name": "Shop"}, folder=folder)
    products = window.pages.widget(window.allowed.index("Products"))
    assert isinstance(products, QLabel) and products.text() == "PLUGGED PRODUCTS"
    reports = window.pages.widget(window.allowed.index("Reports"))
    assert reports.is_error is True
    assert "secret detail" not in " ".join(w.text() for w in reports.findChildren(QLabel))
    assert not window.pages.widget(window.allowed.index("Dashboard")).__dict__.get("is_error", False)
    _dispose(window)


# ================================================================ migrations

def test_new_migration_files_are_picked_up_by_number(tmp_path, pkg_cleanup):
    name = make_package(tmp_path, "fake_db", {
        "migration_50.py": 'MIGRATION_50 = "CREATE TABLE t50(id INTEGER);"\n',
        "migration_7.py": 'MIGRATION_7 = "CREATE TABLE t7(id INTEGER);"\n',
        "migration_notes.py": "X = 1\n",
    })
    sub = tmp_path / name / "deferred"
    sub.mkdir()
    (sub / "migration_99.py").write_text('MIGRATION_99 = "x"\n')
    found = schema.discover_migrations(tmp_path / name, package=name)
    assert sorted(found) == [7, 50] and found[50].startswith("CREATE TABLE t50")  # sub-folders are ignored


def test_a_migration_file_without_its_script_is_refused(tmp_path, pkg_cleanup):
    name = make_package(tmp_path, "fake_db2", {"migration_60.py": "MIGRATION_61 = 'x'\n"})
    with pytest.raises(RuntimeError, match="must define MIGRATION_60"):
        schema.discover_migrations(tmp_path / name, package=name)


def test_every_migration_file_is_registered_and_numbers_are_unique():
    files = {int(p.stem.split("_")[1]) for p in (ROOT / "db").glob("migration_*.py") if p.stem.split("_")[1].isdigit()}
    assert files <= set(schema.MIGRATIONS)
    assert set(schema.discover_migrations()) == files
    assert all(isinstance(v, str) and v.strip() for v in schema.MIGRATIONS.values())


def test_a_new_migration_number_is_applied_to_an_existing_database(tmp_path, pkg_cleanup, monkeypatch):
    from db.connection import open_db
    conn = open_db(tmp_path / "x.db", "pw")
    latest = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    monkeypatch.setitem(schema.MIGRATIONS, latest + 5, "CREATE TABLE added_later(id INTEGER PRIMARY KEY);")
    schema.migrate(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'added_later'").fetchone()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == latest + 5
    conn.close()


# ================================================================ translations

def test_english_is_shown_as_it_is():
    assert tr("Dashboard") == "Dashboard"
    assert tr("Today: {date}", date="05/11/2026") == "Today: 05/11/2026"


def test_hindi_and_marathi_translate_and_keep_placeholders():
    i18n.set_language("hi")
    assert tr("Dashboard") == "डैशबोर्ड"
    assert tr("Today: {date}", date="05/11/2026") == "आज: 05/11/2026"
    assert tr("{days} day(s) ago", days=3) == "3 दिन पहले"
    i18n.set_language("mr")
    assert tr("Dashboard") == "डॅशबोर्ड"
    assert tr("{n} item(s) sold", n=2) == "2 आयटम विकले"


def test_a_text_without_translation_falls_back_to_english():
    i18n.set_language("hi")
    assert tr("A sentence nobody translated") == "A sentence nobody translated"
    assert tr("Nothing for {who}", who="Anu") == "Nothing for Anu"


def test_a_broken_translation_never_breaks_a_screen(monkeypatch):
    monkeypatch.setitem(i18n._table("hi"), "Today: {date}", "आज: {wrong_name}")
    i18n.set_language("hi")
    assert tr("Today: {date}", date="05/11/2026") == "Today: 05/11/2026"


def test_unknown_language_is_refused_and_settings_fall_back_to_english(tmp_path):
    with pytest.raises(ValueError, match="Language must be one of"):
        i18n.set_language("fr")
    conn, _, _ = shop.setup_new_shop(tmp_path / "s", "owner", "owner-password", pin="1234")
    assert i18n.load_language_from_settings(conn) == "en"
    set_setting(conn, "ui_language", "mr")
    assert i18n.load_language_from_settings(conn) == "mr"
    set_setting(conn, "ui_language", "klingon")
    assert i18n.load_language_from_settings(conn) == "en"
    conn.close()


def test_every_text_in_the_source_is_translated_into_hindi_and_marathi():
    assert i18n.missing_translations("hi") == []
    assert i18n.missing_translations("mr") == []
    assert i18n.placeholder_mistakes("hi") == [] and i18n.placeholder_mistakes("mr") == []


def test_the_scanner_finds_tr_and_N_texts(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "demo.py").write_text('x = tr("Hello")\ny = N_("Bye {n}")\nz = tr(variable)\n')
    (tmp_path / "core").mkdir()
    assert sorted(i18n.source_strings(tmp_path)) == ["Bye {n}", "Hello"]  # tr(variable) cannot be read, so it is skipped


def test_main_window_opens_in_the_shops_language(window_for):
    conn, session, folder = window_for()
    set_setting(conn, "ui_language", "hi")
    window = mw.MainWindow(conn, session, {"name": "Shop"}, folder=folder)
    assert window.menu.item(0).text() == "डैशबोर्ड"
    assert window.allowed[0] == "Dashboard"  # internal names stay English
    assert window.out_btn.text() == "साइन आउट"
    _dispose(window)
    set_setting(conn, "ui_language", "mr")
    window = mw.MainWindow(conn, session, {"name": "Shop"}, folder=folder)
    assert window.menu.item(0).text() == "डॅशबोर्ड"
    _dispose(window)
    set_setting(conn, "ui_language", "en")
    window = mw.MainWindow(conn, session, {"name": "Shop"}, folder=folder)
    assert window.menu.item(0).text() == "Dashboard"  # and it goes back
    _dispose(window)


# ================================================================ printing interface

def test_printing_says_not_connected_instead_of_pretending():
    for result in (
        print_service.print_bill(None, None, 1),
        print_service.save_bill_pdf(None, None, 1, "x.pdf"),
        print_service.print_credit_note(None, None, 1),
        print_service.print_receipt(None, None, 1),
        print_service.print_labels(None, None, [{"variant_id": 1, "copies": 2}]),
    ):
        assert result.ok is False and "not connected" in result.message and result.path is None


def test_print_message_follows_the_language():
    i18n.set_language("hi")
    assert "प्रिंटिंग" in print_service.print_bill(None, None, 1).message
