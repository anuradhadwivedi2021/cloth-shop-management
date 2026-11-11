"""Tally XML export: sales, credit notes and purchases of a date range, as vouchers Tally Prime/ERP 9 can import.

  export_tally_xml(conn, user_id, date_from, date_to, path, ledgers=None)  -> {'path', 'sales', 'credit_notes', 'purchases'}

Each voucher balances by construction (debits = credits). Ledger names are the ones the accountant uses in Tally;
the defaults below can be changed per call. The ledgers must exist in Tally (Import creates vouchers, not ledgers).
Cancelled bills are left out. Needs the permission to see reports and costs.
"""

from xml.sax.saxutils import escape

from core.audit import log_event
from core.permissions import require_permission
from core.report_common import check_range, require_view
from core.shop_settings import get_shop_profile
from db.connection import transaction

LEDGERS = {"cash": "Cash", "bank": "Bank", "sales": "Sales", "sales_return": "Sales Returns", "purchases": "Purchases",
           "cgst_out": "Output CGST", "sgst_out": "Output SGST", "igst_out": "Output IGST",
           "cgst_in": "Input CGST", "sgst_in": "Input SGST", "igst_in": "Input IGST",
           "round_off": "Round Off", "debtors": "Sundry Debtors", "advances": "Customer Advances"}
_NON_CASH_BANK = ("upi", "card", "bank_transfer", "cheque", "other")


def _amt(paise) -> str:
    return f"{paise / 100:.2f}"


def _tally_date(text) -> str:
    return text[:10].replace("-", "")


def _ledger_entry(name, paise, *, debit) -> str:
    """Tally: a debit is ISDEEMEDPOSITIVE Yes with a negative amount, a credit is No with a positive one."""
    return ("<ALLLEDGERENTRIES.LIST><LEDGERNAME>%s</LEDGERNAME><ISDEEMEDPOSITIVE>%s</ISDEEMEDPOSITIVE>"
            "<AMOUNT>%s</AMOUNT></ALLLEDGERENTRIES.LIST>" % (escape(name), "Yes" if debit else "No",
                                                             ("-" if debit else "") + _amt(abs(paise))))


def _voucher(kind, date_text, number, party, narration, entries) -> str:
    """entries: (ledger, paise, is_debit); zero entries are skipped."""
    body = "".join(_ledger_entry(n, p, debit=d) for n, p, d in entries if p)
    debit = sum(p for _n, p, d in entries if d)
    credit = sum(p for _n, p, d in entries if not d)
    assert debit == credit, f"voucher {number} does not balance"
    return ('<TALLYMESSAGE xmlns:UDF="TallyUDF"><VOUCHER VCHTYPE="%s" ACTION="Create" OBJVIEW="Accounting Voucher View">'
            "<DATE>%s</DATE><VOUCHERTYPENAME>%s</VOUCHERTYPENAME><VOUCHERNUMBER>%s</VOUCHERNUMBER>"
            "<PARTYLEDGERNAME>%s</PARTYLEDGERNAME><NARRATION>%s</NARRATION>%s</VOUCHER></TALLYMESSAGE>"
            % (kind, date_text, kind, escape(number), escape(party), escape(narration), body))


def _signed(round_off, plus_is_credit, entries, name):
    """Round-off goes to the side that balances it."""
    if round_off:
        entries.append((name, abs(round_off), (round_off > 0) != plus_is_credit))


def _tax_split(tax_paise, intra, led, side):
    if intra:
        half = tax_paise // 2
        return [(led[f"cgst_{side}"], half), (led[f"sgst_{side}"], tax_paise - half)]
    return [(led[f"igst_{side}"], tax_paise)]


def _sales(conn, start, end, led):
    out = []
    for s in conn.execute("SELECT s.*, c.name AS cust FROM sales s LEFT JOIN customers c ON c.id = s.customer_id "
                          "WHERE s.status = 'completed' AND substr(s.created_at,1,10) BETWEEN ? AND ? ORDER BY s.id", (start, end)):
        entries, paid = [], 0
        for p in conn.execute("SELECT mode, SUM(amount_paise) AS amt FROM payments WHERE document_type = 'sale' AND document_id = ? "
                              "GROUP BY mode", (s["id"],)):
            name = led["cash"] if p["mode"] == "cash" else led["bank"] if p["mode"] in _NON_CASH_BANK else led["advances"]
            entries.append((name, p["amt"], True))
            paid += p["amt"]
        rest = s["total_paise"] - paid
        party = s["cust"] or led["debtors"]
        if rest > 0:
            entries.append((party if s["cust"] else led["debtors"], rest, True))
        elif rest < 0:                                    # change given back: it is not part of the sale
            entries = _trim(entries, -rest)
        entries.append((led["sales"], s["taxable_paise"], False))
        entries += [(n, p, False) for n, p in _tax_split(s["tax_paise"], s["intra_state"], led, "out")]
        _signed(s["round_off_paise"], True, entries, led["round_off"])
        out.append(_voucher("Sales", _tally_date(s["created_at"]), s["invoice_no"], party, "Sales bill " + s["invoice_no"], entries))
    return out


def _trim(entries, extra):
    """Take `extra` paise (cash change) off the cash entry."""
    result = []
    for name, paise, debit in entries:
        if extra and debit and name == LEDGERS["cash"]:
            cut = min(extra, paise)
            paise, extra = paise - cut, extra - cut
        result.append((name, paise, debit))
    return result


def _credit_notes(conn, start, end, led):
    out = []
    for r in conn.execute("SELECT r.*, c.name AS cust FROM sale_returns r LEFT JOIN customers c ON c.id = r.customer_id "
                          "WHERE substr(r.created_at,1,10) BETWEEN ? AND ? ORDER BY r.id", (start, end)):
        intra = conn.execute("SELECT intra_state FROM sales WHERE id = ?", (r["sale_id"],)).fetchone()["intra_state"]
        entries = [(led["sales_return"], r["taxable_paise"], True)]
        entries += [(n, p, True) for n, p in _tax_split(r["tax_paise"], intra, led, "out")]
        _signed(r["round_off_paise"], False, entries, led["round_off"])
        total = sum(p for _n, p, d in entries if d) - sum(p for _n, p, d in entries if not d)
        refund = min(r["refund_paise"], total)
        credit_note = min(r["store_credit_paise"], total - refund)
        entries.append((led["cash"], refund, False))
        entries.append((led["advances"], credit_note, False))
        entries.append((led["debtors"], total - refund - credit_note, False))
        out.append(_voucher("Credit Note", _tally_date(r["created_at"]), r["credit_note_no"], r["cust"] or led["debtors"],
                            "Sales return " + r["credit_note_no"], entries))
    return out


def _purchases(conn, start, end, led, shop_state):
    out = []
    for p in conn.execute("SELECT pu.*, su.name AS supplier, su.gstin AS gstin FROM purchases pu JOIN suppliers su ON su.id = pu.supplier_id "
                          "WHERE pu.status = 'confirmed' AND pu.invoice_date BETWEEN ? AND ? ORDER BY pu.id", (start, end)):
        intra = not (p["gstin"] and shop_state and str(p["gstin"])[:2] != str(shop_state))
        goods = p["total_paise"] - p["tax_paise"] - p["round_off_paise"]
        entries = [(led["purchases"], goods, True)]
        entries += [(n, v, True) for n, v in _tax_split(p["tax_paise"], intra, led, "in")]
        _signed(p["round_off_paise"], False, entries, led["round_off"])
        entries.append((p["supplier"], sum(v for _n, v, d in entries if d) - sum(v for _n, v, d in entries if not d), False))
        number = p["voucher_no"] or p["invoice_no"] or f"PUR-{p['id']}"
        out.append(_voucher("Purchase", _tally_date(p["invoice_date"]), number, p["supplier"],
                            f"Supplier invoice {p['invoice_no'] or ''}".strip(), entries))
    return out


def build_tally_xml(conn, user_id, date_from, date_to, ledgers=None) -> tuple:
    require_view(conn, user_id)
    require_permission(conn, user_id, "view_cost", "export purchases")
    start, end = check_range(date_from, date_to)
    led = dict(LEDGERS, **(ledgers or {}))
    profile = get_shop_profile(conn)
    sales, notes = _sales(conn, start, end, led), _credit_notes(conn, start, end, led)
    purchases = _purchases(conn, start, end, led, profile.get("state_code"))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY>'
           "<IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME><STATICVARIABLES><SVCURRENTCOMPANY>%s</SVCURRENTCOMPANY>"
           "</STATICVARIABLES></REQUESTDESC><REQUESTDATA>%s</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>\n"
           % (escape(profile.get("name") or ""), "".join(sales + notes + purchases)))
    return xml, {"sales": len(sales), "credit_notes": len(notes), "purchases": len(purchases)}


def export_tally_xml(conn, user_id, date_from, date_to, path, ledgers=None) -> dict:
    xml, counts = build_tally_xml(conn, user_id, date_from, date_to, ledgers)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(xml)
    except OSError:
        raise ValueError("The file could not be saved. Choose another folder.") from None
    with transaction(conn):
        log_event(conn, "tally_exported", user_id=user_id, entity="export", after={"from": str(date_from), "to": str(date_to), **counts})
    return {"path": str(path), **counts}
