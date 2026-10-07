"""Private file creation for the explicit storage path supplied by the host."""
import os
import math
import sqlite3
from pathlib import Path


def connect(path: str | Path, *, timeout: float = 5) -> sqlite3.Connection:
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and non-negative")
    if str(path) != ':memory:':
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    return sqlite3.connect(str(path), timeout=timeout, check_same_thread=False)
