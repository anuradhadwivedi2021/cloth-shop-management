"""Encrypted backup and safe restore.

Two buttons in the app:  "Create Backup"  and  "Restore Backup".

A backup is ONE file (ClothShop_backup_YYYYMMDD_HHMMSS.csbak) holding:
    - a consistent copy of the shop database (taken with SQLite's backup API,
      so it is safe even while the shop is open)
    - a manifest (date, bill count, last bill number, record counts, counters)
    - a copy of the key file (shop.keys), so the backup opens with the SAME
      username/password or recovery key as the shop. That is what lets a
      backup restore on a clean machine.

The database copy + manifest are encrypted again with AES-256-GCM using the
database key. The sha256 of the encrypted part is stored in the header, so a
damaged or half-copied file is detected even before anyone types a password.

Restore never replaces anything until the backup has been fully decrypted,
opened and checked. Before replacing a shop that already exists, a safety
backup of the current data is made. Document number counters are never
lowered, so a bill number is never used twice after a restore.
"""

import hashlib
import json
import os
import shutil
import struct
import tempfile
from datetime import date, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlcipher3 import dbapi2 as sqlcipher

from core import keystore
from core.audit import log_event, verify_chain
from core.settings import get_setting, set_setting
from db.connection import open_db, transaction
from db.schema import MIGRATIONS

MAGIC = b"CSBAK1\n"
EXT = ".csbak"
NAME_PREFIX = "ClothShop_backup_"
FORMAT_VERSION = 1
MIN_FREE_BYTES = 100 * 1024 * 1024  # refuse to write when less than 100 MB would be left
DEFAULT_REMINDER_DAYS = 7
DEFAULT_KEEP = 10
SAFETY_FOLDER = "safety_backups"
_FMT = "%Y-%m-%d %H:%M:%S"

_COUNT_TABLES = (
    "products", "variants", "customers", "suppliers", "purchases",
    "sales", "payments", "stock_movements", "users", "audit_log",
)


class BackupError(Exception):
    """A backup or restore could not be completed. The message is safe to show."""


# ---------------------------------------------------------------- helpers

def _paths(folder):
    folder = Path(folder)
    return folder / "shop.db", folder / "shop.keys"


def _open_raw(path, key_hex):
    conn = sqlcipher.connect(str(path))
    conn.isolation_level = None
    conn.execute(f"PRAGMA key = '{key_hex}'")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    return conn


def _same_volume(a, b) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _stats(db_path, key_hex) -> dict:
    """Record counts and key figures of a database file (read-only)."""
    conn = _open_raw(db_path, key_hex)
    try:
        conn.execute("PRAGMA query_only = ON")
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _COUNT_TABLES}
        last = conn.execute("SELECT invoice_no FROM sales ORDER BY id DESC LIMIT 1").fetchone()
        total = conn.execute("SELECT COALESCE(SUM(total_paise), 0) FROM sales WHERE status = 'completed'").fetchone()[0]
        counters = [
            {"series": r[0], "fin_year": r[1], "prefix": r[2], "last_number": r[3]}
            for r in conn.execute("SELECT series, fin_year, prefix, last_number FROM number_series")
        ]
        version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        return {
            "counts": counts,
            "bill_count": counts["sales"],
            "last_bill_no": last[0] if last else None,
            "sales_total_paise": total,
            "counters": counters,
            "schema_version": version,
        }
    finally:
        conn.close()


def _build_file(path, header: dict, ciphertext: bytes):
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">I", len(head)))
        f.write(head)
        f.write(ciphertext)
        f.flush()
        os.fsync(f.fileno())


def _read_file(path):
    """Return (header dict, ciphertext). Raises BackupError if the file is not an intact backup."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise BackupError("The backup file could not be read.") from None
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 4:
        raise BackupError("This is not a Cloth Shop backup file.")
    pos = len(MAGIC)
    (hlen,) = struct.unpack(">I", data[pos:pos + 4])
    pos += 4
    try:
        header = json.loads(data[pos:pos + hlen].decode("utf-8"))
        if header.get("format") != FORMAT_VERSION:
            raise ValueError
    except ValueError:
        raise BackupError("The backup file is damaged.") from None
    ciphertext = data[pos + hlen:]
    if len(ciphertext) != header.get("cipher_len") or hashlib.sha256(ciphertext).hexdigest() != header.get("sha256"):
        raise BackupError("The backup file is damaged or incomplete.")
    return header, ciphertext


def _aad(header) -> bytes:
    return MAGIC + str(header["created_at"]).encode("utf-8")


def _pack(manifest: dict, db_bytes: bytes) -> bytes:
    m = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(m)) + m + db_bytes


def _unpack(plain: bytes):
    (mlen,) = struct.unpack(">I", plain[:4])
    return json.loads(plain[4:4 + mlen].decode("utf-8")), plain[4 + mlen:]


# ---------------------------------------------------------------- create

def _file_name(dest, stamp: datetime) -> Path:
    base = f"{NAME_PREFIX}{stamp.strftime('%Y%m%d_%H%M%S')}"
    path = Path(dest) / (base + EXT)
    n = 2
    while path.exists():
        path = Path(dest) / f"{base}_{n}{EXT}"
        n += 1
    return path


def _write_backup(conn, db_key_hex, folder, dest, *, now=None, min_free_bytes=MIN_FREE_BYTES) -> dict:
    """Create one backup file. No permission check here; callers do that."""
    now = now or datetime.now()
    db_path, key_path = _paths(folder)
    dest = Path(dest)
    tmp_dir = None
    try:
        dest.mkdir(parents=True, exist_ok=True)
        needed = (db_path.stat().st_size if db_path.exists() else 0) * 2 + min_free_bytes
        if shutil.disk_usage(dest).free < needed:
            raise BackupError("Not enough free space at the backup location.")
        tmp_dir = Path(tempfile.mkdtemp(prefix=".tmp-backup-", dir=dest))

        snap = tmp_dir / "snapshot.db"
        target = _open_raw(snap, db_key_hex)
        try:
            conn.backup(target)  # consistent copy, safe while the shop is open
        finally:
            target.close()
        stats = _stats(snap, db_key_hex)
        manifest = {
            "created_at": now.strftime(_FMT),
            "schema_version": stats["schema_version"],
            "bill_count": stats["bill_count"],
            "last_bill_no": stats["last_bill_no"],
            "sales_total_paise": stats["sales_total_paise"],
            "counts": stats["counts"],
            "counters": stats["counters"],
        }
        plain = _pack(manifest, snap.read_bytes())
        nonce = os.urandom(12)
        header = {
            "format": FORMAT_VERSION,
            "created_at": manifest["created_at"],
            "keystore": keystore._read(key_path),
            "nonce": nonce.hex(),
        }
        ciphertext = AESGCM(bytes.fromhex(db_key_hex)).encrypt(nonce, plain, _aad(header))
        header["cipher_len"] = len(ciphertext)
        header["sha256"] = hashlib.sha256(ciphertext).hexdigest()

        final = _file_name(dest, now)
        tmp_file = tmp_dir / "out.csbak"
        _build_file(tmp_file, header, ciphertext)
        os.replace(tmp_file, final)  # the file appears complete or not at all
        return {
            "status": "success",
            "path": str(final),
            "file_name": final.name,
            "created_at": manifest["created_at"],
            "size_bytes": final.stat().st_size,
            "bill_count": manifest["bill_count"],
            "same_disk_warning": _same_volume(dest, folder),
        }
    except BackupError:
        raise
    except (OSError, sqlcipher.Error):
        raise BackupError("The backup could not be created. Check the backup location and free space.") from None
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def create_backup(conn, session, folder, dest, *, now=None, min_free_bytes=MIN_FREE_BYTES) -> dict:
    """The 'Create Backup' button. Returns a result dict (date, time, status, file, warnings).

    If `same_disk_warning` is True, the backup is on the same disk as the shop data,
    so the screen should tell the owner to also copy it to a USB drive.
    """
    session.require(conn, "create_backup", "create backups")
    try:
        result = _write_backup(conn, session.db_key, folder, dest, now=now, min_free_bytes=min_free_bytes)
    except BackupError as exc:
        try:
            set_setting(conn, "last_backup_status", "failed")
            with transaction(conn):
                log_event(conn, "backup_failed", user_id=session.user_id, entity="backup", after={"reason": str(exc)})
        except Exception:
            pass
        raise
    set_setting(conn, "last_backup_at", result["created_at"])
    set_setting(conn, "last_backup_status", "success")
    with transaction(conn):
        log_event(
            conn, "backup_created", user_id=session.user_id, entity="backup",
            after={"file": result["file_name"], "size_bytes": result["size_bytes"]},
        )
    return result


# ---------------------------------------------------------------- open / test

def verify_backup_file(path) -> dict:
    """Quick check with no password: is the file complete and undamaged?"""
    header, _ = _read_file(path)
    return {"intact": True, "created_at": header["created_at"]}


def _open_backup(path, slot, secret):
    header, ciphertext = _read_file(path)
    with tempfile.TemporaryDirectory(prefix="csbak-") as tmp:
        key_copy = Path(tmp) / "shop.keys"
        key_copy.write_text(json.dumps(header["keystore"]), encoding="utf-8")
        try:
            key_hex = keystore.unlock_keystore(key_copy, slot, secret)
        except keystore.KeystoreError:
            raise BackupError("Wrong username, password or recovery key for this backup.") from None
    try:
        plain = AESGCM(bytes.fromhex(key_hex)).decrypt(bytes.fromhex(header["nonce"]), ciphertext, _aad(header))
    except InvalidTag:
        raise BackupError("The backup file is damaged.") from None
    manifest, db_bytes = _unpack(plain)
    return header, manifest, db_bytes, key_hex


def inspect_backup(path, slot, secret) -> dict:
    """'Test this backup' and the restore preview. Opens the backup read-only; changes nothing.

    `slot` is a username (with its password) or 'recovery' (with the recovery key).
    """
    header, manifest, db_bytes, key_hex = _open_backup(path, slot, secret)
    with tempfile.TemporaryDirectory(prefix="csbak-") as tmp:
        db = Path(tmp) / "check.db"
        db.write_bytes(db_bytes)
        try:
            stats = _stats(db, key_hex)
        except sqlcipher.Error:
            raise BackupError("The backup could not be opened. It may be damaged.") from None
    return {
        "ok": True,
        "created_at": manifest["created_at"],
        "schema_version": manifest["schema_version"],
        "bill_count": stats["bill_count"],
        "last_bill_no": stats["last_bill_no"],
        "sales_total_paise": stats["sales_total_paise"],
        "counts": stats["counts"],
        "newer_than_this_app": manifest["schema_version"] > max(MIGRATIONS),
    }


# ---------------------------------------------------------------- restore

def restore_backup(folder, backup_path, slot, secret, *, conn=None, session=None, confirm=False, now=None) -> dict:
    """The 'Restore Backup' button. Show `inspect_backup` first, then call this with confirm=True.

    - If a shop already exists in `folder`, pass its open `conn` and `session`
      (needs the restore_backup permission). A safety backup of the current
      data is made first, and the connection is closed. The shop then opens
      with the usernames and passwords as they were on the backup date.
    - On a clean machine (no shop in `folder`) nothing else is needed; the
      backup's own password or recovery key is the proof.
    Nothing is replaced unless the backup passes every check.
    """
    if not confirm:
        raise BackupError("Restore was not confirmed.")
    now = now or datetime.now()
    folder = Path(folder)
    db_path, key_path = _paths(folder)
    existing = db_path.exists() or key_path.exists()

    if existing:
        if conn is None or session is None:
            raise BackupError("Sign in as the owner before restoring over an existing shop.")
        session.require(conn, "restore_backup", "restore backups")

    header, manifest, db_bytes, key_hex = _open_backup(backup_path, slot, secret)
    if manifest["schema_version"] > max(MIGRATIONS):
        raise BackupError("This backup was made by a newer version of the app. Update the app first.")

    kept_counters = []
    if existing:
        kept_counters = [
            (r[0], r[1], r[2], r[3])
            for r in conn.execute("SELECT series, fin_year, prefix, last_number FROM number_series")
        ]

    folder.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".tmp-restore-", dir=folder))
    safety = None
    try:
        new_db, new_keys = work / "shop.db", work / "shop.keys"
        new_db.write_bytes(db_bytes)
        new_keys.write_text(json.dumps(header["keystore"], indent=1), encoding="utf-8")

        # Check the restored copy completely before anything is replaced.
        restored = open_db(new_db, key_hex)  # also upgrades an older schema
        try:
            if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("The backup failed its integrity check. Nothing was changed.")
            chain_ok, _ = verify_chain(restored)
            if not chain_ok:
                raise BackupError("The audit log in this backup has been tampered with. Nothing was changed.")
            for table, expected in manifest["counts"].items():
                if restored.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != expected:
                    raise BackupError("The backup contents do not match its record. Nothing was changed.")
            # Never lower a document counter: keep the higher of current and backup.
            with transaction(restored):
                for series, fin_year, prefix, last in kept_counters:
                    restored.execute(
                        "INSERT INTO number_series(series, fin_year, prefix, last_number) VALUES (?,?,?,?) "
                        "ON CONFLICT(series, fin_year) DO UPDATE SET last_number = MAX(last_number, excluded.last_number)",
                        (series, fin_year, prefix, last),
                    )
                by = None
                if session is not None:
                    row = restored.execute("SELECT id FROM users WHERE username = ?", (session.username,)).fetchone()
                    by = row[0] if row else None
                log_event(
                    restored, "backup_restored", user_id=by, entity="backup",
                    after={
                        "backup_created_at": manifest["created_at"],
                        "restored_by": session.username if session else "clean-machine restore",
                    },
                )
            summary = {
                "bill_count": restored.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
                "last_bill_no": (restored.execute("SELECT invoice_no FROM sales ORDER BY id DESC LIMIT 1").fetchone() or [None])[0],
                "counts": {t: restored.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _COUNT_TABLES},
            }
            restored.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            restored.close()
        for leftover in (work / "shop.db-wal", work / "shop.db-shm"):
            leftover.unlink(missing_ok=True)

        # Protect the current data, then swap.
        if existing:
            safety = _write_backup(conn, session.db_key, folder, folder / SAFETY_FOLDER, now=now)
            conn.close()
            for leftover in (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
                leftover.unlink(missing_ok=True)
        os.replace(new_db, db_path)
        os.replace(new_keys, key_path)
    except BackupError:
        raise
    except (OSError, sqlcipher.Error):
        raise BackupError("The restore failed. Your current data was not replaced.") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)

    return {
        "status": "success",
        "backup_created_at": manifest["created_at"],
        "bill_count": summary["bill_count"],
        "last_bill_no": summary["last_bill_no"],
        "counts": summary["counts"],
        "safety_backup": safety["path"] if safety else None,
        "message": "Restore complete. Sign in again. Passwords are as they were on the backup date.",
    }


# ---------------------------------------------------------------- reminders and housekeeping

def backup_due(conn, today=None) -> dict:
    """For the dashboard reminder: is the last backup older than the owner's setting (default 7 days)?"""
    today = today or date.today()
    try:
        days = int(get_setting(conn, "backup_reminder_days", DEFAULT_REMINDER_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_REMINDER_DAYS
    last = get_setting(conn, "last_backup_at")
    if not last:
        return {"due": True, "last_backup_at": None, "days_since": None, "reminder_days": days}
    since = (today - datetime.strptime(last, _FMT).date()).days
    return {"due": since >= days, "last_backup_at": last, "days_since": since, "reminder_days": days}


def list_backups(dest) -> list:
    """Backups found in a folder, newest first. Damaged files are listed as intact=False."""
    out = []
    for p in sorted(Path(dest).glob(f"{NAME_PREFIX}*{EXT}")):
        try:
            info = verify_backup_file(p)
            out.append({"path": str(p), "file_name": p.name, "created_at": info["created_at"],
                        "size_bytes": p.stat().st_size, "intact": True})
        except BackupError:
            out.append({"path": str(p), "file_name": p.name, "created_at": None,
                        "size_bytes": p.stat().st_size, "intact": False})
    return sorted(out, key=lambda b: b["file_name"], reverse=True)


def prune_backups(conn, session, dest, keep=DEFAULT_KEEP) -> list:
    """Delete the oldest backups beyond `keep`. Only runs when the owner asks; never automatic.

    Only files named like our backups are touched. Returns the file names deleted.
    """
    session.require(conn, "create_backup", "prune backups")
    if keep < 1:
        raise BackupError("Keep at least 1 backup.")
    old = list_backups(dest)[keep:]
    deleted = []
    for b in old:
        Path(b["path"]).unlink(missing_ok=True)
        deleted.append(b["file_name"])
    if deleted:
        with transaction(conn):
            log_event(conn, "backups_pruned", user_id=session.user_id, entity="backup", after={"deleted": deleted})
    return deleted
