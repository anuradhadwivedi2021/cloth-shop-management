import os, zipfile

SKIP_DIRS = {".venv", "venv", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
             "node_modules", "dist", "build", "backups", ".idea", ".vscode"}
SKIP_EXT = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".zip", ".log", ".bak", ".key", ".env"}
SKIP_NAMES = {".env"}

with zipfile.ZipFile("cloth-shop-src.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_NAMES or os.path.splitext(f)[1].lower() in SKIP_EXT:
                continue
            path = os.path.join(root, f)
            z.write(path, os.path.relpath(path, ".").replace("\\", "/"))
print("done:", os.path.getsize("cloth-shop-src.zip") // 1024, "KB")
