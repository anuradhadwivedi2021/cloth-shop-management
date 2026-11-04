"""Screen text in English, Hindi and Marathi.

Every text a user can read is written in English inside tr("..."). (A text defined in a list at import time
is written N_("...") there and shown with tr(text).) If the chosen language has a
translation for it, that is shown; otherwise the English text is shown (so a missing translation
never breaks a screen, it only stays in English).

    from ui.i18n import tr
    label(tr("Today's sales"))
    label(tr("Net sales after returns: {amount}", amount=format_rupees(p)))   # {placeholders} are filled in

Translations are plain files: ui/translations/hi.json and ui/translations/mr.json, each a
{"English text": "translated text"} dictionary. Keep the {placeholders} exactly as in English.

The language is the shop setting `ui_language` (en / hi / mr). MainWindow loads it when it opens, so a
change in Settings applies the next time the window is built (sign out and in again).

Find texts that still need translating:   python -m ui.i18n
"""

import ast
import json
import sys
from pathlib import Path

SUPPORTED = {"en": "English", "hi": "हिन्दी", "mr": "मराठी"}
TRANSLATIONS_DIR = Path(__file__).resolve().parent / "translations"
SOURCE_DIRS = ("ui", "core")

_language = "en"
_tables = {}


def _table(code: str) -> dict:
    if code not in _tables:
        path = TRANSLATIONS_DIR / f"{code}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            data = {}
        _tables[code] = data if isinstance(data, dict) else {}
    return _tables[code]


def reload() -> None:
    """Forget loaded translation files (after editing them)."""
    _tables.clear()


def set_language(code: str) -> None:
    global _language
    if code not in SUPPORTED:
        raise ValueError(f"Language must be one of {', '.join(SUPPORTED)}.")
    _language = code


def language() -> str:
    return _language


def load_language_from_settings(conn) -> str:
    """Use the shop's ui_language setting (English if it is not set or not valid)."""
    from core.settings import get_setting  # screens never run SQL themselves

    code = get_setting(conn, "ui_language", "en") or "en"
    set_language(code if code in SUPPORTED else "en")
    return _language


def N_(text: str) -> str:
    """Marks a text for translation without translating it yet (for lists and tables defined at import time).
    Later show it with tr(text)."""
    return text


def tr(text: str, **values) -> str:
    """The text in the current language. {placeholders} are filled from keyword arguments."""
    shown = _table(_language).get(text) or text if _language != "en" else text
    if not values:
        return shown
    try:
        return shown.format(**values)
    except (KeyError, IndexError, ValueError):
        return text.format(**values)  # a broken translation must never break the screen


# ---------------------------------------------------------------- finding texts to translate

def source_strings(root=None) -> dict:
    """Every literal text passed to tr(...) or N_(...) in the source: {text: ['file:line', ...]}."""
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    found = {}
    for folder in SOURCE_DIRS:
        for path in sorted((root / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("tr", "N_")
                        and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                    found.setdefault(node.args[0].value, []).append(f"{path.relative_to(root).as_posix()}:{node.lineno}")
    return found


def missing_translations(code: str, root=None) -> list:
    """Texts used in the source that the language file does not have yet."""
    if code == "en":
        return []
    table = _table(code)
    return sorted(text for text in source_strings(root) if not table.get(text))


def placeholder_mistakes(code: str) -> list:
    """Translations whose {placeholders} differ from the English text (they would break a screen)."""
    import string

    def names(text):
        return sorted(f for _, f, _, _ in string.Formatter().parse(text) if f)

    return sorted(k for k, v in _table(code).items() if names(k) != names(v))


if __name__ == "__main__":
    bad = False
    for lang in ("hi", "mr"):
        missing = missing_translations(lang)
        wrong = placeholder_mistakes(lang)
        print(f"{SUPPORTED[lang]}: {len(missing)} not translated, {len(wrong)} with wrong {{placeholders}}")
        for text in missing:
            print("   missing:", text)
        for text in wrong:
            print("   placeholder mismatch:", text)
        bad = bad or bool(missing or wrong)
    sys.exit(1 if bad else 0)
