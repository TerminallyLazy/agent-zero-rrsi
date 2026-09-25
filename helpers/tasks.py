"""Sanitized task admission and deterministic graders, outside candidate state."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from usr.plugins.rrsi.helpers.contracts import TaskSpec, TaskPack
from usr.plugins.rrsi.helpers.state import StateStore, canonical_hash, confined, utc_now

SECRET_PATTERNS = (
    (r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", "PRIVATE_KEY"),
    (r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b", "TOKEN"),
    (r"(?i)\b(?:api[_ -]?key|password|passwd|secret|authorization|access[_ -]?token)\s*[:=]\s*[^\s,;]+", "SECRET"),
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "EMAIL"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "SSN"),
    (r"(?<!\w)(?:\+\d{1,3}[- .]?)?\(?\d{3}\)?[- .]\d{3}[- .]\d{4}(?!\w)", "PHONE"),
    (r"(?i)(?:/Users/|/home/)[^/\s]+", "HOME"),
    (r"(?i)\b(?:MRN|DOB|patient[_ -]?id|account[_ -]?number)\s*[:=]\s*\S+", "IDENTIFIER"),
)
SENSITIVE_CONTEXT = re.compile(r"(?i)\b(patient|medical record|confidential|social security|credit card|bank account|private key|bearer)\b")
LIVE_EFFECT = re.compile(r"(?i)\b(send (?:an? )?(?:email|message)|purchase|buy|transfer money|delete account|submit payment|sign in|log in)\b")


def sanitize(text: str, secrets: list[str] | None = None) -> dict:
    value = str(text)
    reasons = []
    for secret in secrets or []:
        if secret and len(secret) >= 4 and secret in value:
            value = value.replace(secret, "[REDACTED_SECRET]")
            reasons.append("known_secret")
    for pattern, label in SECRET_PATTERNS:
        value, count = re.subn(pattern, f"[REDACTED_{label}]", value)
        if count:
            reasons.append(label.lower())
    quarantine = bool(SENSITIVE_CONTEXT.search(value))
    return {"text": value, "redactions": sorted(set(reasons)), "quarantined": quarantine}


def validate_task_pack(tasks: list[TaskSpec]) -> None:
    if not tasks:
        raise ValueError("Task pack cannot be empty")
    ids, families = set(), {}
    for task in tasks:
        if task.id in ids:
            raise ValueError("Duplicate task ID")
        ids.add(task.id)
        if task.family in families and families[task.family] != task.split:
            raise ValueError("A task family cannot cross evolve/heldout/transfer boundaries")
        families[task.family] = task.split
        scan = sanitize(json.dumps(task.to_dict()))
        if scan["quarantined"] or scan["redactions"]:
            raise ValueError("Task pack contains sensitive or unredacted content")


def grade(task: TaskSpec, response: str, output_root: Path | None = None) -> dict:
    rule = task.evaluator
    kind = rule["kind"]
    try:
        if kind == "exact":
            passed = response.strip() == str(rule["expected"]).strip()
        elif kind == "json":
            body = response.strip()
            if body.startswith("```"):
                body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
            passed = json.loads(body) == rule["expected"]
        elif kind == "contains":
            passed = all(x in response for x in rule["all"]) and not any(x in response for x in rule.get("none", []))
        elif kind == "files":
            if output_root is None:
                raise ValueError("Output directory required")
            passed = all(confined(output_root, name, existing=True).read_text() == expected
                         for name, expected in rule["expected"].items())
        elif kind == "python_tests":
            # Execute tests only in the separate grader container, never on the controller.
            raise ValueError("python_tests requires the isolated grader executor")
        else:
            raise ValueError("Unsupported evaluator")
        return {"reward": float(passed), "valid": True, "reason": "passed" if passed else "incorrect_output"}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {"reward": 0.0, "valid": False, "reason": type(exc).__name__}


class TaskRepository:
    def __init__(self, store: StateStore):
        self.store = store

    def import_pack(self, values: list[dict], *, source: str = "import") -> list[str]:
        tasks = [TaskSpec(**value) for value in values]
        validate_task_pack(tasks)
        with self.store.lock("tasks", blocking=True):
            current = {t.id: t for t in self.list()}
            for task in tasks:
                if task.id in current and current[task.id] != task:
                    raise ValueError("Existing task IDs are immutable; use a new ID")
                current[task.id] = task
            validate_task_pack(list(current.values()))
            for task in tasks:
                self.store.write_json(f"tasks/{task.id}.json", task.to_dict())
        self.store.event("task_import", source=source, ids=[t.id for t in tasks])
        return [t.id for t in tasks]

    def list(self, split: str | None = None) -> list[TaskSpec]:
        folder = self.store.path("tasks")
        tasks = [TaskSpec(**json.loads(p.read_text())) for p in sorted(folder.glob("*.json"))]
        return [t for t in tasks if split is None or t.split == split]

    def capture(self, *, prompt: str, response: str, context_id: str,
                replay_task: dict | None = None, known_secrets: list[str] | None = None) -> dict:
        """Record sanitized material only. Unscorable interactions remain ineligible."""
        p, r = sanitize(prompt, known_secrets), sanitize(response, known_secrets)
        digest = canonical_hash({"prompt": p["text"], "response": r["text"]})
        status = "quarantined" if p["quarantined"] or r["quarantined"] else "needs_replay_fixture"
        if LIVE_EFFECT.search(p["text"]):
            status = "external_side_effect"
        record = {"id": digest, "at": utc_now(),
                  "context_hash": hashlib.sha256(context_id.encode()).hexdigest(),
                  "status": status,
                  "redactions": sorted(set(p["redactions"] + r["redactions"]))}
        # No chat prose is persisted. Unknown names and context can remain sensitive
        # even when regexes find nothing; retain metadata until a defensible replay exists.
        if status == "quarantined":
            record["quarantine_reason"] = "sensitive_context"
        elif replay_task is not None and status != "external_side_effect":
            task = TaskSpec(**replay_task)
            validate_task_pack([task])
            # A trusted fixture receipt must name the independent oracle, never the captured answer.
            if task.source != "replay_fixture":
                raise ValueError("Captured answers are not independent evaluator truth")
            self.import_pack([task.to_dict()], source="capture")
            record.update(status="eligible", task_id=task.id)
        else:
            record["quarantine_reason"] = "unproven_reproducibility_or_sensitive_content"
        self.store.write_json(f"captures/{digest}.json", record)
        return {k: v for k, v in record.items() if k not in {"prompt", "response"}}

    def expire_captures(self, retention_days: int = 30) -> int:
        if type(retention_days) is not int or retention_days < 0:
            raise ValueError("Retention must be a nonnegative number of days")
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        removed = 0
        with self.store.lock("captures", blocking=True):
            for path in self.store.path("captures").glob("*.json"):
                safe = self.store.path(path.relative_to(self.store.root))
                value = json.loads(safe.read_text())
                if datetime.fromisoformat(value["at"].replace("Z", "+00:00")) < cutoff:
                    safe.unlink()
                    removed += 1
        return removed

    def snapshot(self) -> TaskPack:
        values = [t.to_dict() for t in self.list()]
        validate_task_pack([TaskSpec(**v) for v in values])
        return {"schema_version": 1, "sha256": canonical_hash(values), "tasks": values}
