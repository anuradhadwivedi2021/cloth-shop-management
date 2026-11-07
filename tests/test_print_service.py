"""Printing a bill: what the screens call. The Qt printer itself is replaced here (it needs a real printer
or PySide6); what is checked is the bill that is sent, the logging, the messages and that a failure is harmless."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from core.products import create_product, generate_variants
from core.sales import create_sale
from core.settings import set_setting
from core.shop_settings import update_shop_profile
from core.stock import add_movement, get_stock
from core.tax import ensure_default_tax_rules
from db.connection import open_db, transaction
from tests.test_shop_settings import MH
from ui import i18n, print_service


class _Session:
    user_id = 1

    def __init__(self, locked=False):
        self.locked = locked

    def touch(self):
        if self.locked:
            raise RuntimeError("locked")


@pytest.fixture
def shop(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    conn.execute("INSERT INTO users(username, password_hash, role) VALUES ('owner', 'x', 'owner')")
    ensure_default_tax_rules(conn)
    update_shop_profile(conn, 1, name="Anu Cloth House", gstin=MH, now=datetime(2026, 1, 1))
    pid = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    (v,) = generate_variants(conn, pid, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, v, 10, "opening stock")
    sid = create_sale(conn, [{"variant_id": v, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                      user_id=1, on_date="2026-11-01")
    yield SimpleNamespace(conn=conn, sid=sid, v=v, session=_Session())
    conn.close()
    i18n.set_language("en")


@pytest.fixture
def sent(monkeypatch):
    """Replace the two Qt steps; remember what would have been printed or written."""
    log = SimpleNamespace(printed=[], pdfs=[], printer="Test Printer", fail=None)

    def fake_print(bill, html):
        if log.fail:
            raise log.fail
        log.printed.append((bill, html))
        return log.printer

    def fake_pdf(bill, html, path):
        if log.fail:
            raise log.fail
        log.pdfs.append((bill, html, path))
        with open(path, "wb") as fh:
            fh.write(b"%PDF-fake")

    monkeypatch.setattr(print_service, "_print_html", fake_print)
    monkeypatch.setattr(print_service, "_write_pdf", fake_pdf)
    return log


def _audit(conn):
    return conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'sale_reprint'").fetchone()[0]


def test_print_bill_sends_the_saved_bill_to_the_printer(shop, sent):
    result = print_service.print_bill(shop.conn, shop.session, shop.sid)
    assert result.ok and "Test Printer" in result.message and result.path is None
    bill, html = sent.printed[0]
    assert bill["sale_id"] == shop.sid and "Anu Cloth House" in html and "Duplicate" not in html
    assert _audit(shop.conn) == 0  # the first print is not a reprint


def test_reprint_is_marked_duplicate_logged_and_creates_nothing(shop, sent):
    sales_before = shop.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    stock_before = get_stock(shop.conn, shop.v)
    result = print_service.print_bill(shop.conn, shop.session, shop.sid, duplicate=True)
    assert result.ok and "Duplicate copy" in sent.printed[0][1]
    assert _audit(shop.conn) == 1
    assert shop.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == sales_before
    assert get_stock(shop.conn, shop.v) == stock_before


def test_a_locked_screen_cannot_reprint(shop, sent):
    result = print_service.print_bill(shop.conn, _Session(locked=True), shop.sid, duplicate=True)
    assert not result.ok and sent.printed == [] and _audit(shop.conn) == 0


def test_no_printer_is_a_clear_message_not_a_crash(shop, sent):
    sent.fail = RuntimeError("no printer")
    result = print_service.print_bill(shop.conn, shop.session, shop.sid)
    assert not result.ok and "No printer was found" in result.message and "saved" in result.message


def test_a_printer_error_never_escapes(shop, sent):
    sent.fail = OSError("paper jam")
    result = print_service.print_bill(shop.conn, shop.session, shop.sid)
    assert not result.ok and result.message == "The printer reported a problem."
    assert "paper jam" not in result.message  # technical detail goes to the log only


def test_unknown_bill_and_broken_session_do_not_crash(shop, sent):
    assert print_service.print_bill(shop.conn, shop.session, 9999).message == "The bill could not be found."
    assert not print_service.print_bill(shop.conn, None, shop.sid).ok
    assert sent.printed == []


def test_save_pdf_into_a_folder_names_the_file_after_the_bill(shop, sent, tmp_path):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    result = print_service.save_bill_pdf(shop.conn, shop.session, shop.sid, str(folder))
    assert result.ok and result.path and result.path.startswith(str(folder))
    assert result.path.endswith("-duplicate.pdf") and (folder / result.path.split("/")[-1]).exists()
    assert "Duplicate copy" in sent.pdfs[0][1] and _audit(shop.conn) == 1


def test_save_pdf_adds_the_extension_and_makes_the_folder(shop, sent, tmp_path):
    target = tmp_path / "new folder" / "my bill"
    result = print_service.save_bill_pdf(shop.conn, shop.session, shop.sid, str(target))
    assert result.ok and result.path.endswith("my bill.pdf") and (tmp_path / "new folder" / "my bill.pdf").exists()


def test_save_pdf_failure_is_reported_and_an_empty_file_is_not_success(shop, sent, tmp_path, monkeypatch):
    sent.fail = OSError("disk full")
    result = print_service.save_bill_pdf(shop.conn, shop.session, shop.sid, str(tmp_path / "a.pdf"))
    assert not result.ok and result.message == "The PDF could not be saved." and result.path is None
    sent.fail = None
    monkeypatch.setattr(print_service, "_write_pdf", lambda bill, html, path: open(path, "wb").close())
    assert not print_service.save_bill_pdf(shop.conn, shop.session, shop.sid, str(tmp_path / "b.pdf")).ok


def test_messages_follow_the_screen_language(shop, sent):
    i18n.set_language("hi")
    assert "प्रिंटर" in print_service.print_bill(shop.conn, shop.session, shop.sid).message
    assert print_service.print_bill(shop.conn, shop.session, 9999).message == "बिल नहीं मिला।"


def test_the_bill_uses_the_paper_chosen_in_settings(shop, sent):
    set_setting(shop.conn, "bill_paper", "a4")
    print_service.print_bill(shop.conn, shop.session, shop.sid)
    assert sent.printed[0][0]["paper"]["name"] == "a4" and "<th" in sent.printed[0][1]