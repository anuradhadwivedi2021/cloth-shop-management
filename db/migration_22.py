# db/migration_22.py
"""Schema migration 22: alteration / tailoring job orders (SRS ALT-01, ALT-02).

alteration_jobs  one job per garment brought for alteration or stitching: who, what, measurements, charge, advance,
                 promised date and status. It is a job register: money taken (advance, balance) is written down here
                 for the customer's slip; it is not a sale and does not enter the cash drawer or the tax reports.
Jobs are never deleted; a job that will not be done is marked 'cancelled'.
"""

MIGRATION_22 = """
CREATE TABLE alteration_jobs (
    id              INTEGER PRIMARY KEY,
    number          INTEGER NOT NULL,
    job_no          TEXT NOT NULL UNIQUE,
    fin_year        TEXT NOT NULL,
    customer_id     INTEGER REFERENCES customers(id),
    customer_name   TEXT,
    customer_phone  TEXT,
    item            TEXT NOT NULL,
    work            TEXT NOT NULL,
    measurements    TEXT,
    charge_paise    INTEGER NOT NULL CHECK (charge_paise >= 0),
    advance_paise   INTEGER NOT NULL DEFAULT 0 CHECK (advance_paise >= 0 AND advance_paise <= charge_paise),
    promised_date   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'received'
                    CHECK (status IN ('received', 'in_progress', 'ready', 'delivered', 'cancelled')),
    balance_paid_paise INTEGER NOT NULL DEFAULT 0 CHECK (balance_paid_paise >= 0),
    ready_at        TEXT,
    delivered_at    TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')),
    updated_at      TEXT
);
CREATE INDEX idx_alteration_status ON alteration_jobs(status, promised_date);
CREATE TRIGGER alteration_jobs_no_delete BEFORE DELETE ON alteration_jobs
BEGIN SELECT RAISE(ABORT, 'an alteration job cannot be deleted; cancel it instead'); END;
"""
