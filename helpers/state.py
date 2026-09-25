"""Durable local state, outside the plugin's HTTP-served source directory."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def confined(root: Path, relative: str | Path, *, existing: bool = False) -> Path:
    base = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("Path must be relative and stay inside its owner directory")
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise ValueError("Path escapes its owner directory")
    if existing and not target.exists():
        raise FileNotFoundError(str(relative))
    return target


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode() + b"\n"
    fd, tmp = tempfile.mkstemp(prefix=".rrsi-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class BusyError(RuntimeError):
    pass


class StateStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.environ.get("RRSI_STATE_DIR") or
                         Path(__file__).resolve().parents[3] / "rrsi").resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def path(self, relative: str | Path) -> Path:
        return confined(self.root, relative)

    def read_json(self, relative: str | Path, default: Any = None) -> Any:
        path = self.path(relative)
        if not path.exists():
            return default
        return json.loads(path.read_text())

    def write_json(self, relative: str | Path, value: Any) -> None:
        atomic_json(self.path(relative), value)

    def model_binding_key(self) -> bytes:
        """Private installation key; never return it from public evidence APIs."""
        with self.lock("model-binding-key", blocking=True):
            relative = "private/model-binding-key.json"
            record = self.read_json(relative)
            if record is None:
                record = {"key": os.urandom(32).hex()}
                self.write_json(relative, record)
            value = record.get("key") if isinstance(record, dict) else None
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("Model binding key is invalid; restore the private RRSI state backup")
            os.chmod(self.path(relative), 0o600)
            return bytes.fromhex(value)

    @contextmanager
    def lock(self, name: str, *, blocking: bool = False) -> Iterator[None]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError("Invalid lock name")
        path = self.path(f"locks/{name}.lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a+") as handle:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise BusyError(f"{name} is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def event(self, kind: str, **data: Any) -> dict:
        record = {"at": utc_now(), "kind": kind, **data}
        with self.lock("events", blocking=True):
            path = self.path("events.jsonl")
            with path.open("a", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def active_version(self) -> str:
        return self.read_json("active.json", {}).get("version", "baseline")

    def artifact_path(self, version: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", version):
            raise ValueError("Harness version must be a SHA-256 digest")
        return self.path(f"artifacts/h_{version}")

    def set_active(self, version: str, reason: str, *, expected_version: str | None = None) -> dict:
        if version != "baseline" and not (self.artifact_path(version) / "manifest.json").is_file():
            raise FileNotFoundError("Harness artifact is unavailable")
        with self.lock("activation", blocking=True):
            previous = self.active_version()
            if expected_version is not None and previous != expected_version:
                return {"version":previous,"skipped":True,"reason":"active_version_changed"}
            if version == previous:
                return {**self.read_json("active.json", {"version":version,"previous":version}),"unchanged":True}
            receipt = {"version": version, "previous": previous, "reason": reason,
                       "at": utc_now()}
            self.write_json("active.json", receipt)
            self.event("activation", **receipt)
        return receipt

    def rollback(self, reason: str, *, expected_version: str | None = None, validate_previous=None) -> dict:
        with self.lock("activation", blocking=True):
            active = self.read_json("active.json", {"version":"baseline","previous":"baseline"})
            if expected_version is not None and active["version"] != expected_version:
                return {"version":active["version"],"skipped":True,"reason":"active_version_changed"}
            previous=active.get("previous","baseline")
            rejected_previous = None
            if previous != "baseline":
                try:
                    if not (self.artifact_path(previous)/"manifest.json").is_file():
                        raise FileNotFoundError("Previous harness artifact is unavailable")
                    if validate_previous is not None:
                        validate_previous(previous)
                except (ValueError, OSError, KeyError, TypeError):
                    rejected_previous, previous = previous, "baseline"
            if previous == active["version"]:
                return {**active,"unchanged":True}
            receipt={"version":previous,"previous":active["version"],"reason":reason,"at":utc_now()}
            if rejected_previous is not None:
                receipt["rejected_previous"] = rejected_previous
                receipt["recovery_reason"] = "Previous artifact missing or incompatible with current framework"
            self.write_json("active.json",receipt)
            self.event("activation",**receipt)
            return receipt
