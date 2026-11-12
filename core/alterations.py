"""ALT-01 / ALT-02: alteration and tailoring job orders.

  create_job(conn, user_id, item=, work=, charge_paise=, promised_date=, ...)   take a garment in
  update_job(conn, user_id, job_id, **changes)      fix details while the job is open
  set_status(conn, user_id, job_id, status)         received -> in_progress -> ready -> delivered (or cancelled)
  list_jobs(conn, user_id, status=None)             all jobs, newest first
  ready_for_pickup(conn, user_id)                   'ready' jobs: customers to call
  overdue(conn, user_id, today=)                    open jobs past the promised date, oldest first

A job register only: the advance and the balance are written down on the job (and its slip). They are not a
sale, do not enter the cash drawer and do not appear in tax reports. Jobs are never deleted.
"""

from datetime import date, datetime

from core.audit import log_event
from core.numbering import financial_year, next_number
from core.permissions import require_permission
from db.connection import transaction

STATUSES = ("received", "in_progress", "ready", "delivered", "cancelled")
OPEN = ("received", "in_progress", "ready")
_NEXT = {"received": ("in_progress", "ready", "cancelled"), "in_progress": ("ready", "cancelled"),
         "ready": ("delivered", "in_progress", "cancelled")}
_EDITABLE = ("customer_id", "customer_name", "customer_phone", "item", "work", "measurements", "charge_paise",
             "advance_paise", "promised_date")
MAX_TEXT = 300
MAX_MEASUREMENTS = 1000


def _text(value, what, *, required=False, limit=MAX_TEXT):
    value = " ".join(str(value or "").split())
    if required and not value:
        raise ValueError(f"{what} is required.")
    if len(value) > limit:
        raise ValueError(f"{what} can have at most {limit} characters.")
    return value or None


def _money(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be an amount of 0 or more.")
    return value


def _date(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError("The promised date is not a real date.") from None


def _clean(changes: dict) -> dict:
    out = {}
    for key, value in changes.items():
        if key in ("item", "work"):
            out[key] = _text(value, "The item" if key == "item" else "The work to do", required=True)
        elif key in ("customer_name", "customer_phone"):
            out[key] = _text(value, "The customer detail", limit=100)
        elif key == "measurements":
            out[key] = " ".join(str(value or "").split())[:MAX_MEASUREMENTS] or None
        elif key in ("charge_paise", "advance_paise"):
            out[key] = _money(value, "The charge" if key == "charge_paise" else "The advance")
        elif key == "promised_date":
            out[key] = _date(value)
        elif key == "customer_id":
            out[key] = value
        else:
            raise ValueError(f"'{key}' cannot be changed here.")
    return out


def _check_customer(conn, customer_id):
    if customer_id is not None and not conn.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone():
        raise ValueError("The customer was not found.")


def create_job(conn, user_id, *, item, work, charge_paise, promised_date, advance_paise=0, measurements=None,
               customer_id=None, customer_name=None, customer_phone=None, today=None) -> int:
    require_permission(conn, user_id, "sell", "take an alteration job")
    f = _clean({"item": item, "work": work, "charge_paise": charge_paise, "advance_paise": advance_paise,
                "promised_date": promised_date, "measurements": measurements, "customer_id": customer_id,
                "customer_name": customer_name, "customer_phone": customer_phone})
    if f["advance_paise"] > f["charge_paise"]:
        raise ValueError("The advance cannot be more than the charge.")
    on = today or date.today()
    if f["promised_date"] < on.isoformat():
        raise ValueError("The promised date cannot be in the past.")
    with transaction(conn):
        _check_customer(conn, f["customer_id"])
        number, job_no = next_number(conn, "ALT", financial_year(on), "ALT")
        cur = conn.execute(
            "INSERT INTO alteration_jobs(number, job_no, fin_year, customer_id, customer_name, customer_phone, item, work, "
            "measurements, charge_paise, advance_paise, promised_date, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (number, job_no, financial_year(on), f["customer_id"], f["customer_name"], f["customer_phone"], f["item"],
             f["work"], f["measurements"], f["charge_paise"], f["advance_paise"], f["promised_date"], user_id))
        log_event(conn, "alteration_created", user_id=user_id, entity="alteration_job", entity_id=cur.lastrowid,
                  after={"job_no": job_no, "charge_paise": f["charge_paise"], "advance_paise": f["advance_paise"]})
        return cur.lastrowid


def _row(conn, job_id):
    row = conn.execute("SELECT * FROM alteration_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("Alteration job not found.")
    return row


def _shape(row) -> dict:
    job = dict(row)
    job["balance_paise"] = job["charge_paise"] - job["advance_paise"]
    return job


def get_job(conn, user_id, job_id) -> dict:
    require_permission(conn, user_id, "sell", "see alteration jobs")
    return _shape(_row(conn, job_id))


def update_job(conn, user_id, job_id, **changes) -> list:
    """Change details of a job that is still open. Returns the names of the fields that really changed."""
    require_permission(conn, user_id, "sell", "change an alteration job")
    unknown = set(changes) - set(_EDITABLE)
    if unknown:
        raise ValueError("These cannot be changed here: " + ", ".join(sorted(unknown)) + ".")
    cleaned = _clean(changes)
    with transaction(conn):
        row = _row(conn, job_id)
        if row["status"] not in OPEN:
            raise ValueError("A delivered or cancelled job cannot be changed.")
        if "customer_id" in cleaned:
            _check_customer(conn, cleaned["customer_id"])
        diff = {k: v for k, v in cleaned.items() if row[k] != v}
        if not diff:
            return []
        charge = diff.get("charge_paise", row["charge_paise"])
        advance = diff.get("advance_paise", row["advance_paise"])
        if advance > charge:
            raise ValueError("The advance cannot be more than the charge.")
        conn.execute(f"UPDATE alteration_jobs SET {', '.join(f'{k} = ?' for k in diff)}, "
                     "updated_at = strftime('%Y-%m-%d %H:%M:%S','now','localtime') WHERE id = ?", (*diff.values(), job_id))
        log_event(conn, "alteration_updated", user_id=user_id, entity="alteration_job", entity_id=job_id,
                  after={"fields": sorted(diff)})
        return sorted(diff)


def set_status(conn, user_id, job_id, status) -> dict:
    """Move a job on. Delivering takes the balance (charge minus advance): it is written on the job."""
    require_permission(conn, user_id, "sell", "change an alteration job")
    if status not in STATUSES:
        raise ValueError("Unknown status.")
    with transaction(conn):
        row = _row(conn, job_id)
        if status not in _NEXT.get(row["status"], ()):
            raise ValueError(f"A job that is '{row['status']}' cannot be marked '{status}'.")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sets, args = ["status = ?", "updated_at = ?"], [status, now]
        if status == "ready":
            sets.append("ready_at = ?"); args.append(now)
        if status == "delivered":
            sets += ["delivered_at = ?", "balance_paid_paise = ?"]
            args += [now, row["charge_paise"] - row["advance_paise"]]
        conn.execute(f"UPDATE alteration_jobs SET {', '.join(sets)} WHERE id = ?", (*args, job_id))
        log_event(conn, "alteration_status_changed", user_id=user_id, entity="alteration_job", entity_id=job_id,
                  before={"status": row["status"]}, after={"status": status})
        return _shape(_row(conn, job_id))


def list_jobs(conn, user_id, status=None) -> list:
    require_permission(conn, user_id, "sell", "see alteration jobs")
    if status is not None and status not in STATUSES:
        raise ValueError("Unknown status.")
    sql = "SELECT * FROM alteration_jobs"
    rows = conn.execute(sql + (" WHERE status = ?" if status else "") + " ORDER BY id DESC",
                        (status,) if status else ()).fetchall()
    return [_shape(r) for r in rows]


def ready_for_pickup(conn, user_id) -> list:
    """Jobs that are ready: the customers to call, longest-waiting first."""
    require_permission(conn, user_id, "sell", "see alteration jobs")
    return [_shape(r) for r in conn.execute(
        "SELECT * FROM alteration_jobs WHERE status = 'ready' ORDER BY ready_at, id")]


def overdue(conn, user_id, today=None) -> list:
    """Open jobs (not yet ready) whose promised date has passed, with the days late. Oldest promise first."""
    require_permission(conn, user_id, "sell", "see alteration jobs")
    on = today or date.today()
    out = []
    for r in conn.execute("SELECT * FROM alteration_jobs WHERE status IN ('received','in_progress') AND promised_date < ? "
                          "ORDER BY promised_date, id", (on.isoformat(),)):
        job = _shape(r)
        job["days_late"] = (on - date.fromisoformat(r["promised_date"])).days
        out.append(job)
    return out
