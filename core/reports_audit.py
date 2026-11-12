"""Audit report (SRS section 8, Audit group; USR-02).

Shows what was done, by whom and when, from the tamper-evident audit log:
price changes, stock adjustments, discounts over the cap, reprinted and returned bills, logins,
and everything else the app records. It only READS the log; nothing here can change it.

* Reading needs "view_reports".
* Cost figures inside an entry (for example a changed purchase cost) are hidden from users
  without "view_cost", the same rule as every other report.
* Searching looks only at the action, the record type and the user's name, never inside the
  details, so a hidden figure cannot be found by guessing it.
* `check_audit_chain` re-checks the hash chain and says where it was broken, if anywhere.
"""

import json

from core.audit import verify_chain
from core.permissions import has_permission
from core.report_common import check_range, require_view

# The five groups the SRS asks for come first.
SRS_CATEGORIES = ("prices", "stock_adjustments", "discounts", "bills", "logins")
CATEGORY_LABELS = {
    "prices": "Price changes",
    "stock_adjustments": "Stock adjustments and counts",
    "discounts": "Discounts and approvals",
    "bills": "Reprinted bills and returns",
    "logins": "Logins and security",
    "users": "Users and permissions",
    "money": "Cash, expenses and payments",
    "settings": "Settings and tax rules",
    "data": "Backups, exports and customer data",
    "records": "Products, customers, suppliers and documents",
    "other": "Other",
}
CATEGORIES = tuple(CATEGORY_LABELS)

_ACTIONS = {
    "prices": {"price_changed"},
    "stock_adjustments": {"stock_count_started", "stock_count_applied", "stock_count_cancelled"},
    "discounts": {"discount_override", "discount_cap_changed", "credit_limit_override"},
    "bills": {"sale_reprint", "sale_return_created", "labels_printed", "estimate_created", "estimate_converted",
              "estimate_cancelled",
              "alteration_created", "alteration_updated", "alteration_status_changed"},
    "logins": {
        "login", "login_failed", "login_blocked", "account_unlocked", "owner_pin_approved",
        "owner_pin_failed", "password_changed", "pin_changed", "recovery_key_used",
    },
    "users": {"owner_created", "user_created", "permission_changed", "permission_reset"},
    "money": {
        "cash_day_opened", "cash_day_closed", "cash_movement", "expense_added", "expense_corrected",
        "expense_reversed", "supplier_payment", "supplier_payment_reversed", "supplier_opening_balance_set",
        "customer_payment_received", "customer_receipt_reversed", "tally_exported", "commission_rate_changed", "fin_year_locked", "fin_year_unlocked", "voucher_issued", "voucher_cancelled",
        "loyalty_adjusted", "store_credit_adjusted", "credit_terms_changed", "due_date_changed",
    },
    "settings": {"settings_changed", "shop_profile_changed", "series_prefix_changed", "tax_rule_added", "tax_rule_ended"},
    "data": {
        "backup_created", "backup_failed", "backup_restored", "backups_pruned", "report_exported",
        "customer_data_exported", "customer_erased", "customer_consent_recorded", "customer_consent_withdrawn",
    },
    "records": {
        "product_created", "fabric_min_cut_changed", "fabric_piece_received", "product_updated", "product_activated", "variant_updated", "variant_activated",
        "variants_added", "products_imported", "product_image_added", "product_image_removed", "customer_created", "customer_updated", "customer_reactivated", "supplier_created",
        "supplier_updated", "sale_created", "purchase_confirmed", "purchase_draft_saved",
        "purchase_draft_replaced", "purchase_draft_cancelled", "purchase_return_created",
    },
}
_ACTION_CATEGORY = {action: cat for cat, actions in _ACTIONS.items() for action in actions}
_STOCK_PREFIX = "stock_"  # stock_damaged, stock_missing, stock_returned_ok, ... (one per adjustment kind)
MAX_LIMIT = 5000


def category_of(action: str) -> str:
    """Which report group an audit action belongs to ('other' when it is not known yet)."""
    if action in _ACTION_CATEGORY:
        return _ACTION_CATEGORY[action]
    if (action or "").startswith(_STOCK_PREFIX):
        return "stock_adjustments"
    return "other"


def _label(action: str) -> str:
    return (action or "").replace("_", " ").capitalize()


def _category_where(category):
    """SQL condition (and arguments) selecting one category's actions."""
    if category in (None, ""):
        return "", []
    if category not in CATEGORIES:
        raise ValueError(f"Category must be one of: {', '.join(CATEGORIES)}.")
    known = sorted(_ACTIONS.get(category, ()))
    marks = ",".join("?" for _ in known)
    stock_like = "a.action LIKE 'stock\\_%' ESCAPE '\\'"
    if category == "other":
        every = sorted(_ACTION_CATEGORY)
        return (f" AND a.action NOT IN ({','.join('?' for _ in every)}) AND NOT ({stock_like})", every)
    condition = f"a.action IN ({marks})" if known else "0"
    if category == "stock_adjustments":
        condition = f"({condition} OR {stock_like})"
    return f" AND {condition}", known


def _load(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _hide_costs(value):
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if "cost" not in str(k).lower()}
    return value


def _detail(before, after) -> str:
    """One readable line, for example 'default_price_paise: 105000 -> 110000'."""
    if not isinstance(after, dict):
        return ""
    parts = []
    for key, new in after.items():
        if isinstance(before, dict) and key in before:
            parts.append(f"{key}: {before[key]} -> {new}")
        else:
            parts.append(f"{key}: {new}")
    return "; ".join(parts)


# ---------- the report ----------

def audit_report(
    conn, date_from, date_to, *, user_id, category=None, action=None, by_user=None, search=None, limit=500,
) -> dict:
    """Audit log entries in a period, newest first, with counts by group and by user.

    category: one of CATEGORIES (None = all). action: one exact action name. by_user: a user id.
    `total` counts every matching entry; `rows` holds at most `limit` of them (newest first).
    """
    require_view(conn, user_id)
    start, end = check_range(date_from, date_to)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"Limit must be a whole number from 1 to {MAX_LIMIT}.")
    can_see_cost = has_permission(conn, user_id, "view_cost")

    base = " FROM audit_log a LEFT JOIN users u ON u.id = a.user_id WHERE substr(a.created_at, 1, 10) BETWEEN ? AND ?"
    args = [start, end]
    if by_user is not None:
        base += " AND a.user_id = ?"
        args.append(by_user)
    if action:
        base += " AND a.action = ?"
        args.append(action.strip())
    if search and search.strip():
        like = f"%{search.strip()}%"
        base += " AND (a.action LIKE ? OR a.entity LIKE ? OR COALESCE(u.full_name, u.username) LIKE ?)"
        args += [like, like, like]

    # counts use the period and the user/action/search filters but not the category, so the
    # group totals always show what else happened
    by_category = dict.fromkeys(CATEGORIES, 0)
    users = {}
    for r in conn.execute(
        "SELECT a.action AS action, a.user_id AS uid, COALESCE(u.full_name, u.username) AS name, COUNT(*) AS n"
        + base + " GROUP BY a.action, a.user_id", args,
    ):
        by_category[category_of(r["action"])] += r["n"]
        entry = users.setdefault(r["uid"], {"user_id": r["uid"], "user_name": r["name"] or "(system)", "count": 0})
        entry["count"] += r["n"]

    cat_sql, cat_args = _category_where(category)
    total = conn.execute("SELECT COUNT(*)" + base + cat_sql, args + cat_args).fetchone()[0]
    rows = []
    for r in conn.execute(
        "SELECT a.id, a.created_at, a.user_id, COALESCE(u.full_name, u.username) AS user_name, a.action, "
        "a.entity, a.entity_id, a.before_json, a.after_json" + base + cat_sql + " ORDER BY a.id DESC LIMIT ?",
        args + cat_args + [limit],
    ):
        before, after = _load(r["before_json"]), _load(r["after_json"])
        if not can_see_cost:
            before, after = _hide_costs(before), _hide_costs(after)
        rows.append({
            "id": r["id"], "when": r["created_at"], "user_id": r["user_id"],
            "user_name": r["user_name"] or "(system)", "action": r["action"], "label": _label(r["action"]),
            "category": category_of(r["action"]), "entity": r["entity"], "entity_id": r["entity_id"],
            "before": before, "after": after, "detail": _detail(before, after),
        })
    return {
        "date_from": start, "date_to": end, "category": category or None, "rows": rows, "shown": len(rows),
        "total": total, "by_category": by_category, "category_labels": dict(CATEGORY_LABELS),
        "by_user": sorted(users.values(), key=lambda u: (-u["count"], u["user_name"].lower())),
    }


def check_audit_chain(conn, *, user_id) -> dict:
    """Re-check the whole audit log. ok=False means an entry was changed or removed; first_bad_id says where."""
    require_view(conn, user_id)
    ok, bad_id = verify_chain(conn)
    return {
        "ok": ok, "first_bad_id": bad_id,
        "entries": conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
    }
