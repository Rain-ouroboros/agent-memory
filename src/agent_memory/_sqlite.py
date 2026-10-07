"""Private file creation for the explicit storage path supplied by the host."""
import os
import sqlite3
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ':memory:':
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    return sqlite3.connect(str(path), timeout=5, check_same_thread=False)
