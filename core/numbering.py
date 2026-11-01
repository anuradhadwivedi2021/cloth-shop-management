"""Financial year and never-reused document numbers.

Numbers must be generated inside the SAME transaction that saves the document,
so a crash can never leave a gap or a duplicate.
"""

from datetime import date


def financial_year(on) -> str:
    """Indian financial year label, e.g. '26-27' for 1 Apr 2026 to 31 Mar 2027.

    Accepts a date or a 'YYYY-MM-DD' string.
    """
    if isinstance(on, str):
        on = date.fromisoformat(on)
    start = on.year if on.month >= 4 else on.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def next_number(conn, series: str, fin_year: str, prefix: str):
    """Take the next number of a series. Returns (number, formatted), e.g. (123, 'INV/26-27/000123').

    Must be called inside `with transaction(conn):`. If that transaction is
    rolled back, the number is released too, so there are no gaps.
    """
    if not conn.in_transaction:
        raise RuntimeError("next_number must be called inside a transaction.")
    conn.execute(
        "INSERT OR IGNORE INTO number_series(series, fin_year, prefix, last_number) VALUES (?, ?, ?, 0)",
        (series, fin_year, prefix),
    )
    conn.execute(
        "UPDATE number_series SET last_number = last_number + 1 WHERE series = ? AND fin_year = ?",
        (series, fin_year),
    )
    number = conn.execute(
        "SELECT last_number FROM number_series WHERE series = ? AND fin_year = ?",
        (series, fin_year),
    ).fetchone()[0]
    return number, f"{prefix}/{fin_year}/{number:06d}"