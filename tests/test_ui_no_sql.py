"""Screens must never talk to the database directly: they call tested core functions.

Doing SQL in a screen skips permission checks, the audit log, transactions and the
"stock changes only through movements" rule. Screens still to be re-wired are listed in
LEGACY; when one is fixed, remove it from the list (this test tells you if you forget).
"""

import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"
FORBIDDEN = re.compile(r"\.execute\(|\.executemany\(|\.executescript\(|\.cursor\(|\.commit\(\)|\.rollback\(\)")

# Screens that still need to be re-wired onto core/ (remove each name once it is clean).
LEGACY = {
    
    
    "expenses_view.py",
    "purchases_view.py",
    "settings_view.py",
    "stock_count_view.py",
}


def _offending(path: Path) -> list:
    hits = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        if FORBIDDEN.search(code):
            hits.append(f"{path.name}:{number}: {line.strip()}")
    return hits


def test_screens_do_not_run_sql():
    problems = []
    for path in sorted(UI.glob("*.py")):
        if path.name not in LEGACY:
            problems += _offending(path)
    assert not problems, "Screens must call core/ functions, not the database:\n" + "\n".join(problems)


def test_legacy_list_has_no_stale_entries():
    for name in sorted(LEGACY):
        path = UI / name
        assert path.exists(), f"{name} is in LEGACY but does not exist"
        assert _offending(path), f"{name} is clean now: remove it from LEGACY"