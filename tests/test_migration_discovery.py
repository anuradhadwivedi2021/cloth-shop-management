"""Migrations must be found without looking at .py files on disk, because the packaged .exe has no source files."""

import re
from pathlib import Path

from db import schema

DB_FOLDER = Path(__file__).resolve().parent.parent / "db"


def test_default_discovery_does_not_need_source_files_on_disk(monkeypatch):
    on_disk = {int(m.group(1)) for p in DB_FOLDER.glob("migration_*.py") if (m := re.match(r"migration_(\d+)\.py", p.name))}

    def no_files(self, pattern):
        raise AssertionError("discovery must not glob the folder when no directory is given")

    monkeypatch.setattr(Path, "glob", no_files)
    assert set(schema.discover_migrations()) == on_disk
    assert 13 in schema.discover_migrations()  # the one that is only found by discovery, not listed in schema.py
