import json
import os
import tempfile
from typing import Any, Callable

from filelock import FileLock


def atomic_write_json(path: str, obj: Any) -> None:
    """Atomically write JSON to `path` using a temp file and os.replace()."""
    dirpath = os.path.dirname(path)
    os.makedirs(dirpath or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirpath, prefix=".tmp", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def locked_read_json(path: str, default: Any = None) -> Any:
    """Read JSON with a file lock; returns `default` if file missing or unreadable."""
    lock_path = f"{path}.lock"
    lock = FileLock(lock_path)
    with lock:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default


def locked_update_json(path: str, update_fn: Callable[[Any], Any], default: Any = None) -> None:
    """Atomically read-modify-write JSON under a file lock.

    update_fn receives the current object (or default) and should return the new object to write.
    """
    lock_path = f"{path}.lock"
    lock = FileLock(lock_path)
    with lock:
        current = default
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    current = json.load(f)
            except Exception:
                current = default

        new_obj = update_fn(current)
        atomic_write_json(path, new_obj)
