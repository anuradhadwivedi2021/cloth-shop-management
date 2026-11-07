"""Money reports: the cash book and the expense report (SRS Report list, group "Money"; SRS 14.3, 15, 16.4).

Rules every report here follows (same as core.reports_sales):
* Figures come from SAVED records only (cash payments, cash movements, cash days, expenses).
* Every document is counted once. Reversals are shown as their own lines and subtract from totals.
* A cash difference found at closing is SHOWN, never used to change a figure.
* Nothing here is called "profit". Expenses alone do not make a profit figure.
* Reading needs the "view_reports" permission. No customer personal details appear.

Dates are inclusive (YYYY-MM-DD).

Cash book
---------
`cash_book` returns two views of the same cash:
  * `days`: one row per business day with the cash register figures (opening, cash sales, refunds,
    supplier payments, expenses, hand-entered cash in and out, expected, counted and difference).
    These come from core.cash.get_cash_day, so the report always agrees with the register screen.
  * `entries`: every cash movement in time order with a running balance for its day, starting from
    that day's opening cash. The last balance of a day equals that day's expected cash.

Expense report
--------------
`expense_report` lists every expense of the period (oldest first) and totals them by category, month
and payment mode. A reversal is a negative line, so net totals are correct after corrections.
"""

from core.cash import _CASH_SIGN, get_cash_day  # _CASH_SIGN: one source for "what a payment does to the drawer"
from core.expenses import list_expenses
from core.report_common import check_range, require_view

CASH_BOOK_FORMULA = (
    "Expected cash = opening cash + cash sales - cash refunds to customers - cash paid to suppliers "
    "+ cash refunded by suppliers - cash expenses + cash put in by hand - cash taken out by hand. "
    "A difference at closing is shown, never adjusted."
)
EXPENSE_FORMULA = (
    "Net expenses = expenses recorded - expenses reversed. "
    "A correction is a reversal of the old entry plus a new entry."
)

_CASH_DOC_TYPES = tuple(_CASH_SIGN)

_DESCRIPTION = {
    "sale": "Cash received on bill",
    "sale_return": "Cash refunded to customer",
    "purchase": "Cash paid to supplier",
    "purchase_return": "Cash refunded by supplier",
    "expense": "Cash paid for expense",
}


# ---------- cash book ----------

def _cash_dates(conn, start, end) -> list:
    """Every business day of the period that has a cash register row, a cash payment or a hand movement."""
    marks = ",".join("?" for _ in _CASH_DOC_TYPES)
    rows = conn.execute(
        "SELECT business_date AS d FROM cash_days WHERE business_date BETWEEN ? AND ? "
        "UNION SELECT business_date FROM cash_movements WHERE business_date BETWEEN ? AND ? "
        f"UNION SELECT substr(created_at, 1, 10) FROM payments WHERE mode = 'cash' "
        f"AND document_type IN ({marks}) AND substr(created_at, 1, 10) BETWEEN ? AND ? "
        "ORDER BY 1",
        (start, end, start, end, *_CASH_DOC_TYPES, start, end),
    ).fetchall()
    return [r["d"] for r in rows]


def _cash_entries(conn, start, end) -> list:
    """Cash payments and hand movements of the period, with the document number where one exists."""
    marks = ",".join("?" for _ in _CASH_DOC_TYPES)
    entries = []
    for r in conn.execute(
        "SELECT p.id, p.created_at, p.document_type AS kind, p.document_id, p.amount_paise, p.reversal_of, p.reason, "
        "s.invoice_no AS sale_no, sr.credit_note_no AS cn_no, COALESCE(pu.voucher_no, pu.invoice_no) AS pur_no, "
        "pr.debit_note_no AS dn_no, e.category AS exp_category, e.description AS exp_description "
        "FROM payments p "
        "LEFT JOIN sales s ON p.document_type = 'sale' AND s.id = p.document_id "
        "LEFT JOIN sale_returns sr ON p.document_type = 'sale_return' AND sr.id = p.document_id "
        "LEFT JOIN purchases pu ON p.document_type = 'purchase' AND pu.id = p.document_id "
        "LEFT JOIN purchase_returns pr ON p.document_type = 'purchase_return' AND pr.id = p.document_id "
        "LEFT JOIN expenses e ON p.document_type = 'expense' AND e.id = p.document_id "
        f"WHERE p.mode = 'cash' AND p.document_type IN ({marks}) AND substr(p.created_at, 1, 10) BETWEEN ? AND ?",
        (*_CASH_DOC_TYPES, start, end),
    ):
        kind = r["kind"]
        number = {
            "sale": r["sale_no"], "sale_return": r["cn_no"], "purchase": r["pur_no"],
            "purchase_return": r["dn_no"], "expense": f"EXP-{r['document_id']}" if r["exp_category"] else None,
        }[kind] or f"{kind}-{r['document_id']}"
        description = _DESCRIPTION[kind]
        if kind == "expense" and r["exp_category"]:
            description = f"{r['exp_category']}: {r['exp_description']}"
        if r["reversal_of"] is not None:
            description = f"Reversal. {description}" + (f" ({r['reason']})" if r["reason"] else "")
        effect = _CASH_SIGN[kind] * r["amount_paise"]
        entries.append({
            "date": r["created_at"][:10], "time": r["created_at"][11:] or "00:00:00", "type": kind,
            "number": number, "description": description, "effect": effect, "_order": 0, "id": r["id"],
        })
    for r in conn.execute(
        "SELECT id, business_date, created_at, amount_paise, reason FROM cash_movements "
        "WHERE business_date BETWEEN ? AND ?", (start, end),
    ):
        entries.append({
            "date": r["business_date"], "time": r["created_at"][11:] or "00:00:00", "type": "manual",
            "number": f"CM-{r['id']}",
            "description": ("Cash put in: " if r["amount_paise"] > 0 else "Cash taken out: ") + r["reason"],
            "effect": r["amount_paise"], "_order": 1, "id": r["id"],
        })
    entries.sort(key=lambda e: (e["date"], e["time"], e["_order"], e["id"]))
    return entries


def cash_book(conn, date_from, date_to, *, user_id) -> dict:
    """The cash book for the period: day figures, every cash entry with a running balance, and totals."""
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)

    days = []
    for day in _cash_dates(conn, start, end):
        d = get_cash_day(conn, day, user_id=user_id)
        days.append({
            "business_date": d["business_date"], "status": d["status"],
            "opening_paise": d["opening_paise"], "cash_sales_paise": d["cash_sales_paise"],
            "customer_refunds_paise": d["customer_refunds_paise"],
            "supplier_payments_paise": d["supplier_payments_paise"],
            "supplier_refunds_paise": d["supplier_refunds_paise"], "expenses_paise": d["expenses_paise"],
            "manual_in_paise": d["manual_in_paise"], "manual_out_paise": d["manual_out_paise"],
            "expected_paise": d["expected_paise"],
            "counted_paise": d.get("counted_paise"), "difference_paise": d.get("difference_paise"),
            "difference_reason": d.get("difference_reason"),
            "changed_after_close": d.get("changed_after_close", False),
        })
    opening_by_day = {d["business_date"]: d["opening_paise"] for d in days}

    balances = dict(opening_by_day)
    entries = []
    for e in _cash_entries(conn, start, end):
        balances[e["date"]] = balances.get(e["date"], 0) + e["effect"]
        entries.append({
            "date": e["date"], "time": e["time"], "type": e["type"], "number": e["number"],
            "description": e["description"],
            "cash_in_paise": e["effect"] if e["effect"] > 0 else 0,
            "cash_out_paise": -e["effect"] if e["effect"] < 0 else 0,
            "balance_paise": balances[e["date"]],
        })

    closed = [d for d in days if d["status"] == "closed"]
    totals = {
        "day_count": len(days),
        "cash_in_paise": sum(e["cash_in_paise"] for e in entries),
        "cash_out_paise": sum(e["cash_out_paise"] for e in entries),
        "closed_days": len(closed),
        "difference_paise": sum(d["difference_paise"] or 0 for d in closed),
        "days_with_difference": sum(1 for d in closed if d["difference_paise"]),
    }
    totals["net_change_paise"] = totals["cash_in_paise"] - totals["cash_out_paise"]
    return {"date_from": start, "date_to": end, "days": days, "entries": entries, "totals": totals,
            "formula": CASH_BOOK_FORMULA}


# ---------- expense report ----------

def _group(rows, key_of) -> list:
    totals = {}
    for r in rows:
        key = key_of(r)
        bucket = totals.setdefault(key, {"count": 0, "total_paise": 0})
        bucket["count"] += 1
        bucket["total_paise"] += r["amount_paise"]
    return [{"key": k, **totals[k]} for k in sorted(totals)]


def expense_report(conn, date_from, date_to, *, user_id, category=None) -> dict:
    """Every expense of the period, oldest first, with net totals by category, month and payment mode.

    `status` of a line: 'active', 'reversed' (cancelled by a later reversal) or 'reversal' (the negative
    line that cancels it). Reversals are negative, so every total here is already net.
    """
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    found = list_expenses(conn, user_id=user_id, date_from=start, date_to=end, category=category)
    rows = [{
        "id": r["id"], "date": r["expense_date"], "category": r["category"], "description": r["description"],
        "payment_mode": r["payment_mode"], "reference": r["reference"], "amount_paise": r["amount_paise"],
        "status": r["status"], "reason": r["reason"],
    } for r in found]
    rows.sort(key=lambda r: (r["date"], r["id"]))

    recorded = sum(r["amount_paise"] for r in rows if r["amount_paise"] > 0)
    reversed_total = -sum(r["amount_paise"] for r in rows if r["amount_paise"] < 0)
    return {
        "date_from": start, "date_to": end, "category": category, "rows": rows,
        "totals": {
            "line_count": len(rows),
            "recorded_paise": recorded,
            "reversed_paise": reversed_total,
            "net_paise": recorded - reversed_total,
        },
        "by_category": _group(rows, lambda r: r["category"]),
        "by_month": _group(rows, lambda r: r["date"][:7]),
        "by_payment_mode": _group(rows, lambda r: r["payment_mode"]),
        "formula": EXPENSE_FORMULA,
    }