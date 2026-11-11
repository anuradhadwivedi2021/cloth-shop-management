"""CUS-05/06: loyalty points, gift vouchers and store credit used on a bill; all or nothing."""

import pytest

from core import wallet as w
from core.customers import create_customer
from core.returns import process_return
from core.sales import create_sale
from core.settings import set_setting
from tests.test_bill_document import DAY, _cart, shop  # noqa: F401  (the same small shop; total Rs 6,230)

TOTAL = 623000


@pytest.fixture
def cust(shop):
    set_setting(shop.conn, "loyalty_enabled", "1")
    return create_customer(shop.conn, "9000000001", user_id=1, name="Sita", consent_given=True)


def _sale(s, cust_id, tenders=None, cash=None, **kw):
    pay = [{"mode": "cash", "amount_paise": cash}] if cash else []
    return create_sale(s.conn, _cart(s), pay, user_id=1, on_date=DAY, customer_id=cust_id, tenders=tenders, **kw)


def _balance_matches_ledger(conn):
    for r in conn.execute("SELECT id, balance_paise FROM gift_vouchers"):
        assert r["balance_paise"] == conn.execute(
            "SELECT COALESCE(SUM(amount_paise),0) FROM voucher_ledger WHERE voucher_id=?", (r["id"],)).fetchone()[0]


# ---------------------------------------------------------------- earning

def test_points_are_earned_on_the_money_paid_and_only_when_switched_on(shop, cust):
    _sale(shop, cust, cash=TOTAL)
    assert w.points_balance(shop.conn, cust) == 62                       # 1 point per Rs 100 on Rs 6,230
    set_setting(shop.conn, "loyalty_enabled", "0")
    _sale(shop, cust, cash=TOTAL)
    assert w.points_balance(shop.conn, cust) == 62
    _sale(shop, None, cash=TOTAL)                                         # no customer, no points
    assert shop.conn.execute("SELECT COUNT(*) FROM loyalty_ledger").fetchone()[0] == 1


def test_the_earning_rate_is_a_setting(shop, cust):
    set_setting(shop.conn, "loyalty_points_per_100", "2")
    _sale(shop, cust, cash=TOTAL)
    assert w.points_balance(shop.conn, cust) == 124


# ---------------------------------------------------------------- using points

def test_points_pay_part_of_a_bill_and_earn_nothing_themselves(shop, cust):
    _sale(shop, cust, cash=TOTAL)                                         # 62 points
    sid = _sale(shop, cust, tenders=[{"kind": "loyalty", "points": 60}], cash=TOTAL - 6000)
    modes = {r["mode"]: r["amount_paise"] for r in shop.conn.execute("SELECT mode, amount_paise FROM payments WHERE document_id=? AND document_type='sale'", (sid,))}
    assert modes == {"cash": TOTAL - 6000, "loyalty": 6000}
    assert w.points_balance(shop.conn, cust) == 62 - 60 + 61              # earned on Rs 6,170 paid in money


def test_points_rules(shop, cust):
    _sale(shop, cust, cash=TOTAL)
    for tender, text in (({"kind": "loyalty", "points": 10}, "At least 50"),
                         ({"kind": "loyalty", "points": 500}, "does not have that many"),
                         ({"kind": "loyalty", "points": 0}, "more than zero")):
        with pytest.raises(ValueError, match=text):
            _sale(shop, cust, tenders=[tender], cash=TOTAL)
    set_setting(shop.conn, "loyalty_enabled", "0")
    with pytest.raises(ValueError, match="switched off"):
        _sale(shop, cust, tenders=[{"kind": "loyalty", "points": 60}], cash=TOTAL)
    with pytest.raises(ValueError, match="Customer"):
        _sale(shop, None, tenders=[{"kind": "loyalty", "points": 60}], cash=TOTAL)


def test_a_failed_bill_takes_nothing_from_any_balance(shop, cust):
    _sale(shop, cust, cash=TOTAL)
    before = w.points_balance(shop.conn, cust)
    with pytest.raises(ValueError):                                       # short of money and credit sales are off
        _sale(shop, cust, tenders=[{"kind": "loyalty", "points": 60}], cash=100)
    assert w.points_balance(shop.conn, cust) == before
    assert shop.conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1


# ---------------------------------------------------------------- store credit

def test_store_credit_pays_and_cannot_be_overspent(shop, cust):
    w.adjust_store_credit(shop.conn, 1, cust, 200000, "gift from the owner")
    sid = _sale(shop, cust, tenders=[{"kind": "store_credit", "amount_paise": 150000}], cash=TOTAL - 150000)
    assert w.store_credit_balance(shop.conn, cust) == 50000
    assert shop.conn.execute("SELECT mode FROM payments WHERE document_id=? AND reference IS NULL AND mode='store_credit'", (sid,)).fetchone()
    with pytest.raises(ValueError, match="that much store credit"):
        _sale(shop, cust, tenders=[{"kind": "store_credit", "amount_paise": 60000}], cash=TOTAL)
    with pytest.raises(ValueError, match="together are more than the bill"):
        w.adjust_store_credit(shop.conn, 1, cust, 10**7, "big")
        _sale(shop, cust, tenders=[{"kind": "store_credit", "amount_paise": TOTAL + 1}], cash=1)


# ---------------------------------------------------------------- vouchers

def test_a_voucher_is_sold_used_in_parts_and_runs_out(shop, cust):
    v = w.issue_voucher(shop.conn, 1, 300000, today=None)
    assert v["code"].startswith("GV-") and w.get_voucher(shop.conn, v["code"].lower())["balance_paise"] == 300000
    cash = shop.conn.execute("SELECT mode, amount_paise, document_type FROM payments WHERE reference = ?", (f"voucher {v['code']}",)).fetchone()
    assert (cash["mode"], cash["amount_paise"], cash["document_type"]) == ("cash", 300000, "other")
    _sale(shop, None, tenders=[{"kind": "voucher", "code": v["code"], "amount_paise": 100000}], cash=TOTAL - 100000)
    assert w.get_voucher(shop.conn, v["code"])["balance_paise"] == 200000
    _sale(shop, None, tenders=[{"kind": "voucher", "code": v["code"]}], cash=TOTAL - 200000)     # takes what is left
    final = w.get_voucher(shop.conn, v["code"])
    assert final["balance_paise"] == 0 and final["status"] == "fully_redeemed" and not final["usable"]
    with pytest.raises(ValueError, match="no balance|FULLY|has no"):
        _sale(shop, None, tenders=[{"kind": "voucher", "code": v["code"]}], cash=TOTAL)
    _balance_matches_ledger(shop.conn)


def test_voucher_checks(shop, cust):
    other = create_customer(shop.conn, "9000000002", user_id=1, name="Gita", consent_given=True)
    mine = w.issue_voucher(shop.conn, 1, 50000, customer_id=cust)
    with pytest.raises(ValueError, match="another customer"):
        _sale(shop, other, tenders=[{"kind": "voucher", "code": mine["code"]}], cash=TOTAL)
    with pytest.raises(ValueError, match="not found"):
        _sale(shop, None, tenders=[{"kind": "voucher", "code": "NOPE"}], cash=TOTAL)
    with pytest.raises(ValueError, match="does not have that much"):
        _sale(shop, cust, tenders=[{"kind": "voucher", "code": mine["code"], "amount_paise": 60000}], cash=TOTAL)
    with pytest.raises(ValueError, match="once on a bill"):
        _sale(shop, cust, tenders=[{"kind": "voucher", "code": mine["code"]}, {"kind": "voucher", "code": mine["code"]}], cash=TOTAL)
    w.cancel_voucher(shop.conn, 1, mine["code"], reason="lost")
    with pytest.raises(ValueError, match="cancelled"):
        _sale(shop, cust, tenders=[{"kind": "voucher", "code": mine["code"]}], cash=TOTAL)
    with pytest.raises(ValueError, match="already cancelled"):
        w.cancel_voucher(shop.conn, 1, mine["code"], reason="again")


def test_an_expired_voucher_cannot_be_used_and_a_past_expiry_cannot_be_issued(shop, cust):
    with pytest.raises(ValueError, match="past"):
        w.issue_voucher(shop.conn, 1, 1000, expires_on="2020-01-01")
    from datetime import date
    v = w.issue_voucher(shop.conn, 1, 50000, expires_on="2026-11-05", today=date(2026, 11, 1))
    with pytest.raises(ValueError, match="expired"):
        create_sale(shop.conn, _cart(shop), [{"mode": "cash", "amount_paise": TOTAL}], user_id=1, on_date="2026-11-09",
                        tenders=[{"kind": "voucher", "code": v["code"]}])


def test_voucher_issue_rules_and_fixed_fields(shop):
    for bad in (0, -5, True, 10**12):
        with pytest.raises(ValueError):
            w.issue_voucher(shop.conn, 1, bad)
    with pytest.raises(ValueError, match="how the voucher was paid"):
        w.issue_voucher(shop.conn, 1, 1000, pay_mode="voucher")
    a = w.issue_voucher(shop.conn, 1, 1000, code="diwali 2026")
    assert a["code"] == "DIWALI2026"
    with pytest.raises(ValueError, match="already used"):
        w.issue_voucher(shop.conn, 1, 1000, code="DIWALI2026")
    import sqlcipher3
    with pytest.raises(sqlcipher3.dbapi2.DatabaseError):
        shop.conn.execute("UPDATE gift_vouchers SET initial_amount_paise = 5 WHERE code = 'DIWALI2026'")
    with pytest.raises(sqlcipher3.dbapi2.DatabaseError):
        shop.conn.execute("DELETE FROM voucher_ledger")


# ---------------------------------------------------------------- returns and corrections

def test_points_earned_on_returned_goods_are_taken_back_once(shop, cust):
    sid = _sale(shop, cust, cash=TOTAL)                                    # 62 points
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id=? AND variant_id=?", (sid, shop.l_black)).fetchone()["id"]
    process_return(shop.conn, sid, [{"sale_line_id": line, "qty": 1, "reason": "wrong_size"}], user_id=1, on_date=DAY)
    back = w.points_balance(shop.conn, cust)
    assert 62 > back >= 62 - 62 * 413000 // TOTAL - 1                      # about 66% of the points are taken back
    assert shop.conn.execute("SELECT tx_type FROM loyalty_ledger ORDER BY id DESC").fetchone()[0] == "RETURN_REVERSAL"


def test_corrections_need_a_note_and_cannot_go_below_zero(shop, cust):
    w.adjust_points(shop.conn, 1, cust, 100, "welcome gift")
    assert w.balances(shop.conn, cust)["points"] == 100 and w.balances(shop.conn, cust)["points_value_paise"] == 10000
    with pytest.raises(ValueError, match="note"):
        w.adjust_points(shop.conn, 1, cust, 5, " ")
    with pytest.raises(ValueError, match="that many"):
        w.adjust_points(shop.conn, 1, cust, -101, "x")
    w.adjust_points(shop.conn, 1, cust, -40, "expired")
    assert w.points_balance(shop.conn, cust) == 60
    assert [r["points"] for r in w.history(shop.conn, 1, cust)["points_log"]] == [-40, 100]


# ---------------------------------------------------------------- screens

def _qt():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_the_payment_dialog_offers_points_credit_and_voucher_and_reports_them(shop, cust):
    _qt()
    from ui.billing_counter_view import PaymentDialog

    w.adjust_store_credit(shop.conn, 1, cust, 100000, "gift")
    w.adjust_points(shop.conn, 1, cust, 100, "gift")
    v = w.issue_voucher(shop.conn, 1, 50000)
    info = dict(w.balances(shop.conn, cust), point_value_paise=100)
    seen = []
    dlg = PaymentDialog(TOTAL, lambda p: seen.append(p) or (True, ""), wallet=info,
                        voucher_balance=lambda c: w.get_voucher(shop.conn, c)["balance_paise"])
    dlg.pts_edit.setText("60")
    dlg.credit_edit.setText("500")
    dlg.voucher_edit.setText(v["code"])
    assert dlg.tenders() == [{"kind": "loyalty", "points": 60}, {"kind": "store_credit", "amount_paise": 50000},
                             {"kind": "voucher", "code": v["code"]}]
    assert dlg._wallet_paise() == 6000 + 50000 + 50000
    assert dlg.rows[0][1].text() == "5170.00"                  # the money still to collect is offered
    dlg.pts_edit.setText("x")
    with pytest.raises(ValueError):
        dlg.tenders()
    dlg.submit()
    assert seen == [] and dlg.error.text()                       # nothing is saved with a typo in the points box


def test_the_vouchers_screen_sells_lists_and_cancels(shop, monkeypatch):
    _qt()
    from PySide6.QtWidgets import QMessageBox

    from ui.screens import vouchers_screen

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    s = vouchers_screen.VouchersScreen(shop.conn, SimpleNamespace(user_id=1))
    s.amount.setText("750")
    s.issue()
    assert s.tbl.rowCount() == 1 and "750.00" in s.tbl.item(0, 1).text() and "GV-" in s.lbl_done.text()
    s.amount.setText("abc")
    s.issue()
    assert s.tbl.rowCount() == 1
    s.tbl.selectRow(0)
    s.cancel()
    assert "Cancelled" in s.tbl.item(0, 4).text()


from types import SimpleNamespace  # noqa: E402
