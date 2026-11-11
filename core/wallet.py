"""CUS-05 / CUS-06: loyalty points, gift vouchers and store credit, used as part payment of a bill.

Points and store credit are ledgers (balance = sum of entries); a voucher keeps its balance next to an append-only ledger and every use happens INSIDE the transaction
that saves the bill: if the bill fails, nothing is taken from any balance.

  balances(conn, customer_id)                    points, their value, and store credit
  issue_voucher / get_voucher / cancel_voucher   gift vouchers (sold for money, so the cash book sees it)
  adjust_points / adjust_store_credit            owner corrections, with a note
  plan_tenders(...)                              used by core.sales: checks what the customer wants to use on a bill
  claw_back_points(...)                          used by core.returns: points earned on returned goods are taken back

Tenders (what the customer pays WITH, besides money) look like:
  {"kind": "store_credit", "amount_paise": 50000}
  {"kind": "loyalty",      "points": 120}
  {"kind": "voucher",      "code": "GV-ABCD-2345", "amount_paise": optional (default: as much as the bill needs)}
They can never pay more than the bill. Points are earned on the part of the bill paid with real money, not on
the part paid with points, vouchers or store credit.
"""

import secrets
from datetime import date

from core.audit import log_event
from core.permissions import require_permission
from core.settings import get_bool, get_setting
from db.connection import transaction

TENDER_KINDS = ("store_credit", "loyalty", "voucher")
PAY_MODES = ("cash", "upi", "card", "bank_transfer", "cheque", "other")
MAX_VOUCHER_PAISE = 10_00_000 * 100
_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"          # no 0/O/1/I: easy to read out and type


# ---------------------------------------------------------------- settings and balances

def loyalty_settings(conn) -> dict:
    def number(key, default):
        try:
            return int(get_setting(conn, key, default))
        except (TypeError, ValueError):
            return default
    return {"enabled": get_bool(conn, "loyalty_enabled"), "per_100": number("loyalty_points_per_100", 1),
            "point_value_paise": number("loyalty_point_value_paise", 100),
            "min_redeem": number("loyalty_min_redeem_points", 50)}


def _customer(conn, customer_id):
    if customer_id is None or not conn.execute(
            "SELECT 1 FROM customers WHERE id = ? AND is_active = 1", (customer_id,)).fetchone():
        raise ValueError("Customer not found or not active.")


def points_balance(conn, customer_id) -> int:
    return conn.execute("SELECT COALESCE(SUM(points),0) FROM loyalty_ledger WHERE customer_id = ?", (customer_id,)).fetchone()[0]


def store_credit_balance(conn, customer_id) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_paise),0) FROM store_credit_ledger WHERE customer_id = ?",
                        (customer_id,)).fetchone()[0]


def balances(conn, customer_id) -> dict:
    cfg = loyalty_settings(conn)
    points = points_balance(conn, customer_id)
    return {"points": points, "points_value_paise": max(points, 0) * cfg["point_value_paise"],
            "store_credit_paise": store_credit_balance(conn, customer_id), "loyalty_enabled": cfg["enabled"]}


# ---------------------------------------------------------------- vouchers

def _norm_code(code) -> str:
    return "".join(str(code or "").upper().split())


def _voucher_row(conn, code):
    row = conn.execute("SELECT * FROM gift_vouchers WHERE code = ?", (_norm_code(code),)).fetchone()
    if row is None:
        raise ValueError("This voucher code was not found.")
    return row


def _voucher_view(conn, row, on=None) -> dict:
    on = (on or date.today()).isoformat() if not isinstance(on, str) else on
    balance = row["balance_paise"]
    expired = bool(row["expiry_date"]) and row["expiry_date"] < on
    usable = row["status"] == "ACTIVE" and not expired and balance > 0
    return {"id": row["id"], "code": row["code"], "value_paise": row["initial_amount_paise"], "balance_paise": balance,
            "customer_id": row["customer_id"], "expires_on": row["expiry_date"], "status": row["status"].lower(),
            "expired": expired, "usable": usable}


def get_voucher(conn, code, *, on=None) -> dict:
    return _voucher_view(conn, _voucher_row(conn, code), on)


def _new_code(conn) -> str:
    for _ in range(20):
        raw = "".join(secrets.choice(_CODE_CHARS) for _ in range(8))
        code = f"GV-{raw[:4]}-{raw[4:]}"
        if not conn.execute("SELECT 1 FROM gift_vouchers WHERE code = ?", (code,)).fetchone():
            return code
    raise RuntimeError("Could not make a voucher code.")


def issue_voucher(conn, user_id, value_paise, *, pay_mode="cash", customer_id=None, expires_on=None, code=None,
                  today=None) -> dict:
    """Sell a gift voucher. The money taken is recorded as a payment, so the cash book stays right."""
    require_permission(conn, user_id, "sell", "sell a gift voucher")
    if isinstance(value_paise, bool) or not isinstance(value_paise, int) or not 0 < value_paise <= MAX_VOUCHER_PAISE:
        raise ValueError("The voucher value must be more than zero.")
    if pay_mode not in PAY_MODES:
        raise ValueError("Choose how the voucher was paid for.")
    today = today or date.today()
    if expires_on:
        try:
            if date.fromisoformat(str(expires_on)) < today:
                raise ValueError("The expiry date is already past.")
        except ValueError as exc:
            if "past" in str(exc):
                raise
            raise ValueError("The expiry date must be a valid date (YYYY-MM-DD).") from None
    else:
        expires_on = None
    code = _norm_code(code)
    if code and not (4 <= len(code) <= 30 and all(c.isalnum() or c == "-" for c in code)):
        raise ValueError("A voucher code can have 4 to 30 letters, digits or dashes.")
    with transaction(conn):
        if customer_id is not None:
            _customer(conn, customer_id)
        code = code or _new_code(conn)
        if conn.execute("SELECT 1 FROM gift_vouchers WHERE code = ?", (code,)).fetchone():
            raise ValueError("This voucher code is already used.")
        vid = conn.execute("INSERT INTO gift_vouchers(code, customer_id, initial_amount_paise, balance_paise, expiry_date) "
                           "VALUES (?,?,?,?,?)", (code, customer_id, value_paise, value_paise, expires_on)).lastrowid
        conn.execute("INSERT INTO voucher_ledger(voucher_id, amount_paise, doc_type, doc_id, user_id) VALUES (?,?,?,?,?)",
                     (vid, value_paise, "issue", vid, user_id))
        conn.execute("INSERT INTO payments(document_type, document_id, mode, amount_paise, reference, user_id) VALUES ('other',?,?,?,?,?)",
                     (vid, pay_mode, value_paise, f"voucher {code}", user_id))
        log_event(conn, "voucher_issued", user_id=user_id, entity="voucher", entity_id=vid,
                  after={"value_paise": value_paise, "pay_mode": pay_mode, "expires_on": expires_on})
    return {"id": vid, "code": code}


def cancel_voucher(conn, user_id, code, *, reason) -> None:
    require_permission(conn, user_id, "manage_settings", "cancel a voucher")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to cancel a voucher.")
    with transaction(conn):
        row = _voucher_row(conn, code)
        if row["status"] == "CANCELLED":
            raise ValueError("This voucher is already cancelled.")
        conn.execute("UPDATE gift_vouchers SET status = 'CANCELLED' WHERE id = ?", (row["id"],))
        log_event(conn, "voucher_cancelled", user_id=user_id, entity="voucher", entity_id=row["id"], after={"reason": reason[:200]})


def list_vouchers(conn, user_id, *, only_usable=False, limit=500) -> list:
    require_permission(conn, user_id, "sell", "see vouchers")
    out = [_voucher_view(conn, r) for r in conn.execute("SELECT * FROM gift_vouchers ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 2000)),))]
    return [v for v in out if v["usable"]] if only_usable else out


# ---------------------------------------------------------------- owner corrections

def adjust_points(conn, user_id, customer_id, points, note) -> None:
    require_permission(conn, user_id, "manage_settings", "change loyalty points")
    if isinstance(points, bool) or not isinstance(points, int) or points == 0:
        raise ValueError("Points must be a whole number, not zero.")
    if not (note or "").strip():
        raise ValueError("A note is required for every correction.")
    with transaction(conn):
        _customer(conn, customer_id)
        if points < 0 and points_balance(conn, customer_id) + points < 0:
            raise ValueError("The customer does not have that many points.")
        conn.execute("INSERT INTO loyalty_ledger(customer_id, points, tx_type, doc_type, note, user_id) VALUES (?,?,?,?,?,?)",
                     (customer_id, points, "EARNED_SALE" if points > 0 else "EXPIRY", "adjustment", note.strip()[:200], user_id))
        log_event(conn, "loyalty_adjusted", user_id=user_id, entity="customer", entity_id=customer_id, after={"points": points})


def adjust_store_credit(conn, user_id, customer_id, amount_paise, note) -> None:
    require_permission(conn, user_id, "manage_settings", "change store credit")
    if isinstance(amount_paise, bool) or not isinstance(amount_paise, int) or amount_paise == 0:
        raise ValueError("The amount must be a whole number of paise, not zero.")
    if not (note or "").strip():
        raise ValueError("A note is required for every correction.")
    with transaction(conn):
        _customer(conn, customer_id)
        if amount_paise < 0 and store_credit_balance(conn, customer_id) + amount_paise < 0:
            raise ValueError("The customer does not have that much store credit.")
        conn.execute("INSERT INTO store_credit_ledger(customer_id, amount_paise, doc_type, note, user_id) VALUES (?,?,?,?,?)",
                     (customer_id, amount_paise, "adjustment", note.strip()[:200], user_id))
        log_event(conn, "store_credit_adjusted", user_id=user_id, entity="customer", entity_id=customer_id,
                  after={"amount_paise": amount_paise})


def history(conn, user_id, customer_id) -> dict:
    require_permission(conn, user_id, "view_customers", "see loyalty and credit")
    return {
        "points_log": [dict(r) for r in conn.execute(
            "SELECT created_at, points, doc_type, note FROM loyalty_ledger WHERE customer_id = ? ORDER BY id DESC LIMIT 200", (customer_id,))],
        "store_credit_log": [dict(r) for r in conn.execute(
            "SELECT created_at, amount_paise, doc_type, note FROM store_credit_ledger WHERE customer_id = ? ORDER BY id DESC LIMIT 200", (customer_id,))],
        **balances(conn, customer_id),
    }


# ---------------------------------------------------------------- on a bill

class TenderPlan:
    """What the customer pays with besides money: payment rows to insert, and the ledger entries to write."""

    def __init__(self):
        self.payments, self.entries, self.total_paise = [], [], 0

    def apply(self, conn, sale_id, user_id) -> None:
        for kind, key, amount in self.entries:
            if kind == "store_credit":
                conn.execute("INSERT INTO store_credit_ledger(customer_id, amount_paise, doc_type, doc_id, note, user_id) VALUES (?,?,?,?,?,?)",
                             (key, -amount, "sale", sale_id, "used on a bill", user_id))
            elif kind == "loyalty":
                conn.execute("INSERT INTO loyalty_ledger(customer_id, points, tx_type, doc_type, doc_id, note, user_id) VALUES (?,?,?,?,?,?,?)",
                             (key, -amount, "REDEEMED_SALE", "sale", sale_id, "used on a bill", user_id))
            else:
                conn.execute("INSERT INTO voucher_ledger(voucher_id, amount_paise, doc_type, doc_id, user_id) VALUES (?,?,?,?,?)",
                             (key, -amount, "sale", sale_id, user_id))
                conn.execute("UPDATE gift_vouchers SET balance_paise = balance_paise - ?, "
                             "status = CASE WHEN balance_paise - ? = 0 THEN 'FULLY_REDEEMED' ELSE status END WHERE id = ?",
                             (amount, amount, key))


def plan_tenders(conn, tenders, *, customer_id, limit_paise, on) -> TenderPlan:
    """Check every tender against the balances. `limit_paise` is what is still to pay on the bill."""
    plan = TenderPlan()
    if not tenders:
        return plan
    if not isinstance(tenders, (list, tuple)):
        raise ValueError("The payment with points, vouchers or store credit is not valid.")
    remaining = limit_paise
    used_vouchers, kinds_seen = set(), set()
    cfg = loyalty_settings(conn)
    for t in tenders:
        kind = t.get("kind")
        if kind not in TENDER_KINDS:
            raise ValueError("Unknown way of paying.")
        if kind in ("store_credit", "loyalty"):
            if kind in kinds_seen:
                raise ValueError("Each of store credit and points can be used once on a bill.")
            kinds_seen.add(kind)
            _customer(conn, customer_id)
        if kind == "store_credit":
            amount = t.get("amount_paise")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise ValueError("The store credit to use must be more than zero.")
            if amount > store_credit_balance(conn, customer_id):
                raise ValueError("The customer does not have that much store credit.")
            reference, entry = None, ("store_credit", customer_id, amount)
        elif kind == "loyalty":
            points = t.get("points")
            if isinstance(points, bool) or not isinstance(points, int) or points <= 0:
                raise ValueError("The points to use must be more than zero.")
            if not cfg["enabled"]:
                raise ValueError("Loyalty points are switched off.")
            if points < cfg["min_redeem"]:
                raise ValueError(f"At least {cfg['min_redeem']} points must be used at once.")
            if points > points_balance(conn, customer_id):
                raise ValueError("The customer does not have that many points.")
            amount = points * cfg["point_value_paise"]
            reference, entry = f"{points} points", ("loyalty", customer_id, points)
        else:
            view = get_voucher(conn, t.get("code"), on=on)
            if view["id"] in used_vouchers:
                raise ValueError("A voucher can be used once on a bill.")
            used_vouchers.add(view["id"])
            if view["status"] == "cancelled":
                raise ValueError("This voucher was cancelled.")
            if view["expired"]:
                raise ValueError("This voucher has expired.")
            if view["balance_paise"] <= 0:
                raise ValueError("This voucher has no balance left.")
            if view["customer_id"] is not None and view["customer_id"] != customer_id:
                raise ValueError("This voucher belongs to another customer.")
            amount = t.get("amount_paise")
            if amount is None:
                amount = min(view["balance_paise"], remaining)
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise ValueError("The voucher amount to use must be more than zero.")
            if amount > view["balance_paise"]:
                raise ValueError("The voucher does not have that much left.")
            reference, entry = view["code"], ("voucher", view["id"], amount)
        if amount > remaining:
            raise ValueError("Points, vouchers and store credit together are more than the bill.")
        remaining -= amount
        plan.total_paise += amount
        plan.payments.append({"mode": {"store_credit": "store_credit", "loyalty": "loyalty", "voucher": "voucher"}[kind],
                              "amount_paise": amount, "reference": reference})
        plan.entries.append(entry)
    return plan


def earn_points_in_sale(conn, sale_id, customer_id, paid_with_money_paise, user_id) -> int:
    """Points for the part of the bill paid with real money. Returns the points given (0 when off)."""
    cfg = loyalty_settings(conn)
    if not cfg["enabled"] or customer_id is None or paid_with_money_paise <= 0:
        return 0
    points = (paid_with_money_paise // 10000) * cfg["per_100"]
    if points > 0:
        conn.execute("INSERT INTO loyalty_ledger(customer_id, points, tx_type, doc_type, doc_id, note, user_id) VALUES (?,?,?,?,?,?,?)",
                     (customer_id, points, "EARNED_SALE", "sale", sale_id, "earned on a bill", user_id))
    return points


def claw_back_points(conn, sale_id, return_id, returned_value_paise, sale_total_paise, user_id) -> int:
    """Take back the points earned on goods that came back (in the same share as the bill value returned)."""
    if sale_total_paise <= 0:
        return 0
    row = conn.execute("SELECT customer_id, COALESCE(SUM(points),0) AS earned FROM loyalty_ledger "
                       "WHERE doc_type = 'sale' AND doc_id = ? AND tx_type = 'EARNED_SALE'", (sale_id,)).fetchone()
    if not row or row["customer_id"] is None or row["earned"] <= 0:
        return 0
    taken = conn.execute("SELECT COALESCE(-SUM(points),0) FROM loyalty_ledger WHERE doc_type = 'sale_return' AND note = ?",
                         (f"sale {sale_id}",)).fetchone()[0]
    due = min(row["earned"], (row["earned"] * min(returned_value_paise, sale_total_paise)) // sale_total_paise)
    due = min(due, row["earned"] - taken)
    if due <= 0:
        return 0
    conn.execute("INSERT INTO loyalty_ledger(customer_id, points, tx_type, doc_type, doc_id, note, user_id) VALUES (?,?,?,?,?,?,?)",
                 (row["customer_id"], -due, "RETURN_REVERSAL", "sale_return", return_id, f"sale {sale_id}", user_id))
    return due
