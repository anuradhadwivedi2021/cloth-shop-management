"""Sales reports, day book and GST sales register (SRS 8, 16.1, 16.4 and the Tax group).

Rules every report here follows:
* Figures come from SAVED documents only (bills, credit notes, payments), never from screens.
* Sales value, money received and profit are different things and are shown separately.
  Nothing here is called "profit" (that needs cost and the owner's accounting rules).
* Net sales = taxable value of bills - taxable value of credit notes, before tax.
  The formula is printed in every summary.
* Each document is counted once.
* Reading needs the "view_reports" permission. No customer personal details appear in any report.

Dates are inclusive. A bill belongs to the date it was saved; a credit note to the date it was issued.
"""


from core.report_common import check_range, require_view

NET_SALES_FORMULA = "Net sales = taxable value of bills - taxable value of credit notes (both before tax)"
GST_NOTE = (
    "Layout follows the GSTR-1 headings (B2B, B2C rate-wise, credit notes, HSN summary). "
    "Check it with your accountant before filing."
)
GROUPS = ("product", "variant", "category", "brand", "day", "hour", "salesperson", "payment_mode")
_RETURN_AWARE = ("product", "variant", "category", "brand", "day")


def _in(column):
    return f"substr({column}, 1, 10) BETWEEN ? AND ?"


# ---------- summary ----------

def sales_summary(conn, date_from, date_to, *, user_id) -> dict:
    """Totals for the period: bills, credit notes, net sales and the formula."""
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    s = conn.execute(
        "SELECT COUNT(*) AS bills, COALESCE(SUM(subtotal_paise),0) AS gross, COALESCE(SUM(discount_paise),0) AS disc, "
        "COALESCE(SUM(taxable_paise),0) AS taxable, COALESCE(SUM(tax_paise),0) AS tax, "
        "COALESCE(SUM(round_off_paise),0) AS rnd, COALESCE(SUM(total_paise),0) AS total "
        f"FROM sales WHERE status = 'completed' AND {_in('created_at')}", (start, end),
    ).fetchone()
    items = conn.execute(
        "SELECT COALESCE(SUM(sl.qty),0) FROM sale_lines sl JOIN sales s ON s.id = sl.sale_id "
        f"WHERE s.status = 'completed' AND {_in('s.created_at')}", (start, end),
    ).fetchone()[0]
    r = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(taxable_paise),0) AS taxable, COALESCE(SUM(tax_paise),0) AS tax, "
        "COALESCE(SUM(round_off_paise),0) AS rnd, COALESCE(SUM(total_paise),0) AS total, "
        "COALESCE(SUM(refund_paise),0) AS refunds, COALESCE(SUM(store_credit_paise),0) AS credit, "
        "COALESCE(SUM(CASE WHEN outcome = 'exchange' THEN 1 ELSE 0 END),0) AS exchanges "
        f"FROM sale_returns WHERE {_in('created_at')}", (start, end),
    ).fetchone()
    returned_qty = conn.execute(
        "SELECT COALESCE(SUM(l.qty),0) FROM sale_return_lines l JOIN sale_returns r ON r.id = l.return_id "
        f"WHERE {_in('r.created_at')}", (start, end),
    ).fetchone()[0]
    cancelled = conn.execute(
        f"SELECT COUNT(*) FROM sales WHERE status = 'cancelled' AND {_in('created_at')}", (start, end)
    ).fetchone()[0]
    return {
        "date_from": start, "date_to": end,
        "bill_count": s["bills"], "cancelled_bills": cancelled, "items_sold": items,
        "average_bill_paise": round(s["total"] / s["bills"]) if s["bills"] else 0,
        "gross_paise": s["gross"], "discount_paise": s["disc"], "taxable_paise": s["taxable"],
        "tax_paise": s["tax"], "round_off_paise": s["rnd"], "invoice_total_paise": s["total"],
        "credit_note_count": r["n"], "exchange_count": r["exchanges"], "items_returned": returned_qty,
        "credit_notes_taxable_paise": r["taxable"], "credit_notes_tax_paise": r["tax"],
        "credit_notes_total_paise": r["total"], "refunds_paise": r["refunds"], "store_credit_paise": r["credit"],
        "net_sales_paise": s["taxable"] - r["taxable"], "net_tax_paise": s["tax"] - r["tax"],
        "formula": NET_SALES_FORMULA,
    }


# ---------- money received ----------

def payment_mode_summary(conn, date_from, date_to, *, user_id) -> list:
    """Money received on bills and refunded on credit notes, by payment method.

    This is money, not sales value: a credit-note "store credit" is not money paid out.
    """
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    rows = conn.execute(
        "SELECT mode, COALESCE(SUM(CASE WHEN document_type = 'sale' THEN amount_paise END), 0) AS received, "
        "COALESCE(-SUM(CASE WHEN document_type = 'sale_return' THEN amount_paise END), 0) AS refunded "
        "FROM payments WHERE document_type IN ('sale','sale_return') AND " + _in("created_at") +
        " GROUP BY mode ORDER BY mode", (start, end),
    ).fetchall()
    return [
        {"mode": r["mode"], "received_paise": r["received"], "refunded_paise": r["refunded"],
         "net_paise": r["received"] - r["refunded"]}
        for r in rows
    ]


# ---------- grouped sales ----------

def _group_sql(group_by):
    """(sales_select, sales_joins, returns_select, returns_joins) for the groupings that have them."""
    sales_from = "FROM sale_lines sl JOIN sales s ON s.id = sl.sale_id"
    ret_from = "FROM sale_return_lines l JOIN sale_returns r ON r.id = l.return_id"
    vp_sl = " JOIN variants v ON v.id = sl.variant_id JOIN products p ON p.id = v.product_id"
    vp_l = " JOIN variants v ON v.id = l.variant_id JOIN products p ON p.id = v.product_id"
    if group_by == "product":
        return "sl.product_name", sales_from, "l.product_name", ret_from
    if group_by == "variant":
        return ("sl.product_name || ' | ' || sl.size || ' | ' || sl.colour", sales_from,
                "l.product_name || ' | ' || l.size || ' | ' || l.colour", ret_from)
    if group_by == "category":
        return "COALESCE(p.category, '(none)')", sales_from + vp_sl, "COALESCE(p.category, '(none)')", ret_from + vp_l
    if group_by == "brand":
        return "COALESCE(p.brand, '(none)')", sales_from + vp_sl, "COALESCE(p.brand, '(none)')", ret_from + vp_l
    if group_by == "day":
        return "substr(s.created_at, 1, 10)", sales_from, "substr(r.created_at, 1, 10)", ret_from
    return None


def sales_by(conn, date_from, date_to, group_by, *, user_id) -> list:
    """Sales grouped by product, variant, category, brand, day, hour, salesperson or payment mode.

    For product, variant, category, brand and day the rows also show what was returned and the net.
    Hour, salesperson and payment-mode rows show bills only (credit notes are not split that way).
    Largest taxable value first; day and hour are in time order.
    """
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    if group_by not in GROUPS:
        raise ValueError(f"Group by one of: {', '.join(GROUPS)}.")

    if group_by == "payment_mode":
        rows = conn.execute(
            "SELECT mode AS k, COUNT(DISTINCT document_id) AS bills, COALESCE(SUM(amount_paise),0) AS total "
            "FROM payments WHERE document_type = 'sale' AND " + _in("created_at") + " GROUP BY mode ORDER BY total DESC",
            (start, end),
        ).fetchall()
        return [{"key": r["k"], "label": r["k"], "bills": r["bills"], "total_paise": r["total"]} for r in rows]

    if group_by in ("hour", "salesperson"):
        key = "substr(s.created_at, 12, 2)" if group_by == "hour" else "COALESCE(u.full_name, u.username)"
        join = "" if group_by == "hour" else " JOIN users u ON u.id = s.salesperson_id"
        rows = conn.execute(
            f"SELECT {key} AS k, COUNT(DISTINCT s.id) AS bills, SUM(sl.qty) AS qty, SUM(sl.taxable_paise) AS taxable, "
            f"SUM(sl.tax_paise) AS tax, SUM(sl.line_total_paise) AS total, SUM(sl.discount_paise) AS disc "
            f"FROM sale_lines sl JOIN sales s ON s.id = sl.sale_id{join} "
            f"WHERE s.status = 'completed' AND {_in('s.created_at')} GROUP BY k", (start, end),
        ).fetchall()
        out = [
            {"key": r["k"], "label": r["k"], "bills": r["bills"], "qty": r["qty"], "taxable_paise": r["taxable"],
             "tax_paise": r["tax"], "total_paise": r["total"], "discount_paise": r["disc"]}
            for r in rows
        ]
        return sorted(out, key=(lambda r: r["key"]) if group_by == "hour" else (lambda r: -r["taxable_paise"]))

    sel, frm, rsel, rfrm = _group_sql(group_by)
    sold = {
        r["k"]: r for r in conn.execute(
            f"SELECT {sel} AS k, COUNT(DISTINCT s.id) AS bills, SUM(sl.qty) AS qty, SUM(sl.taxable_paise) AS taxable, "
            f"SUM(sl.tax_paise) AS tax, SUM(sl.line_total_paise) AS total, SUM(sl.discount_paise) AS disc "
            f"{frm} WHERE s.status = 'completed' AND {_in('s.created_at')} GROUP BY k", (start, end),
        )
    }
    returned = {
        r["k"]: r for r in conn.execute(
            f"SELECT {rsel} AS k, SUM(l.qty) AS qty, SUM(l.taxable_paise) AS taxable, SUM(l.tax_paise) AS tax "
            f"{rfrm} WHERE {_in('r.created_at')} GROUP BY k", (start, end),
        )
    }
    out = []
    for k in set(sold) | set(returned):
        s, r = sold.get(k), returned.get(k)
        taxable = s["taxable"] if s else 0
        r_taxable = r["taxable"] if r else 0
        out.append({
            "key": k, "label": k,
            "bills": s["bills"] if s else 0, "qty": s["qty"] if s else 0,
            "taxable_paise": taxable, "tax_paise": s["tax"] if s else 0,
            "total_paise": s["total"] if s else 0, "discount_paise": s["disc"] if s else 0,
            "returned_qty": r["qty"] if r else 0, "returned_taxable_paise": r_taxable,
            "net_taxable_paise": taxable - r_taxable,
        })
    return sorted(out, key=(lambda x: x["key"]) if group_by == "day" else (lambda x: (-x["taxable_paise"], x["key"])))


# ---------- day book ----------

def day_book(conn, date_from, date_to, *, user_id) -> dict:
    """Every document of the period in time order, with totals per kind.

    Kinds: sale, credit_note, purchase, debit_note, expense. Cancelled bills are listed but not totalled.
    No customer details are shown.
    """
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    entries = []

    def add(day, time, kind, row_id, number, party, taxable, tax, total, note=None):
        entries.append({
            "date": day, "time": time or "00:00:00", "type": kind, "id": row_id, "number": number, "party": party,
            "taxable_paise": taxable, "tax_paise": tax, "total_paise": total, "note": note,
        })

    for r in conn.execute(
        "SELECT id, created_at, invoice_no, tax_mode, status, taxable_paise, tax_paise, total_paise FROM sales "
        f"WHERE {_in('created_at')}", (start, end),
    ):
        add(r["created_at"][:10], r["created_at"][11:], "sale", r["id"], r["invoice_no"], None,
            r["taxable_paise"], r["tax_paise"], r["total_paise"], "cancelled" if r["status"] == "cancelled" else r["tax_mode"])
    for r in conn.execute(
        "SELECT r.id, r.created_at, r.credit_note_no, r.outcome, r.taxable_paise, r.tax_paise, r.total_paise, s.invoice_no "
        f"FROM sale_returns r JOIN sales s ON s.id = r.sale_id WHERE {_in('r.created_at')}", (start, end),
    ):
        add(r["created_at"][:10], r["created_at"][11:], "credit_note", r["id"], r["credit_note_no"], None,
            r["taxable_paise"], r["tax_paise"], r["total_paise"], f"{r['outcome']} against {r['invoice_no']}")
    for r in conn.execute(
        "SELECT p.id, COALESCE(p.confirmed_at, p.created_at) AS at, p.voucher_no, p.invoice_no, s.name, "
        "p.subtotal_paise, p.tax_paise, p.total_paise FROM purchases p JOIN suppliers s ON s.id = p.supplier_id "
        f"WHERE p.status = 'confirmed' AND {_in('COALESCE(p.confirmed_at, p.created_at)')}", (start, end),
    ):
        add(r["at"][:10], r["at"][11:], "purchase", r["id"], r["voucher_no"], r["name"],
            r["subtotal_paise"], r["tax_paise"], r["total_paise"], f"supplier invoice {r['invoice_no']}")
    for r in conn.execute(
        "SELECT d.id, d.created_at, d.debit_note_no, d.subtotal_paise, d.tax_paise, d.total_paise, s.name "
        f"FROM purchase_returns d JOIN suppliers s ON s.id = d.supplier_id WHERE {_in('d.created_at')}", (start, end),
    ):
        add(r["created_at"][:10], r["created_at"][11:], "debit_note", r["id"], r["debit_note_no"], r["name"],
            r["subtotal_paise"], r["tax_paise"], r["total_paise"])
    for r in conn.execute(
        "SELECT id, expense_date, created_at, category, description, amount_paise FROM expenses "
        "WHERE expense_date BETWEEN ? AND ?", (start, end),
    ):
        add(r["expense_date"], r["created_at"][11:], "expense", r["id"], f"EXP-{r['id']}", r["category"],
            0, 0, r["amount_paise"], r["description"])

    order = {"sale": 0, "credit_note": 1, "purchase": 2, "debit_note": 3, "expense": 4}
    entries.sort(key=lambda e: (e["date"], e["time"], order[e["type"]], e["id"]))

    totals = {}
    for kind in order:
        rows = [e for e in entries if e["type"] == kind and e["note"] != "cancelled"]
        totals[kind] = {
            "count": len(rows), "taxable_paise": sum(e["taxable_paise"] for e in rows),
            "tax_paise": sum(e["tax_paise"] for e in rows), "total_paise": sum(e["total_paise"] for e in rows),
        }
    return {"date_from": start, "date_to": end, "entries": entries, "totals": totals}


# ---------- checks ----------

def reconcile(conn, date_from, date_to, *, user_id) -> dict:
    """Check that the reports add up to the saved documents, to the paisa.

    Returns {"ok": bool, "problems": [text, ...], "unpaid_bills": [invoice_no, ...]}.
    Problems are things that should never happen (a bill whose lines do not add up to its total,
    or the summary not equal to the day book). unpaid_bills is information: bills not fully paid
    (normal only if credit sales are used).
    """
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    problems = []

    for s in conn.execute(
        "SELECT s.id, s.invoice_no, s.taxable_paise, s.tax_paise, s.round_off_paise, s.total_paise, "
        "COALESCE(SUM(l.taxable_paise), 0) AS lt, COALESCE(SUM(l.tax_paise), 0) AS ltax "
        "FROM sales s LEFT JOIN sale_lines l ON l.sale_id = s.id "
        f"WHERE s.status = 'completed' AND {_in('s.created_at')} GROUP BY s.id", (start, end),
    ):
        if s["lt"] != s["taxable_paise"] or s["ltax"] != s["tax_paise"]:
            problems.append(f"Bill {s['invoice_no']}: lines do not add up to the bill's taxable value or tax.")
        if s["taxable_paise"] + s["tax_paise"] + s["round_off_paise"] != s["total_paise"]:
            problems.append(f"Bill {s['invoice_no']}: taxable + tax + round-off is not the bill total.")
    for r in conn.execute(
        "SELECT r.id, r.credit_note_no, r.taxable_paise, r.tax_paise, r.round_off_paise, r.total_paise, "
        "COALESCE(SUM(l.taxable_paise), 0) AS lt, COALESCE(SUM(l.tax_paise), 0) AS ltax "
        "FROM sale_returns r LEFT JOIN sale_return_lines l ON l.return_id = r.id "
        f"WHERE {_in('r.created_at')} GROUP BY r.id", (start, end),
    ):
        if r["lt"] != r["taxable_paise"] or r["ltax"] != r["tax_paise"]:
            problems.append(f"Credit note {r['credit_note_no']}: lines do not add up to its taxable value or tax.")
        if r["taxable_paise"] + r["tax_paise"] + r["round_off_paise"] != r["total_paise"]:
            problems.append(f"Credit note {r['credit_note_no']}: taxable + tax + round-off is not its total.")

    summary = sales_summary(conn, start, end, user_id=user_id)
    book = day_book(conn, start, end, user_id=user_id)["totals"]
    pairs = (
        ("bills", summary["bill_count"], book["sale"]["count"]),
        ("bill taxable value", summary["taxable_paise"], book["sale"]["taxable_paise"]),
        ("bill tax", summary["tax_paise"], book["sale"]["tax_paise"]),
        ("bill total", summary["invoice_total_paise"], book["sale"]["total_paise"]),
        ("credit note total", summary["credit_notes_total_paise"], book["credit_note"]["total_paise"]),
        ("credit note taxable value", summary["credit_notes_taxable_paise"], book["credit_note"]["taxable_paise"]),
    )
    for label, from_summary, from_book in pairs:
        if from_summary != from_book:
            problems.append(f"Summary and day book differ for {label}: {from_summary} against {from_book}.")

    unpaid = [
        r["invoice_no"] for r in conn.execute(
            "SELECT s.invoice_no FROM sales s WHERE s.status = 'completed' AND " + _in("s.created_at") +
            " AND s.total_paise != COALESCE((SELECT SUM(amount_paise) FROM payments "
            "WHERE document_type = 'sale' AND document_id = s.id), 0) ORDER BY s.id", (start, end),
        )
    ]
    return {"ok": not problems, "problems": problems, "unpaid_bills": unpaid}


def series_gap_check(conn, *, user_id) -> list:
    """Find missing numbers in bill, credit note and debit note series (there should be none).

    Numbers are taken inside the same transaction that saves the document, so a gap means
    something was removed or tampered with. Returns [{"document", "series", "fin_year", "missing": [..]}].
    """
    require_view(conn, user_id)
    gaps = []
    for label, table in (("bill", "sales"), ("credit note", "sale_returns"), ("debit note", "purchase_returns")):
        groups = conn.execute(
            f"SELECT series, fin_year, MIN(number) AS lo, MAX(number) AS hi FROM {table} GROUP BY series, fin_year"
        ).fetchall()
        for g in groups:
            have = {r[0] for r in conn.execute(
                f"SELECT number FROM {table} WHERE series = ? AND fin_year = ?", (g["series"], g["fin_year"])
            )}
            missing = [n for n in range(1, g["hi"] + 1) if n not in have]
            if missing:
                gaps.append({"document": label, "series": g["series"], "fin_year": g["fin_year"], "missing": missing})
    return gaps


# ---------- GST sales register ----------

def _split(tax_col, intra_col):
    """SQL pieces: CGST is half the line tax (rounded down), SGST the rest; inter-state is all IGST."""
    cgst = f"COALESCE(SUM(CASE WHEN {intra_col} = 1 THEN {tax_col} / 2 ELSE 0 END), 0)"
    igst = f"COALESCE(SUM(CASE WHEN {intra_col} = 0 THEN {tax_col} ELSE 0 END), 0)"
    return cgst, igst


def gst_sales_register(conn, date_from, date_to, *, user_id) -> dict:
    """Sales for GST filing, in GSTR-1 style sections. Confirm the layout with your accountant.

    b2b          bills to customers who have a GSTIN (one row per bill)
    b2c          all other regular bills, rate by rate
    credit_notes one row per credit note
    hsn_summary  quantity and value by HSN and rate, bills less credit notes
    non_gst      bills made as composition / unregistered (no tax shown)
    totals       bills, credit notes and net
    """
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    cgst, igst = _split("l.tax_paise", "s.intra_state")

    b2b = []
    for r in conn.execute(
        f"SELECT s.invoice_no, substr(s.created_at,1,10) AS d, c.gstin, s.taxable_paise, s.total_paise, "
        f"{cgst} AS cgst, {igst} AS igst, SUM(l.tax_paise) AS tax FROM sales s JOIN sale_lines l ON l.sale_id = s.id "
        f"JOIN customers c ON c.id = s.customer_id WHERE s.status = 'completed' AND s.tax_mode = 'regular' "
        f"AND COALESCE(c.gstin, '') != '' AND {_in('s.created_at')} GROUP BY s.id ORDER BY s.id", (start, end),
    ):
        b2b.append({
            "invoice_no": r["invoice_no"], "date": r["d"], "customer_gstin": r["gstin"],
            "taxable_paise": r["taxable_paise"], "cgst_paise": r["cgst"], "sgst_paise": r["tax"] - r["cgst"] - r["igst"],
            "igst_paise": r["igst"], "total_paise": r["total_paise"],
        })

    b2c = [
        {"rate_percent": r["rate"], "taxable_paise": r["taxable"], "cgst_paise": r["cgst"],
         "sgst_paise": r["tax"] - r["cgst"] - r["igst"], "igst_paise": r["igst"]}
        for r in conn.execute(
            f"SELECT l.tax_rate_percent AS rate, SUM(l.taxable_paise) AS taxable, SUM(l.tax_paise) AS tax, "
            f"{cgst} AS cgst, {igst} AS igst FROM sales s JOIN sale_lines l ON l.sale_id = s.id "
            f"LEFT JOIN customers c ON c.id = s.customer_id WHERE s.status = 'completed' AND s.tax_mode = 'regular' "
            f"AND COALESCE(c.gstin, '') = '' AND {_in('s.created_at')} GROUP BY l.tax_rate_percent ORDER BY l.tax_rate_percent",
            (start, end),
        )
    ]

    r_cgst, r_igst = _split("l.tax_paise", "s.intra_state")
    credit_notes = []
    for r in conn.execute(
        f"SELECT n.credit_note_no, substr(n.created_at,1,10) AS d, s.invoice_no, c.gstin, n.taxable_paise, n.total_paise, "
        f"SUM(l.tax_paise) AS tax, {r_cgst} AS cgst, {r_igst} AS igst FROM sale_returns n "
        f"JOIN sales s ON s.id = n.sale_id JOIN sale_return_lines l ON l.return_id = n.id "
        f"LEFT JOIN customers c ON c.id = n.customer_id WHERE s.tax_mode = 'regular' AND {_in('n.created_at')} "
        f"GROUP BY n.id ORDER BY n.id", (start, end),
    ):
        credit_notes.append({
            "credit_note_no": r["credit_note_no"], "date": r["d"], "against_invoice": r["invoice_no"],
            "customer_gstin": r["gstin"] or None, "taxable_paise": r["taxable_paise"], "cgst_paise": r["cgst"],
            "sgst_paise": r["tax"] - r["cgst"] - r["igst"], "igst_paise": r["igst"], "total_paise": r["total_paise"],
        })

    hsn = {}
    for r in conn.execute(
        f"SELECT COALESCE(l.hsn, '') AS hsn, l.tax_rate_percent AS rate, SUM(l.qty) AS qty, SUM(l.taxable_paise) AS taxable, "
        f"SUM(l.tax_paise) AS tax FROM sales s JOIN sale_lines l ON l.sale_id = s.id WHERE s.status = 'completed' "
        f"AND s.tax_mode = 'regular' AND {_in('s.created_at')} GROUP BY hsn, rate", (start, end),
    ):
        hsn[(r["hsn"], r["rate"])] = {
            "hsn": r["hsn"], "rate_percent": r["rate"], "qty": r["qty"], "taxable_paise": r["taxable"],
            "tax_paise": r["tax"], "returned_qty": 0, "returned_taxable_paise": 0, "returned_tax_paise": 0,
        }
    for r in conn.execute(
        f"SELECT COALESCE(sl.hsn, '') AS hsn, sl.tax_rate_percent AS rate, SUM(l.qty) AS qty, SUM(l.taxable_paise) AS taxable, "
        f"SUM(l.tax_paise) AS tax FROM sale_return_lines l JOIN sale_returns n ON n.id = l.return_id "
        f"JOIN sale_lines sl ON sl.id = l.sale_line_id JOIN sales s ON s.id = n.sale_id "
        f"WHERE s.tax_mode = 'regular' AND {_in('n.created_at')} GROUP BY hsn, rate", (start, end),
    ):
        row = hsn.setdefault((r["hsn"], r["rate"]), {
            "hsn": r["hsn"], "rate_percent": r["rate"], "qty": 0, "taxable_paise": 0, "tax_paise": 0,
            "returned_qty": 0, "returned_taxable_paise": 0, "returned_tax_paise": 0,
        })
        row.update(returned_qty=r["qty"], returned_taxable_paise=r["taxable"], returned_tax_paise=r["tax"])
    hsn_summary = []
    for row in sorted(hsn.values(), key=lambda x: (x["hsn"], x["rate_percent"])):
        row["net_qty"] = row["qty"] - row["returned_qty"]
        row["net_taxable_paise"] = row["taxable_paise"] - row["returned_taxable_paise"]
        row["net_tax_paise"] = row["tax_paise"] - row["returned_tax_paise"]
        hsn_summary.append(row)

    nr = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(taxable_paise),0) AS taxable FROM sales WHERE status = 'completed' "
        f"AND tax_mode != 'regular' AND {_in('created_at')}", (start, end),
    ).fetchone()

    def total(rows):
        return {k: sum(r[k] for r in rows) for k in ("taxable_paise", "cgst_paise", "sgst_paise", "igst_paise")}

    sales_rows = b2b + b2c
    t_sales, t_cn = total(sales_rows), total(credit_notes)
    return {
        "date_from": start, "date_to": end, "b2b": b2b, "b2c": b2c, "credit_notes": credit_notes,
        "hsn_summary": hsn_summary,
        "non_gst": {"bills": nr["n"], "taxable_paise": nr["taxable"]},
        "totals": {
            "sales": t_sales, "credit_notes": t_cn,
            "net": {k: t_sales[k] - t_cn[k] for k in t_sales},
        },
        "note": GST_NOTE,
    }
