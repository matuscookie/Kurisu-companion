"""
storage_index.py - file-name indexer + search (no Qt, plain Python/SQLite)

build_index() walks your drives once and stores every file/folder name in a
small SQLite database. search() then queries that database, so searches feel
instant instead of re-scanning the disk each time.
"""

import os
import sqlite3
import string
import sys
import time
from pathlib import Path

# Folder names that are never indexed (compared in lower case, anywhere).
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "appdata", "node_modules",
    ".git", "__pycache__", ".cache", "$windows.~bt", "windows.old",
}
# Skipped only when directly under a drive root (C:\Windows, /proc, ...).
ROOT_ONLY_SKIP = {"windows", "proc", "sys", "dev"}

# Filter dropdown: category -> file extensions
KINDS = {
    "Documents": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
                  "md", "rtf", "odt", "csv", "epub"],
    "Images": ["png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "tif",
               "tiff", "heic", "psd"],
    "Video": ["mp4", "mkv", "avi", "mov", "wmv", "webm", "flv"],
    "Audio": ["mp3", "wav", "flac", "aac", "ogg", "m4a", "wma"],
    "Code": ["py", "js", "ts", "html", "css", "json", "java", "c", "cpp",
             "h", "cs", "go", "rs", "rb", "php", "sh", "bat"],
    "Archives": ["zip", "rar", "7z", "tar", "gz", "bz2", "iso"],
}


def default_roots() -> list[str]:
    """Every local drive on Windows; home folder + mounted volumes elsewhere."""
    if sys.platform.startswith("win"):
        import ctypes
        roots = []
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                # 2 = removable, 3 = fixed (skips network drives and CD-ROMs)
                if ctypes.windll.kernel32.GetDriveTypeW(root) in (2, 3):
                    roots.append(root)
        return roots

    roots = [str(Path.home())]
    import glob
    for pattern in ("/Volumes/*", "/media/*/*", "/mnt/*"):
        for p in glob.glob(pattern):
            if os.path.isdir(p) and not os.path.islink(p):
                roots.append(p)
    return roots


def build_index(roots, db_path, should_stop=lambda: False,
                on_progress=lambda n, d: None):
    """Walk `roots` and write the index. Returns (total_items, completed).

    The index is built in a temp file and swapped in at the end, so the old
    index keeps working while a new one is being built.
    """
    db_path = Path(db_path)
    tmp = db_path.with_name(db_path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()

    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(
        "CREATE TABLE files (name TEXT, path TEXT, is_dir INTEGER, "
        "size INTEGER, mtime REAL, ext TEXT)"
    )
    insert = "INSERT INTO files VALUES (?,?,?,?,?,?)"

    batch, total = [], 0
    stack = [r for r in roots if os.path.isdir(r)]

    def flush(current):
        nonlocal total
        con.executemany(insert, batch)
        con.commit()
        total += len(batch)
        batch.clear()
        on_progress(total, current)

    while stack and not should_stop():
        cur = stack.pop()
        is_root = os.path.dirname(cur) == cur
        try:
            it = os.scandir(cur)
        except OSError:
            continue                      # no permission / vanished
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    break
                try:
                    name = entry.name
                    if entry.is_symlink():
                        continue
                    is_junction = getattr(entry, "is_junction", None)
                    if is_junction is not None and is_junction():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        low = name.lower()
                        if low in SKIP_DIRS:
                            continue
                        if is_root and low in ROOT_ONLY_SKIP:
                            continue
                        stack.append(entry.path)
                        batch.append((name, entry.path, 1, 0, 0.0, ""))
                    else:
                        st = entry.stat(follow_symlinks=False)
                        ext = os.path.splitext(name)[1][1:].lower()
                        batch.append(
                            (name, entry.path, 0, st.st_size, st.st_mtime, ext)
                        )
                except OSError:
                    continue
        if len(batch) >= 10000:
            flush(cur)

    if batch:
        flush("")
    completed = not should_stop()
    con.close()

    if not completed:
        tmp.unlink(missing_ok=True)
        return total, False

    # Swap the new index in (retry briefly if a search has the old one open).
    last_err = None
    for _ in range(10):
        try:
            os.replace(tmp, db_path)
            return total, True
        except OSError as e:
            last_err = e
            time.sleep(0.2)
    raise last_err


def _pattern(term: str) -> str:
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def search(db_path, text: str, kind: str = "All", limit: int = 300):
    """Return up to `limit` rows: (name, path, is_dir, size, mtime).

    Every word typed must appear in the name (best results) or in the full
    path (extra results shown after).  Case-insensitive.
    """
    terms = text.split()
    if not terms or not Path(db_path).exists():
        return []

    extra, extra_params = "", []
    if kind == "Folders":
        extra = " AND is_dir = 1"
    elif kind in KINDS:
        exts = KINDS[kind]
        extra = f" AND ext IN ({','.join('?' * len(exts))})"
        extra_params = list(exts)

    name_all = " AND ".join("name LIKE ? ESCAPE '\\'" for _ in terms)
    path_all = " AND ".join("path LIKE ? ESCAPE '\\'" for _ in terms)
    pats = [_pattern(t) for t in terms]
    cols = "SELECT name, path, is_dir, size, mtime FROM files"

    con = sqlite3.connect(Path(db_path).as_uri() + "?mode=ro", uri=True)
    try:
        # 1) matches on the file name
        rows = con.execute(
            f"{cols} WHERE {name_all}{extra} LIMIT 2000", pats + extra_params
        ).fetchall()

        first = terms[0].lower()
        whole = text.strip().lower()

        def rank(r):
            n = r[0].lower()
            return (n != whole, not n.startswith(first), len(n), n)

        rows.sort(key=rank)
        rows = rows[:limit]

        # 2) top up with matches that are only in the folder path
        if len(rows) < limit:
            more = con.execute(
                f"{cols} WHERE {path_all} AND NOT ({name_all}){extra} LIMIT ?",
                pats + pats + extra_params + [limit - len(rows)],
            ).fetchall()
            rows += more
        return rows
    finally:
        con.close()
