"""Trial and reserved stock (SRS STK-03): held pieces leave sellable stock, so billing can never sell them."""

import os
from datetime import date
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import stock_holds as holds  # noqa: E402
from core.audit import verify_chain  # noqa: E402
from core.permissions import PermissionDenied  # noqa: E402
from core.products import create_product, generate_variants  # noqa: E402
from core.reports_stock import BUCKETS  # noqa: E402
from core.sales import create_sale  # noqa: E402
from core.stock import InsufficientStockError, add_movement, get_stock  # noqa: E402
from core.tax import ensure_default_tax_rules  # noqa: E402
from db.connection import open_db, transaction  # noqa: E402
from ui.stock_count_view import StockCountView  # noqa: E402
from ui.stock_holds_panel import StockHoldsPanel  # noqa: E402

_APP = QApplication.instance() or QApplication([])
TODAY = date(2026, 11, 7)


@pytest.fixture
def s(tmp_path):
    conn = open_db(tmp_path / "shop.db", "test-password")
    for name, role in (("owner", "owner"), ("till", "cashier"), ("store", "stock"), ("acct", "accountant")):
        conn.execute("INSERT INTO users(username, password_hash, role) VALUES (?, 'x', ?)", (name, role))
    ensure_default_tax_rules(conn)
    shirt = create_product(conn, "Shirt", hsn="6205", default_price_paise=105000)
    (m_blue,) = generate_variants(conn, shirt, ["M"], ["Blue"])
    with transaction(conn):
        add_movement(conn, m_blue, 10, "opening stock")
    yield SimpleNamespace(conn=conn, owner=1, till=2, store=3, acct=4, v=m_blue)
    conn.close()


def _buckets(s):
    return {b: get_stock(s.conn, s.v, b) for b in BUCKETS}


def test_a_trial_hold_moves_pieces_out_of_sellable_into_on_trial(s):
    hold_id = holds.place_hold(s.conn, s.till, s.v, 3, "trial", 3, held_for="Mrs Rao", today=TODAY)
    assert _buckets(s) == {"sellable": 7, "damaged": 0, "returned_pending": 0, "on_trial": 3, "reserved": 0}
    (row,) = holds.list_holds(s.conn, s.till, today=TODAY)
    assert (row["id"], row["kind"], row["open_qty"], row["held_for"]) == (hold_id, "trial", 3, "Mrs Rao")
    assert row["hold_until"] == "2026-11-10" and row["overdue"] is False and row["username"] == "till"


def test_a_reserved_hold_uses_the_reserved_bucket(s):
    holds.place_hold(s.conn, s.store, s.v, 2, "reserved", 7, today=TODAY)
    assert _buckets(s)["reserved"] == 2 and _buckets(s)["on_trial"] == 0 and _buckets(s)["sellable"] == 8


def test_held_pieces_can_never_be_billed(s):
    holds.place_hold(s.conn, s.till, s.v, 3, "trial", 3, today=TODAY)
    with pytest.raises(InsufficientStockError):
        create_sale(s.conn, [{"variant_id": s.v, "qty": 8}], [{"mode": "cash", "amount_paise": 840000}],
                    user_id=s.owner, on_date="2026-11-07")
    assert _buckets(s)["sellable"] == 7 and s.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    create_sale(s.conn, [{"variant_id": s.v, "qty": 7}], [{"mode": "cash", "amount_paise": 735000}],
                user_id=s.owner, on_date="2026-11-07")           # the 7 that are really on the shelf
    assert _buckets(s)["sellable"] == 0 and _buckets(s)["on_trial"] == 3


def test_releasing_everything_puts_the_pieces_back_on_the_shelf(s):
    hold_id = holds.place_hold(s.conn, s.till, s.v, 3, "trial", 3, today=TODAY)
    assert holds.release_hold(s.conn, s.till, hold_id) == 3
    assert _buckets(s) == {"sellable": 10, "damaged": 0, "returned_pending": 0, "on_trial": 0, "reserved": 0}
    assert holds.list_holds(s.conn, s.till, today=TODAY) == []
    (closed,) = holds.list_holds(s.conn, s.till, include_closed=True, today=TODAY)
    assert closed["open_qty"] == 0 and closed["overdue"] is False
    with pytest.raises(ValueError, match="already back"):
        holds.release_hold(s.conn, s.till, hold_id)


def test_pieces_can_come_back_a_few_at_a_time(s):
    hold_id = holds.place_hold(s.conn, s.till, s.v, 5, "reserved", 7, today=TODAY)
    holds.release_hold(s.conn, s.till, hold_id, 2, note="took 3, returned 2")
    assert _buckets(s)["reserved"] == 3 and _buckets(s)["sellable"] == 7
    with pytest.raises(ValueError, match="Only 3"):
        holds.release_hold(s.conn, s.till, hold_id, 4)
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            holds.release_hold(s.conn, s.till, hold_id, bad)
    assert holds.release_hold(s.conn, s.till, hold_id, 3) == 3
    assert _buckets(s)["sellable"] == 10


def test_total_stock_never_changes_when_pieces_move_between_buckets(s):
    hold_id = holds.place_hold(s.conn, s.till, s.v, 4, "trial", 2, today=TODAY)
    assert sum(_buckets(s).values()) == 10
    holds.release_hold(s.conn, s.till, hold_id, 1)
    assert sum(_buckets(s).values()) == 10


def test_not_enough_sellable_stock_saves_nothing(s):
    with pytest.raises(InsufficientStockError):
        holds.place_hold(s.conn, s.till, s.v, 11, "trial", 3, today=TODAY)
    assert s.conn.execute("SELECT COUNT(*) FROM stock_holds").fetchone()[0] == 0
    assert _buckets(s)["sellable"] == 10 and _buckets(s)["on_trial"] == 0


@pytest.mark.parametrize("kind, qty, days, message", [
    ("loan", 1, 3, "trial or reserved"),
    ("trial", 0, 3, "greater than zero"),
    ("trial", True, 3, "greater than zero"),
    ("trial", 1, 0, "between 1 and 60"),
    ("trial", 1, 61, "between 1 and 60"),
])
def test_bad_input_is_refused_with_a_plain_message(s, kind, qty, days, message):
    with pytest.raises(ValueError, match=message):
        holds.place_hold(s.conn, s.till, s.v, qty, kind, days, today=TODAY)
    assert s.conn.execute("SELECT COUNT(*) FROM stock_holds").fetchone()[0] == 0


def test_unknown_item_or_hold_is_refused(s):
    with pytest.raises(ValueError, match="not found"):
        holds.place_hold(s.conn, s.till, 9999, 1, "trial", 3, today=TODAY)
    with pytest.raises(ValueError, match="not found"):
        holds.release_hold(s.conn, s.till, 9999)


def test_a_hold_past_its_date_is_flagged_overdue(s):
    holds.place_hold(s.conn, s.till, s.v, 1, "trial", 3, today=TODAY)            # until 10 Nov
    assert holds.list_holds(s.conn, s.till, today=date(2026, 11, 10))[0]["overdue"] is False
    assert holds.list_holds(s.conn, s.till, today=date(2026, 11, 11))[0]["overdue"] is True


def test_holds_are_listed_with_the_earliest_due_date_first(s):
    late = holds.place_hold(s.conn, s.till, s.v, 1, "reserved", 20, today=TODAY)
    soon = holds.place_hold(s.conn, s.till, s.v, 1, "trial", 2, today=TODAY)
    assert [h["id"] for h in holds.list_holds(s.conn, s.till, today=TODAY)] == [soon, late]


def test_cashier_and_stock_staff_may_hold_but_an_accountant_may_not(s):
    holds.place_hold(s.conn, s.till, s.v, 1, "trial", 3, today=TODAY)
    holds.place_hold(s.conn, s.store, s.v, 1, "reserved", 3, today=TODAY)
    for call in (lambda: holds.place_hold(s.conn, s.acct, s.v, 1, "trial", 3, today=TODAY),
                 lambda: holds.release_hold(s.conn, s.acct, 1),
                 lambda: holds.list_holds(s.conn, s.acct)):
        with pytest.raises(PermissionDenied):
            call()


def test_holds_cannot_be_edited_or_deleted(s):
    hold_id = holds.place_hold(s.conn, s.till, s.v, 2, "trial", 3, today=TODAY)
    holds.release_hold(s.conn, s.till, hold_id, 1)
    for sql in ("UPDATE stock_holds SET qty = 99", "DELETE FROM stock_holds",
                "UPDATE stock_hold_releases SET qty = 99", "DELETE FROM stock_hold_releases"):
        with pytest.raises(Exception, match="append-only"):
            s.conn.execute(sql)


def test_every_hold_and_release_is_in_the_audit_log(s):
    hold_id = holds.place_hold(s.conn, s.till, s.v, 2, "trial", 3, today=TODAY)
    holds.release_hold(s.conn, s.till, hold_id)
    actions = [r[0] for r in s.conn.execute("SELECT action FROM audit_log WHERE action LIKE 'stock_hold_%' ORDER BY id")]
    assert actions == ["stock_hold_placed", "stock_hold_released"]
    assert verify_chain(s.conn)


# ---------------------------------------------------------------- the screen

@pytest.fixture
def panel(s):
    panel = StockHoldsPanel(s.conn, SimpleNamespace(user_id=s.owner))
    panel.shown = SimpleNamespace(infos=[], errors=[])
    panel._info = lambda title, text: panel.shown.infos.append(text)
    panel._error = lambda title, text: panel.shown.errors.append(text)
    return panel


def test_the_panel_holds_and_releases_pieces(s, panel):
    assert panel.set_item(s.v) and panel.spin_qty.maximum() == 10 and panel.spin_days.value() == 3
    panel.spin_qty.setValue(4)
    panel.txt_for.setText("9876543210")
    panel.hold()
    assert panel.shown.errors == [] and "Held" in panel.shown.infos[-1]
    assert _buckets(s)["on_trial"] == 4 and panel.spin_qty.maximum() == 6      # the screen shows what is left
    assert panel.tbl_holds.rowCount() == 1 and panel.tbl_holds.item(0, 3).text() == "On trial"
    assert panel.tbl_holds.item(0, 5).text() == "9876543210" and panel.tbl_holds.item(0, 4).text() == "4"
    panel.tbl_holds.selectRow(0)
    assert panel.btn_release.isEnabled()
    panel.release()
    assert "4 piece(s)" in panel.shown.infos[-1] and panel.tbl_holds.rowCount() == 0
    assert _buckets(s)["sellable"] == 10 and panel.spin_qty.maximum() == 10


def test_reserved_defaults_to_a_longer_hold(s, panel):
    panel.cmb_kind.setCurrentIndex(panel.cmb_kind.findData("reserved"))
    assert panel.spin_days.value() == 7


def test_the_panel_asks_for_an_item_first_and_blocks_a_double_click(s, panel, monkeypatch):
    panel.hold()
    assert "Choose the item first" in panel.shown.errors[-1]
    panel.release()
    assert "Select a line first" in panel.shown.errors[-1]
    panel.set_item(s.v)
    panel._busy = True
    panel.hold()
    assert _buckets(s)["on_trial"] == 0                      # ignored while an action is running


def test_a_core_refusal_is_shown_not_raised(s, panel):
    panel.set_item(s.v)
    holds_before = _buckets(s)
    panel.shown.errors.clear()
    panel.cmb_kind.setCurrentIndex(0)
    panel.spin_qty.setRange(1, 50)
    panel.spin_qty.setValue(50)                                # more than the shop has
    panel.hold()
    assert panel.shown.errors and "Not enough stock" in panel.shown.errors[-1]
    assert _buckets(s) == holds_before and panel._busy is False


def test_the_stock_count_screen_has_the_trial_and_reserved_tab(s):
    view = StockCountView(s.conn, SimpleNamespace(user_id=s.owner))
    titles = [view.tabs.tabText(i) for i in range(view.tabs.count())]
    assert "Trial and reserved" in titles and view.holds_panel.tbl_holds.rowCount() == 0
