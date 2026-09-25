"""Validated, transport-neutral RRSI records. No framework imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import re
from typing import Any, TypedDict, NotRequired

SCHEMA_VERSION = 1
COMPONENTS = ("prompt", "control_flow", "config", "output_plumbing", "context_mgmt",
              "client_tool", "skill", "memory", "subagent")
UPSTREAM_COMMIT = "be50316e1db05914068a973f322770ef08ed7ba1"


class TaskPack(TypedDict):
    schema_version: int
    sha256: str
    tasks: list[dict[str, Any]]


class CampaignRecord(TypedDict):
    id: str
    status: str
    created_at: str
    framework_revision: str
    starting_version: str
    task_hash: str
    task_ids: list[str]
    upstream_commit: str
    next_round: int
    automatic_activation: bool
    updated_at: NotRequired[str]
    operation: NotRequired[str]
    operation_args: NotRequired[dict[str, Any]]
    kind: NotRequired[str]


class TrialMeasurement(TypedDict):
    task_id: str
    trial: int
    trial_id: str
    manifest_hash: str
    harness_version: str
    complete: bool
    runtime_valid: bool
    response: str
    trace: dict[str, Any]
    grade: dict[str, Any]
    usage: dict[str, Any]
    error: str | None


class HarnessVersion(TypedDict):
    schema_version: int
    compatibility: dict[str, Any]
    callbacks: dict[str, str]
    prompts: dict[str, str]
    config: dict[str, Any]
    tools: dict[str, Any]
    skills: dict[str, Any]
    roles: dict[str, Any]
    files: NotRequired[dict[str, str]]
    version: NotRequired[str]


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,95}", value):
        raise ValueError("Expected a bounded identifier containing letters, digits, _ or -")
    return value


@dataclass(frozen=True)
class UsageReceipt:
    call_id: str
    role: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    reported: bool = True
    trial_id: str | None = None

    def __post_init__(self):
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a reported nonnegative integer")
        if self.cost_usd is not None and (not math.isfinite(self.cost_usd) or self.cost_usd < 0):
            raise ValueError("Invalid cost")

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TaskSpec:
    id: str
    family: str
    split: str
    prompt: str
    evaluator: dict[str, Any]
    fixtures: dict[str, str] = field(default_factory=dict)
    source: str = "curated"
    sanitized: bool = True

    def __post_init__(self):
        identifier(self.id)
        identifier(self.family)
        if self.split not in {"evolve", "heldout", "transfer"}:
            raise ValueError("Unknown task split")
        if not self.prompt.strip() or not self.sanitized:
            raise ValueError("Tasks must contain sanitized instructions")
        if self.evaluator.get("kind") not in {"exact", "json", "contains", "files", "python_tests"}:
            raise ValueError("Task needs an independently executable supported evaluator")
        for path in self.fixtures:
            from pathlib import PurePosixPath
            p = PurePosixPath(path)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("Fixture path escapes task directory")
        if len(self.fixtures) > 128 or sum(len(v.encode()) for v in self.fixtures.values()) > 8*1024*1024:
            raise ValueError("Task fixtures exceed the bounded admission limit")
        kind = self.evaluator["kind"]
        if kind in {"exact", "json", "files"} and "expected" not in self.evaluator:
            raise ValueError("Evaluator lacks independently specified expected output")
        if kind == "files":
            from pathlib import PurePosixPath
            expected = self.evaluator["expected"]
            if not isinstance(expected, dict) or not expected:
                raise ValueError("File evaluator needs expected files")
            for path, value in expected.items():
                if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts or not isinstance(value, str):
                    raise ValueError("Invalid file evaluator")
        if kind == "python_tests":
            from pathlib import PurePosixPath
            module = PurePosixPath(self.evaluator.get("module", ""))
            if module.is_absolute() or ".." in module.parts or module.suffix != ".py":
                raise ValueError("Invalid grader module")
            if not str(self.evaluator.get("function", "")).isidentifier():
                raise ValueError("Invalid grader function")
            cases = self.evaluator.get("cases")
            if not isinstance(cases, list) or not 1 <= len(cases) <= 100 or any(not isinstance(c, dict) or "expected" not in c for c in cases):
                raise ValueError("Python evaluator requires independent hidden cases")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CampaignConfig:
    rounds: int = 20
    candidates: int = 2
    trials_per_task: int = 2
    baseline_evaluations: int = 3
    evaluation_parallelism: int = 1
    daily_budget_usd: float = 0
    trial_timeout_seconds: int = 600
    trial_memory_mb: int = 4096
    trial_cpus: float = 2
    framework_image: str = ""

    def __post_init__(self):
        for name in ("rounds", "candidates", "trials_per_task", "baseline_evaluations",
                     "evaluation_parallelism", "trial_timeout_seconds", "trial_memory_mb"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.candidates > 8 or self.trials_per_task < 2 or self.baseline_evaluations < 3:
            raise ValueError("Require 1..8 candidates, >=2 trials and >=3 baseline evaluations")
        if not math.isfinite(self.daily_budget_usd) or self.daily_budget_usd < 0:
            raise ValueError("Budget must be finite and >=0; zero means unlimited")
        if not math.isfinite(self.trial_cpus) or self.trial_cpus <= 0:
            raise ValueError("Trial CPUs must be positive")

    @classmethod
    def from_dict(cls, data: dict) -> "CampaignConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)
