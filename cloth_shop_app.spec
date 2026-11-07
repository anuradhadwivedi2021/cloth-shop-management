# -*- mode: python ; coding: utf-8 -*-
# Build:  python -m PyInstaller --clean cloth_shop_app.spec      (or run build_exe.ps1)
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None


def _everything(name):
    """All modules of a package (some are only loaded at run time, so PyInstaller cannot see them)."""
    try:
        return collect_submodules(name)
    except Exception:
        return [name]


hidden = []
# Our own packages: ui/screens/* are plug-ins found at run time, db/migration_*.py are found by pkgutil.
for package in ('app', 'core', 'db', 'ui'):
    hidden += _everything(package)
# Libraries that load native parts or load modules by name.
for package in ('sqlcipher3', 'argon2', 'reportlab', 'cryptography'):
    hidden += _everything(package)

# Only the translation files are data. The program code is compiled into the .exe (source is not shipped).
added_files = [('ui/translations', 'ui/translations')]

a = Analysis(
    ['app/main.py'],
    pathex=['.'],
    binaries=[],
    datas=added_files,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ClothShopManagementSystem',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can break Qt DLLs and triggers antivirus warnings
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
