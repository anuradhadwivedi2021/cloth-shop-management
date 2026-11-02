"""Shared helpers for reports: report periods and the permission check.

Every report takes an inclusive date range (YYYY-MM-DD to YYYY-MM-DD). `resolve_period`
turns the usual choices (today, yesterday, this week, this month, this financial year,
custom) into that range.
"""

from datetime import date, timedelta

from core.permissions import require_permission

PERIODS = ("today", "yesterday", "this_week", "this_month", "this_fin_year", "custom")


def iso_date(value, label="Date") -> str:
    text = value.isoformat() if isinstance(value, date) else str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(f"{label} must be a valid date (YYYY-MM-DD).") from None


def check_range(date_from, date_to):
    """Validate and return (from, to) as ISO strings."""
    start, end = iso_date(date_from, "From date"), iso_date(date_to, "To date")
    if start > end:
        raise ValueError("The From date cannot be after the To date.")
    return start, end


def resolve_period(kind, *, today=None, date_from=None, date_to=None):
    """Return (from, to) for a named period. Weeks start on Monday; the financial year starts on 1 April."""
    today = date.fromisoformat(iso_date(today or date.today(), "Today"))
    if kind == "today":
        start = end = today
    elif kind == "yesterday":
        start = end = today - timedelta(days=1)
    elif kind == "this_week":
        start, end = today - timedelta(days=today.weekday()), today
    elif kind == "this_month":
        start, end = today.replace(day=1), today
    elif kind == "this_fin_year":
        start, end = date(today.year if today.month >= 4 else today.year - 1, 4, 1), today
    elif kind == "custom":
        return check_range(date_from, date_to)
    else:
        raise ValueError(f"Period must be one of: {', '.join(PERIODS)}.")
    return start.isoformat(), end.isoformat()


def require_view(conn, user_id):
    return require_permission(conn, user_id, "view_reports", "view reports")
