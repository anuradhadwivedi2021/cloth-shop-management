"""USR-05 financial-year lock, and the Tally XML export (vouchers balance and escape properly)."""

import xml.etree.ElementTree as ET
from datetime import date

import pytest

from core import expenses, fy_lock, tally_export as tx
from core.customers import create_customer
from core.purchases import confirm_purchase, create_draft_purchase, create_supplier
from core.returns import process_return
from core.sales import create_sale
from tests.test_bill_document import DAY, _cart, shop  # noqa: F401

OLD_DAY = date(2025, 6, 10)          # financial year 25-26 (ended); DAY is in the running year of the test shop


def _sale(s, on=DAY, **kw):
    s.conn.execute("UPDATE tax_rules SET valid_from = '2000-01-01'")           # the old year needs a GST rate too
    s.conn.commit()
    return create_sale(s.conn, _cart(s), [{"mode": "cash", "amount_paise": 623000}], user_id=1, on_date=on, **kw)


# ---------------------------------------------------------------- the lock

def test_lock_blocks_new_entries_in_that_year_only(shop):
    old = _sale(shop, OLD_DAY)
    assert old
    fy_lock.lock_year(shop.conn, 1, "25-26", today=date(2026, 11, 10))
    assert fy_lock.is_locked(shop.conn, "2025-06-10") and not fy_lock.is_locked(shop.conn, "2026-06-10")
    with pytest.raises(ValueError, match="25-26 is locked"):
        _sale(shop, OLD_DAY)
    with pytest.raises(ValueError, match="25-26 is locked"):
        expenses.add_expense(shop.conn, "2025-07-01", "Rent", "July rent", 10000, user_id=1)
    assert _sale(shop, DAY)                                                      # the running year is open
    expenses.add_expense(shop.conn, "2026-07-01", "Rent", "July rent", 10000, user_id=1)
    assert shop.conn.execute("SELECT COUNT(*) FROM sales WHERE fin_year = '25-26'").fetchone()[0] == 1


def test_a_return_made_today_on_an_old_bill_still_works(shop):
    sid = _sale(shop, OLD_DAY)
    fy_lock.lock_year(shop.conn, 1, "25-26", today=date(2026, 11, 10))
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id=?", (sid,)).fetchone()["id"]
    process_return(shop.conn, sid, [{"sale_line_id": line, "qty": 1, "reason": "wrong_size"}], user_id=1, on_date=DAY)


def test_purchases_cannot_be_confirmed_into_a_locked_year(shop):
    sup = create_supplier(shop.conn, "Ravi Textiles")
    variants = [shop.conn.execute("SELECT id FROM variants LIMIT 1").fetchone()["id"]]
    lines = [{"variant_id": v, "qty": 5, "unit_cost_paise": 50000, "tax_rate_percent": 5} for v in variants]
    pid = create_draft_purchase(shop.conn, sup, "OLD-1", "2025-08-01", lines)
    fy_lock.lock_year(shop.conn, 1, "25-26", today=date(2026, 11, 10))
    with pytest.raises(ValueError, match="locked"):
        confirm_purchase(shop.conn, pid, user_id=1)


def test_database_trigger_is_the_backstop(shop):
    import sqlcipher3
    fy_lock.lock_year(shop.conn, 1, "25-26", today=date(2026, 11, 10))
    with pytest.raises(sqlcipher3.dbapi2.DatabaseError, match="locked"):
        shop.conn.execute("INSERT INTO expenses(expense_date, category, description, amount_paise, payment_mode, user_id) "
                          "VALUES ('2026-03-31','Rent','x',100,'cash',1)")
    shop.conn.execute("INSERT INTO expenses(expense_date, category, description, amount_paise, payment_mode, user_id) "
                      "VALUES ('2026-04-01','Rent','x',100,'cash',1)")             # 1 April starts the next year


def test_lock_rules_and_unlock(shop):
    for bad in ("2025", "25-27", "", "abc"):
        with pytest.raises(ValueError, match="looks like"):
            fy_lock.lock_year(shop.conn, 1, bad)
    with pytest.raises(ValueError, match="has ended"):
        fy_lock.lock_year(shop.conn, 1, "26-27", today=date(2026, 11, 10))
    fy_lock.lock_year(shop.conn, 1, "25-26", today=date(2026, 11, 10))
    with pytest.raises(ValueError, match="already locked"):
        fy_lock.lock_year(shop.conn, 1, "25-26", today=date(2026, 11, 10))
    assert [y["fin_year"] for y in fy_lock.locked_years(shop.conn)] == ["25-26"]
    with pytest.raises(ValueError, match="reason"):
        fy_lock.unlock_year(shop.conn, 1, "25-26", " ")
    fy_lock.unlock_year(shop.conn, 1, "25-26", "CA asked for a correction")
    assert _sale(shop, OLD_DAY)
    with pytest.raises(ValueError, match="not locked"):
        fy_lock.unlock_year(shop.conn, 1, "25-26", "again")


# ---------------------------------------------------------------- Tally

def _vouchers(xml):
    root = ET.fromstring(xml)
    out = []
    for v in root.iter("VOUCHER"):
        entries = [(e.findtext("LEDGERNAME"), float(e.findtext("AMOUNT")), e.findtext("ISDEEMEDPOSITIVE"))
                   for e in v.iter("ALLLEDGERENTRIES.LIST")]
        out.append((v.get("VCHTYPE"), v.findtext("VOUCHERNUMBER"), v.findtext("DATE"), entries))
    return out


def test_sales_and_credit_note_vouchers_balance_and_use_tally_signs(shop):
    cust = create_customer(shop.conn, "9000000001", user_id=1, name="Sita & Sons <Pune>", consent_given=True)
    sid = _sale(shop, customer_id=cust)
    line = shop.conn.execute("SELECT id FROM sale_lines WHERE sale_id=?", (sid,)).fetchone()["id"]
    process_return(shop.conn, sid, [{"sale_line_id": line, "qty": 1, "reason": "wrong_size"}], user_id=1, on_date=DAY)
    xml, counts = tx.build_tally_xml(shop.conn, 1, "2000-01-01", "2100-01-01")
    assert counts == {"sales": 1, "credit_notes": 1, "purchases": 0}
    vouchers = _vouchers(xml)                                                  # parses: names are escaped
    assert {v[0] for v in vouchers} == {"Sales", "Credit Note"}
    for kind, number, day, entries in vouchers:
        assert len(day) == 8 and sum(a for _n, a, _s in entries) == pytest.approx(0, abs=0.001), number
        for name, amount, deemed in entries:
            assert (amount < 0) == (deemed == "Yes")                           # debits are negative, as Tally wants
    sale = next(v for v in vouchers if v[0] == "Sales")
    names = [e[0] for e in sale[3]]
    assert "Cash" in names and "Sales" in names and {"Output CGST", "Output SGST"} <= set(names)
    assert "Sita &amp; Sons &lt;Pune&gt;" in xml


def test_cancelled_bills_and_other_dates_are_left_out_and_ledger_names_can_change(shop):
    _sale(shop)
    xml, counts = tx.build_tally_xml(shop.conn, 1, "2000-01-01", "2000-12-31")
    assert counts["sales"] == 0
    xml, _ = tx.build_tally_xml(shop.conn, 1, "2000-01-01", "2100-01-01", ledgers={"sales": "Cloth Sales", "cash": "Cash in Hand"})
    assert "Cloth Sales" in xml and "Cash in Hand" in xml
    with pytest.raises(ValueError, match="From date"):
        tx.build_tally_xml(shop.conn, 1, "2026-02-01", "2026-01-01")


def test_a_purchase_voucher_credits_the_supplier(shop):
    sup = create_supplier(shop.conn, "Ravi Textiles")
    variant = shop.conn.execute("SELECT id FROM variants LIMIT 1").fetchone()["id"]
    pid = create_draft_purchase(shop.conn, sup, "S-77", "2026-11-01",
                                [{"variant_id": variant, "qty": 10, "unit_cost_paise": 50000, "tax_rate_percent": 5}])
    confirm_purchase(shop.conn, pid, user_id=1)
    xml, counts = tx.build_tally_xml(shop.conn, 1, "2026-11-01", "2026-11-30")
    assert counts["purchases"] == 1
    kind, number, day, entries = _vouchers(xml)[-1]
    assert kind == "Purchase" and day == "20261101"
    by = {n: a for n, a, _s in entries}
    assert by["Ravi Textiles"] > 0 and by["Purchases"] == pytest.approx(-5000.0) and sum(by.values()) == pytest.approx(0, abs=0.001)


def test_export_writes_the_file_and_logs_it(shop, tmp_path):
    _sale(shop)
    out = tx.export_tally_xml(shop.conn, 1, "2000-01-01", "2100-01-01", tmp_path / "tally.xml")
    assert out["sales"] == 1 and ET.parse(out["path"]).getroot().tag == "ENVELOPE"
    assert shop.conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'tally_exported'").fetchone()[0] == 1
    with pytest.raises(ValueError, match="could not be saved"):
        tx.export_tally_xml(shop.conn, 1, "2000-01-01", "2100-01-01", tmp_path / "nope" / "t.xml")


def test_the_year_close_screen_locks_unlocks_and_exports(shop, tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from ui.screens import year_close_screen

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    s = year_close_screen.YearCloseScreen(shop.conn, SimpleNamespace(user_id=1))
    s.year.setText("20-21")
    s.lock()
    assert s.tbl.rowCount() == 1 and "20-21" in s.lbl_done.text()
    s.tbl.selectRow(0)
    s.unlock()
    assert s.tbl.rowCount() == 0
    s.year.setText("bad")
    s.lock()
    assert s.tbl.rowCount() == 0
    _sale(shop)
    target = tmp_path / "t.xml"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    s.date_from.setText("2000-01-01")
    s.date_to.setText("2100-01-01")
    s.export()
    assert target.exists() and "1 sale" in s.lbl_done.text()
