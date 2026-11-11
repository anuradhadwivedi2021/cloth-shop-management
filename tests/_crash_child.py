"""Helper for tests/test_srs_crash.py: a separate process that is killed on purpose.

Not a test itself (the name starts with an underscore). The parent starts it with a mode and arguments:

  count  FOLDER KEY            print how many SQL statements ONE bill save runs
  sale   FOLDER KEY K          save one bill, but die ("power cut") when its K-th SQL statement starts
  loop   FOLDER KEY            save bills forever (the parent kills it with SIGKILL at a random moment)
  backup FOLDER KEY DEST WHEN  make a backup; die before ('before') or right after ('after') the file appears
  restore FOLDER KEY BACKUP USER PASSWORD K
                               restore a backup; die just BEFORE the K-th os.replace call

"Die" means os._exit(137): no clean-up, no commit, no rollback, exactly like pulling the plug.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DIE = 137


def _sale(conn, i=0):
    from core.sales import create_sale

    variant = conn.execute("SELECT id FROM variants ORDER BY id LIMIT 1").fetchone()[0]
    return create_sale(conn, [{"variant_id": variant, "qty": 1}], [{"mode": "cash", "amount_paise": 105000}],
                       user_id=1, on_date="2026-11-07", idempotency_key=f"crash-{os.getpid()}-{i}")


def _fast_crypto():
    """The same speed-up tests/conftest.py gives the test process, so keys made there open here."""
    from argon2 import PasswordHasher

    from core import auth, keystore

    auth._ph = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    keystore.KDF_N = 2 ** 10


def main(argv):
    from db.connection import open_db

    _fast_crypto()

    mode, folder, key = argv[1], argv[2], argv[3]
    conn = open_db(os.path.join(folder, "shop.db"), key)

    if mode == "count":
        seen = []
        conn.set_trace_callback(seen.append)
        _sale(conn)
        print(len(seen))
    elif mode == "sale":
        limit, seen = int(argv[4]), []

        def trace(statement):
            seen.append(statement)
            if len(seen) >= limit:
                os._exit(DIE)

        conn.set_trace_callback(trace)
        _sale(conn)
    elif mode == "loop":
        print("ready", flush=True)
        i = 0
        while True:
            _sale(conn, i)
            i += 1
    elif mode == "backup":
        import core.backup as backup
        from core.session import Session  # noqa: F401  (imported so a missing module fails loudly)

        dest, when = argv[4], argv[5]
        real = backup.os.replace

        def replace(src, dst):
            if when == "before":
                os._exit(DIE)
            real(src, dst)
            os._exit(DIE)

        backup.os.replace = replace
        backup._write_backup(conn, key, folder, dest)
    elif mode == "restore":
        import core.backup as backup
        from types import SimpleNamespace

        path, user, password, limit = argv[4], argv[5], argv[6], int(argv[7])
        calls = []
        real = backup.os.replace

        def replace(src, dst):
            calls.append(dst)
            if len(calls) >= limit:
                sys.stderr.write(f"DIED before replacing {os.path.basename(str(dst))}\n")
                sys.stderr.flush()
                os._exit(DIE)
            real(src, dst)

        backup.os.replace = replace
        session = SimpleNamespace(require=lambda *a, **k: None, db_key=key, username=user, user_id=1)
        backup.restore_backup(folder, path, user, password, conn=conn, session=session, confirm=True)
    else:
        raise SystemExit("unknown mode")


if __name__ == "__main__":
    main(sys.argv)
