"""Shop profile, GST place of supply, and the owner's preferences.

Three parts:

1. SHOP PROFILE (name, address, phone, GSTIN, state, tax scheme, UPI id)
   Kept as a history. A change adds a new version; old versions are never edited, so an old
   bill can be reprinted with the details valid on its date:  get_shop_profile(conn, on_date).

2. PLACE OF SUPPLY
   place_of_supply() tells billing whether a bill is CGST + SGST (customer in the shop's
   state) or IGST (customer in another state). Pass the result as intra_state to create_sale.
   billing_problems() lists what is missing before a GST bill can be issued.

3. PREFERENCES (credit sales, return policy, auto-lock, languages, bill layout ...)
   Every preference has a type, a default and limits. A change is checked as a whole (all or
   nothing) and written to the audit log with before and after values.

Changing a setting never rewrites an old bill, price or tax figure: bills keep their own saved
figures, and the profile is versioned.
"""

import re
from datetime import date, datetime

from core.audit import log_event
from core.backup import DEFAULT_REMINDER_DAYS
from core.customers import DEFAULT_CONSENT_PURPOSE, DEFAULT_RETENTION_MONTHS, validate_gstin
from core.permissions import require_permission
from core.session import DEFAULT_AUTO_LOCK_MINUTES
from db.connection import transaction

_TIME = "%Y-%m-%d %H:%M:%S"

STATE_NAMES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}

TAX_SCHEMES = ("regular", "composition", "unregistered")
PROFILE_FIELDS = ("name", "address", "phone", "email", "gstin", "state_code", "tax_scheme", "upi_id")
_UPI = re.compile(r"^[A-Za-z0-9._\-]{2,}@[A-Za-z][A-Za-z0-9]{1,}$")


# =============================================================== 1. shop profile

def _text(value):
    value = (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()
    return value or None


def _empty_profile():
    profile = {f: None for f in PROFILE_FIELDS}
    profile.update(tax_scheme="regular", valid_from=None, state_name=None, is_set_up=False)
    return profile


def _profile_from_row(row):
    profile = {f: row[f] for f in PROFILE_FIELDS}
    profile.update(valid_from=row["valid_from"], state_name=STATE_NAMES.get(row["state_code"]), is_set_up=True)
    return profile


def get_shop_profile(conn, on_date=None) -> dict:
    """The shop details valid on a date (default today). Use the bill's date to reprint an old bill.

    Before the shop is set up, every field is None and 'is_set_up' is False.
    """
    on_date = (on_date or date.today()).isoformat() if not isinstance(on_date, str) else on_date
    row = conn.execute(
        "SELECT * FROM shop_profile_versions WHERE substr(valid_from, 1, 10) <= ? ORDER BY id DESC LIMIT 1",
        (on_date,),
    ).fetchone()
    if row is None:  # a bill older than the first setup uses the first version
        row = conn.execute("SELECT * FROM shop_profile_versions ORDER BY id LIMIT 1").fetchone()
    return _profile_from_row(row) if row else _empty_profile()


def update_shop_profile(conn, user_id, *, now=None, **fields) -> list:
    """Set or change the shop profile. Returns the names of the fields that actually changed.

    Fields: name, address, phone, email, gstin, state_code, tax_scheme, upi_id.
    The state is taken from the first two digits of the GSTIN. A shop without a GSTIN
    (tax_scheme 'unregistered') may give a state_code on its own.
    """
    unknown = set(fields) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown shop detail: {', '.join(sorted(unknown))}.")
    if not fields:
        return []
    now = now or datetime.now()

    with transaction(conn):
        require_permission(conn, user_id, "manage_settings", "change shop details")
        current = get_shop_profile(conn)
        merged = {f: current[f] for f in PROFILE_FIELDS}
        for key, value in fields.items():
            merged[key] = _text(value)
        if current["is_set_up"] is False:
            merged["tax_scheme"] = fields.get("tax_scheme") or "regular"

        if not merged["name"]:
            raise ValueError("Shop name is required.")
        if merged["tax_scheme"] not in TAX_SCHEMES:
            raise ValueError(f"Tax scheme must be one of {', '.join(TAX_SCHEMES)}.")
        if merged["gstin"]:
            merged["gstin"] = validate_gstin(merged["gstin"])
            derived = merged["gstin"][:2]
            if "state_code" in fields and _text(fields["state_code"]) not in (None, derived):
                raise ValueError("The state does not match the first two digits of the GSTIN.")
            merged["state_code"] = derived
        elif merged["tax_scheme"] in ("regular", "composition"):
            raise ValueError("A GST-registered shop needs its GSTIN.")
        if merged["state_code"] and merged["state_code"] not in STATE_NAMES:
            raise ValueError("State code is not valid.")
        if merged["phone"] and not 7 <= len(re.sub(r"\D", "", merged["phone"])) <= 15:
            raise ValueError("Phone number looks wrong.")
        if merged["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", merged["email"]):
            raise ValueError("Email address looks wrong.")
        if merged["upi_id"] and not _UPI.match(merged["upi_id"]):
            raise ValueError("UPI id looks wrong. It looks like name@bank.")

        diff = {k: merged[k] for k in PROFILE_FIELDS if merged[k] != current[k]}
        if not diff:
            return []
        conn.execute(
            "INSERT INTO shop_profile_versions(valid_from, name, address, phone, email, gstin, state_code, "
            "tax_scheme, upi_id, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now.strftime(_TIME), *[merged[f] for f in PROFILE_FIELDS], user_id),
        )
        log_event(conn, "shop_profile_changed", user_id=user_id, entity="shop_profile",
                  before={k: current[k] for k in diff}, after=diff)
        return sorted(diff)


def billing_problems(conn, mode, on_date=None) -> list:
    """What must be fixed before a bill of this kind can be issued. Empty list = ready.

    mode is the bill type used by create_sale: 'regular' (GST tax invoice),
    'composition' (bill of supply) or 'unregistered' (simple bill without GST).
    A shop may always issue a simple non-GST bill.
    """
    profile = get_shop_profile(conn, on_date)
    if not profile["is_set_up"]:
        return ["Shop details are not set up yet."]
    problems = []
    if mode in ("regular", "composition"):
        if not profile["gstin"]:
            problems.append("The shop GSTIN is not set.")
        if profile["tax_scheme"] != mode:
            label = {"regular": "regular GST", "composition": "composition scheme", "unregistered": "unregistered"}
            problems.append(
                f"This shop is set as {label[profile['tax_scheme']]}, so it cannot issue a "
                f"{'tax invoice' if mode == 'regular' else 'bill of supply'}."
            )
    return problems


# =============================================================== 2. place of supply

def place_of_supply(conn, *, customer_gstin=None, customer_state_code=None, on_date=None) -> dict:
    """Decide CGST + SGST or IGST for a bill. Use the result's 'intra_state' in create_sale.

    - customer with a GSTIN: the state is the first two digits of it
    - customer without GSTIN but a chosen state: that state
    - walk-in customer with no details: treated as the shop's own state (CGST + SGST)
    If the shop's state is not set, CGST + SGST is assumed and 'warning' explains why.
    """
    shop_state = get_shop_profile(conn, on_date)["state_code"]
    if customer_gstin:
        customer_state, source = validate_gstin(customer_gstin)[:2], "customer GSTIN"
    elif customer_state_code:
        customer_state = _text(customer_state_code)
        if customer_state not in STATE_NAMES:
            raise ValueError("State code is not valid.")
        source = "state chosen for the bill"
    else:
        customer_state, source = None, "walk-in customer (shop's state)"

    result = {
        "shop_state_code": shop_state, "customer_state_code": customer_state, "source": source, "warning": None,
    }
    if not shop_state:
        result.update(intra_state=True, label="CGST + SGST",
                      warning="The shop's state is not set, so CGST + SGST was assumed.")
        return result
    intra = customer_state is None or customer_state == shop_state
    result.update(intra_state=intra, label="CGST + SGST" if intra else "IGST")
    return result


# =============================================================== 3. preferences

def _bool(spec, value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if str(value).strip().lower() in ("1", "true", "yes", "on"):
        return "1"
    if str(value).strip().lower() in ("0", "false", "no", "off"):
        return "0"
    raise ValueError(f"{spec['label']}: choose on or off.")


def _int(spec, value):
    if value is None or (isinstance(value, str) and not value.strip()):
        if spec.get("optional"):
            return ""
        raise ValueError(f"{spec['label']}: a number is required.")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{spec['label']}: must be a whole number.")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ValueError(f"{spec['label']}: must be a whole number.") from None
    if not spec["min"] <= number <= spec["max"]:
        raise ValueError(f"{spec['label']}: must be between {spec['min']} and {spec['max']}.")
    return str(number)


def _choice(spec, value):
    value = str(value).strip().lower() if value is not None else ""
    if value not in spec["choices"]:
        raise ValueError(f"{spec['label']}: choose one of {', '.join(spec['choices'])}.")
    return value


def _text_pref(spec, value):
    value = (str(value) if value is not None else "").strip()
    if len(value) > spec.get("max", 300):
        raise ValueError(f"{spec['label']}: too long (at most {spec.get('max', 300)} characters).")
    return value


def _csv(spec, value):
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    return ", ".join(dict.fromkeys(parts))


_PARSERS = {"bool": _bool, "int": _int, "choice": _choice, "text": _text_pref, "csv": _csv}

SPEC = {
    # --- selling
    "credit_sales_enabled": dict(kind="bool", default="0", group="Selling", label="Allow credit sales (customer pays later)"),
    "allow_negative_stock": dict(kind="bool", default="0", group="Selling", label="Allow selling more than the stock count"),
    "default_bill_mode": dict(kind="choice", default="regular", choices=("regular", "composition", "unregistered"),
                              group="Selling", label="Bill type shown first at the counter"),
    # --- returns
    "return_window_days": dict(kind="int", default="", optional=True, min=0, max=365, group="Returns",
                               label="Days allowed for returns (empty = no limit)"),
    "final_sale_categories": dict(kind="csv", default="", group="Returns",
                                  label="Categories that cannot be returned (comma separated)"),
    # --- bill
    "bill_language": dict(kind="choice", default="en", choices=("en", "hi", "mr"), group="Bill", label="Language on bills"),
    "bill_paper": dict(kind="choice", default="thermal80", choices=("a4", "a5", "thermal58", "thermal80"),
                       group="Bill", label="Bill paper"),
    "bill_footer": dict(kind="text", default="", max=300, group="Bill", label="Text at the bottom of the bill"),
    "show_return_policy_on_bill": dict(kind="bool", default="1", group="Bill", label="Print the return policy on the bill"),
    # --- screens and security
    "ui_language": dict(kind="choice", default="en", choices=("en", "hi", "mr"), group="Screens", label="Language of the screens"),
    "auto_lock_minutes": dict(kind="int", default=str(DEFAULT_AUTO_LOCK_MINUTES), min=1, max=240, group="Security",
                              label="Lock the app after this many idle minutes"),
    # --- backup and customer data
    "backup_reminder_days": dict(kind="int", default=str(DEFAULT_REMINDER_DAYS), min=1, max=90, group="Backup",
                                 label="Remind me to back up after this many days"),
    "customer_retention_months": dict(kind="int", default=str(DEFAULT_RETENTION_MONTHS), min=1, max=120, group="Customer data",
                                      label="Review customers inactive for this many months"),
    "customer_consent_purpose": dict(kind="text", default=DEFAULT_CONSENT_PURPOSE, max=300, group="Customer data",
                                     label="Privacy notice shown when saving a customer"),
}


def _typed(spec, stored):
    if spec["kind"] == "bool":
        return stored == "1"
    if spec["kind"] == "int":
        return int(stored) if stored != "" else None
    return stored


def get_preferences(conn) -> dict:
    """Every preference with its current value, default, label and group (for the Settings screen)."""
    rows = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings")}
    out = {}
    for key, spec in SPEC.items():
        stored = rows.get(key, spec["default"])
        out[key] = {
            "value": _typed(spec, stored), "default": _typed(spec, spec["default"]),
            "label": spec["label"], "group": spec["group"], "kind": spec["kind"],
            "choices": spec.get("choices"), "is_default": key not in rows or rows[key] == spec["default"],
        }
    return out


def update_preferences(conn, user_id, changes: dict) -> list:
    """Change one or more preferences. All or nothing. Returns the keys that actually changed."""
    if not changes:
        return []
    unknown = set(changes) - set(SPEC)
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(sorted(unknown))}.")
    new = {key: _PARSERS[SPEC[key]["kind"]](SPEC[key], value) for key, value in changes.items()}

    with transaction(conn):
        require_permission(conn, user_id, "manage_settings", "change settings")
        rows = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings")}
        before_all = {k: rows.get(k, SPEC[k]["default"]) for k in new}
        diff = {k: v for k, v in new.items() if v != before_all[k]}
        for key, value in diff.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        if diff:
            log_event(conn, "settings_changed", user_id=user_id, entity="settings",
                      before={k: before_all[k] for k in diff}, after=diff)
        return sorted(diff)
