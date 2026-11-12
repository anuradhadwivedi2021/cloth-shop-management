"""Document number series: see them and change a prefix.

A number looks like  INV/26-27/000123  =  prefix / financial year / running number.

Rules (so a bill number is never reused and a year's numbers stay consistent):
  * The running number can never be set or lowered. Only the prefix can be changed.
  * A prefix is 1 to 8 capital letters or digits, and two series can never share one
    (otherwise two different documents could get the same number).
  * If the series has no document yet in this financial year, the new prefix is used at once.
    If documents were already issued this year, the new prefix starts with the NEXT financial year,
    so one year's bills never mix two prefixes.
  * Documents already issued are never renamed.
"""

import re
from datetime import date

from core.audit import log_event
from core.numbering import PREFIX_SETTING, financial_year
from core.permissions import require_permission
from core.settings import get_setting
from db.connection import transaction

# series -> what the number is used for. The default prefix of every series is its own name.
KNOWN_SERIES = {
    "INV": "Tax invoice",
    "BOS": "Bill of supply",
    "BILL": "Simple bill (shop not registered)",
    "CN": "Credit note (customer return)",
    "DN": "Debit note (return to supplier)",
    "PUR": "Purchase voucher",
    "SPV": "Supplier payment voucher",
    "RCT": "Customer payment receipt",
    "CNT": "Stock count",
    "EST": "Estimate / quotation",
    "ALT": "Alteration / tailoring job",
}
_PREFIX = re.compile(r"[A-Z0-9]{1,8}")


def effective_prefix(conn, series) -> str:
    """The prefix a NEW financial year's numbers of this series will get."""
    return get_setting(conn, PREFIX_SETTING.format(series)) or series


def _next_year(fin_year: str) -> str:
    start = int(fin_year[:2]) + 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def list_series(conn, on=None) -> list:
    """Every series with its prefix, this year's last number and the number the next document will get."""
    on = on or date.today()
    fin_year = financial_year(on)
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT series, prefix, last_number FROM number_series WHERE fin_year = ?", (fin_year,))}
    result = []
    for series, label in KNOWN_SERIES.items():
        wanted = effective_prefix(conn, series)
        in_use, last = rows.get(series, (wanted, 0))
        result.append({
            "series": series, "label": label, "prefix": wanted, "prefix_in_use": in_use,
            "fin_year": fin_year, "last_number": last,
            "next_number": f"{in_use}/{fin_year}/{last + 1:06d}",
            "starts_next_year": in_use != wanted,
        })
    return result


def set_series_prefix(conn, user_id, series, prefix, *, on=None) -> dict:
    """Change the prefix of one series. Returns {'changed', 'applies_from'} ('now' or 'YY-YY')."""
    series = (series or "").strip().upper()
    if series not in KNOWN_SERIES:
        raise ValueError("Unknown document series.")
    new = (prefix or "").strip().upper()
    if not _PREFIX.fullmatch(new):
        raise ValueError("The prefix must be 1 to 8 letters or digits, for example INV.")
    fin_year = financial_year(on or date.today())

    with transaction(conn):
        require_permission(conn, user_id, "manage_settings", "change document numbering")
        old = effective_prefix(conn, series)
        if new == old:
            return {"changed": False, "applies_from": "now"}
        taken = {effective_prefix(conn, other) for other in KNOWN_SERIES if other != series}
        taken |= {r[0] for r in conn.execute("SELECT DISTINCT prefix FROM number_series WHERE series != ?", (series,))}
        if new in taken:
            raise ValueError(f"Another document series already uses '{new}'. Choose a different prefix.")

        issued = conn.execute(
            "SELECT last_number FROM number_series WHERE series = ? AND fin_year = ?", (series, fin_year)).fetchone()
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (PREFIX_SETTING.format(series), new))
        if issued is None or issued[0] == 0:
            conn.execute("UPDATE number_series SET prefix = ? WHERE series = ? AND fin_year = ? AND last_number = 0",
                         (new, series, fin_year))
            applies_from = "now"
        else:
            applies_from = _next_year(fin_year)
        log_event(conn, "series_prefix_changed", user_id=user_id, entity="number_series", entity_id=None,
                  before={"series": series, "prefix": old},
                  after={"series": series, "prefix": new, "applies_from": applies_from})
        return {"changed": True, "applies_from": applies_from}
