"""Audited integration addition: durable text/JSON writes, no scientific logic."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def atomic_text(path: Path | str, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".rrsi-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path: Path | str, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=1, allow_nan=False) + "\n")


class HaltRun(RuntimeError):
    """Operational interruption must not be scored as a failed candidate."""
