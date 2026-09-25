"""Content-addressed harness publication and compatibility receipts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from usr.plugins.rrsi.helpers.runtime import manifest_hash, validate_manifest
from usr.plugins.rrsi.helpers.contracts import HarnessVersion
from usr.plugins.rrsi.helpers.state import StateStore, atomic_json, confined, utc_now


def baseline_manifest(revision: str) -> HarnessVersion:
    return {"schema_version": 1, "compatibility": {"framework_revision": revision},
            "callbacks": {}, "prompts": {}, "config": {}, "tools": {}, "skills": {}, "roles": {}}


class ArtifactStore:
    def __init__(self, store: StateStore, framework_revision: str):
        self.store, self.revision = store, framework_revision

    def materialize(self, source: Path) -> tuple[str, Path]:
        """Hash and validate a candidate without importing any candidate code."""
        source = source.resolve()
        manifest = json.loads((source / "manifest.json").read_text())
        manifest.pop("version", None)
        manifest.pop("files", None)
        hashes = {}
        for file in sorted(source.rglob("*")):
            relative = file.relative_to(source)
            if file.is_symlink():
                raise ValueError("Harness payload must not contain symlinks")
            if any(part in {"extensions", ".git", "__pycache__"} for part in relative.parts) or file.name == "hooks.py":
                raise ValueError("Harness payload contains a reserved path")
            if file.is_file() and relative.as_posix() != "manifest.json":
                if file.stat().st_size > 4*1024*1024:
                    raise ValueError("Harness file exceeds 4 MiB")
                hashes[relative.as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
        if len(hashes) > 512:
            raise ValueError("Harness payload contains too many files")
        manifest["files"] = hashes
        manifest["version"] = version = manifest_hash(manifest)
        validate_manifest(source, manifest, self.revision)
        target = self.store.artifact_path(version)
        with self.store.lock("artifacts", blocking=True):
            if target.exists():
                existing = json.loads((target / "manifest.json").read_text())
                validate_manifest(target, existing, self.revision)
                if existing != manifest:
                    raise ValueError("Artifact identity collision")
                return version, target
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=target.parent))
            try:
                for relative in hashes:
                    dst = confined(staging, relative); dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(confined(source, relative), dst)
                atomic_json(staging / "manifest.json", manifest)
                staging.rename(target)
            finally:
                if staging.exists(): shutil.rmtree(staging)
        return version, target

    def activate(self, version: str, *, campaign: str, checks: dict, expected_version: str | None = None) -> dict:
        required = {"compatibility", "runtime_canary", "tool_policy", "response_termination"}
        if not required <= checks.keys() or not all(checks[key] is True for key in required):
            raise ValueError("A complete passing functional canary receipt is required")
        root = self.store.artifact_path(version)
        validate_manifest(root, json.loads((root / "manifest.json").read_text()), self.revision)
        self.store.write_json(f"activation-checks/{version}.json", {"at":utc_now(),"campaign":campaign,"checks":checks})
        return self.store.set_active(version, f"completed campaign {campaign}",expected_version=expected_version)

    def rollback(self, reason: str, *, expected_version: str | None = None) -> dict:
        def validate_previous(version):
            root = self.store.artifact_path(version)
            validate_manifest(root, json.loads((root / "manifest.json").read_text()), self.revision)
        return self.store.rollback(reason, expected_version=expected_version,
                                   validate_previous=validate_previous)
