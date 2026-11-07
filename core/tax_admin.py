"""The GST rate table: see it, add a rate rule, end a rate rule.

Rules for changing rates (so an old bill can always be explained):
  * A rule's rate, price limit and start date are NEVER edited. To change a rate, end the old rule
    on a date and add a new rule that starts after it. Rules are never deleted.
  * Two rules for the same HSN prefix and the same price limit may not cover the same day.
  * A rate cannot start on a day that already has bills for those HSN codes, and a rule cannot be ended
    before a day it was already used on a bill. Bills keep the figures they were saved with.
Every change is written to the audit log.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from core.audit import log_event
from core.permissions import require_permission
from db.connection import transaction

_HSN = re.compile(r"\d{2,8}")


def _day(value, label) -> str:
    try:
        return (value if isinstance(value, date) else date.fromisoformat(str(value).strip())).isoformat()
    except ValueError:
        raise ValueError(f"{label} must be a valid date.") from None


def _rate(value) -> float:
    try:
        rate = Decimal(str(value).strip().rstrip("%"))
    except InvalidOperation:
        raise ValueError("The GST rate must be a number, for example 5 or 18.") from None
    if not Decimal(0) <= rate <= Decimal(100):
        raise ValueError("The GST rate must be between 0 and 100.")
    return float(rate)


def list_tax_rules(conn, on=None) -> list:
    """All rules, newest first, with a status (active / future / ended) and whether bills used the rule."""
    today = _day(on or date.today(), "Date")
    rules = []
    for r in conn.execute(
            "SELECT t.*, EXISTS(SELECT 1 FROM sale_lines WHERE tax_rule_id = t.id) AS used_on_bills "
            "FROM tax_rules t ORDER BY t.hsn_prefix, t.price_ceiling_paise IS NULL, t.price_ceiling_paise, t.valid_from"):
        rule = dict(r)
        rule["status"] = ("future" if rule["valid_from"] > today
                          else "ended" if rule["valid_to"] and rule["valid_to"] < today else "active")
        rule["used_on_bills"] = bool(rule["used_on_bills"])
        rules.append(rule)
    return rules


def add_tax_rule(conn, user_id, hsn_prefix, rate_percent, price_ceiling_paise, valid_from, note=None) -> int:
    """Add a rate rule that starts on valid_from. price_ceiling_paise None = no price limit. Returns its id."""
    prefix = str(hsn_prefix or "").strip()
    if not _HSN.fullmatch(prefix):
        raise ValueError("The HSN prefix must be 2 to 8 digits, for example 62.")
    rate = _rate(rate_percent)
    if price_ceiling_paise is not None and (isinstance(price_ceiling_paise, bool)
                                            or not isinstance(price_ceiling_paise, int) or price_ceiling_paise <= 0):
        raise ValueError("The price limit must be more than zero, or empty for no limit.")
    start = _day(valid_from, "Start date")
    note = (note or "").strip() or None
    if note and len(note) > 200:
        raise ValueError("The note is too long (at most 200 characters).")

    with transaction(conn):
        require_permission(conn, user_id, "manage_settings", "change GST rates")
        open_rule = conn.execute(
            "SELECT id FROM tax_rules WHERE hsn_prefix = ? AND price_ceiling_paise IS ? "
            "AND (valid_to IS NULL OR valid_to >= ?) LIMIT 1", (prefix, price_ceiling_paise, start)).fetchone()
        if open_rule is not None:
            raise ValueError("A rule for these HSN codes and this price limit already covers that date. "
                             "End it first, then add the new rate from the next day.")
        billed = conn.execute(
            "SELECT 1 FROM sale_lines sl JOIN sales s ON s.id = sl.sale_id "
            "WHERE sl.hsn LIKE ? AND substr(s.created_at, 1, 10) >= ? LIMIT 1", (prefix + "%", start)).fetchone()
        if billed is not None:
            raise ValueError("Bills for these HSN codes already exist from that date, so a rate cannot start then. "
                             "Choose a later date.")
        rule_id = conn.execute(
            "INSERT INTO tax_rules(hsn_prefix, rate_percent, price_ceiling_paise, valid_from, note) VALUES (?,?,?,?,?)",
            (prefix, rate, price_ceiling_paise, start, note)).lastrowid
        log_event(conn, "tax_rule_added", user_id=user_id, entity="tax_rule", entity_id=rule_id,
                  after={"hsn_prefix": prefix, "rate_percent": rate, "price_ceiling_paise": price_ceiling_paise,
                         "valid_from": start})
        return rule_id


def end_tax_rule(conn, user_id, rule_id, valid_to) -> None:
    """Stop a rule after the day valid_to (the rule still applies on that day)."""
    end = _day(valid_to, "End date")
    with transaction(conn):
        require_permission(conn, user_id, "manage_settings", "change GST rates")
        rule = conn.execute("SELECT * FROM tax_rules WHERE id = ?", (rule_id,)).fetchone()
        if rule is None:
            raise ValueError("Tax rule not found.")
        if rule["valid_to"]:
            raise ValueError("This rule has already ended.")
        if end < rule["valid_from"]:
            raise ValueError("The end date cannot be before the rule starts.")
        used_later = conn.execute(
            "SELECT 1 FROM sale_lines sl JOIN sales s ON s.id = sl.sale_id "
            "WHERE sl.tax_rule_id = ? AND substr(s.created_at, 1, 10) > ? LIMIT 1", (rule_id, end)).fetchone()
        if used_later is not None:
            raise ValueError("Bills were already made with this rule after that date. Choose a later end date.")
        conn.execute("UPDATE tax_rules SET valid_to = ? WHERE id = ?", (end, rule_id))
        log_event(conn, "tax_rule_ended", user_id=user_id, entity="tax_rule", entity_id=rule_id,
                  before={"valid_to": None}, after={"valid_to": end})
