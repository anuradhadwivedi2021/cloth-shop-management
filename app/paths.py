"""Where the shop's data lives on this computer (never uploaded anywhere)."""

import os
from pathlib import Path

ENV_VAR = "CLOTHSHOP_DATA"


def data_folder() -> Path:
    """Folder that holds shop.db and shop.keys.

    Windows: %LOCALAPPDATA%\\ClothShop   (per-user, survives app updates)
    Other:   ~/.clothshop
    Set CLOTHSHOP_DATA to use another folder (for example a test folder).
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "ClothShop" if base else Path.home() / ".clothshop"
