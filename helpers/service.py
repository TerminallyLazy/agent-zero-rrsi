"""Native RRSI control plane; scientific decisions stay in the upstream engine."""
from __future__ import annotations

from collections import Counter
import atexit
import importlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Callable

from usr.plugins.rrsi.helpers.budget import BudgetLedger
from usr.plugins.rrsi.helpers.contracts import CampaignConfig, CampaignRecord, TaskPack, identifier, UPSTREAM_COMMIT
from usr.plugins.rrsi.helpers.state import BusyError, StateStore, canonical_hash, utc_now
from usr.plugins.rrsi.helpers.supervisor import ProcessSupervisor, update_campaign
from usr.plugins.rrsi.helpers.tasks import TaskRepository, sanitize

SCIENTIFIC = {"baseline", "calibrate", "round", "heldout", "readjudicate", "reevaluate"}
RESUMABLE = {"interrupted", "failed", "stopped", "paused", "waiting_for_idle", "awaiting_canary"}
CONFIG_KEYS = set(CampaignConfig.__dataclass_fields__) | {
    "enabled", "automatic_capture", "automatic_campaigns", "automatic_activation",
    "hourly_interval_seconds", "minimum_new_tasks", "model_roles", "pricing",
    "raw_capture_retention_days", "sanitized_task_retention_days"}


def display_evidence(value):
    """Sanitize strings without reparsing possibly modified JSON syntax."""
    if isinstance(value, str):
        scan = sanitize(value)
        return "[QUARANTINED_SENSITIVE_CONTENT]" if scan["quarantined"] else scan["text"]
    if isinstance(value, dict):
        return {str(k): display_evidence(v) for k, v in value.items()}
    if isinstance(value, list):
        return [display_evidence(v) for v in value]
    return value


def validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict) or set(settings) - CONFIG_KEYS:
        raise ValueError("Unknown RRSI configuration field")
    result = dict(settings)
    CampaignConfig.from_dict(result)
    for key in ("enabled", "automatic_capture", "automatic_campaigns", "automatic_activation"):
        if key in result and type(result[key]) is not bool:
            raise ValueError("RRSI toggles must be booleans")
    for key, default, minimum in (("hourly_interval_seconds", 3600, 3600),
                                  ("minimum_new_tasks", 10, 10),
                                  ("raw_capture_retention_days", 0, 0),
                                  ("sanitized_task_retention_days", 30, 1)):
        value = result.get(key, default)
        if type(value) is not int or value < minimum:
            raise ValueError(f"{key} must be an integer of at least {minimum}")
    pricing = result.get("pricing", {})
    roles = {"policy", "utility", "vision", "proposer", "analyst", "critic", "digester"}
    if not isinstance(pricing, dict) or set(pricing) - (roles | {"embedding"}):
        raise ValueError("Unknown pricing role")
    for price in pricing.values():
        if not isinstance(price, dict) or set(price) != {"input_per_million", "output_per_million"}:
            raise ValueError("Prices require input_per_million and output_per_million")
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in price.values()):
            raise ValueError("Prices must be finite and nonnegative")
    overrides = result.get("model_roles", {})
    if not isinstance(overrides, dict) or set(overrides) - roles:
        raise ValueError("Unknown model role")
    for model in overrides.values():
        if not isinstance(model, dict) or set(model) - {"provider", "name", "ctx_length", "vision", "kwargs"}:
            raise ValueError("Model overrides must not contain credentials or endpoints")
        kwargs = model.get("kwargs", {})
        if not isinstance(kwargs, dict) or set(kwargs) - {"temperature", "top_p", "seed", "max_tokens", "max_output_tokens", "reasoning_effort", "a0_api_mode"}:
            raise ValueError("Unsupported model parameter override")
    image = result.get("framework_image", "")
    if not isinstance(image, str) or len(image) > 256 or re.search(r"[\s\x00]", image):
        raise ValueError("Invalid framework image reference")
    return result


def native_config() -> dict:
    from helpers import plugins
    if plugins.get_plugin_meta("rrsi") is None:
        return {"enabled": False}
    config = plugins.get_plugin_config("rrsi") or {}
    result = validate_settings(config)
    result["enabled"] = bool(result.get("enabled", False) and plugins.get_toggle_state("rrsi") == "enabled")
    return result


def native_idle() -> bool:
    from agent import AgentContext
    for context in AgentContext.all():
        data = getattr(context, "data", {}) or {}
        if data.get("rrsi_evaluation"):
            continue
        if context.is_running():
            return False
    return True


def freeze_runtime_config() -> dict:
    from helpers import plugins
    from usr.plugins.rrsi.helpers.runtime import CONFIG_KEYS
    result = {}
    for plugin, allowed in CONFIG_KEYS.items():
        resolved = plugins.get_plugin_config(plugin) or {}
        values = {key: resolved[key] for key in sorted(allowed) if key in resolved}
        if any(type(v) not in (bool, int, float) or (type(v) is float and not math.isfinite(v)) for v in values.values()):
            raise ValueError("Baseline runtime settings require finite numeric or boolean values")
        result[plugin] = values
    return result


class RRSIService:
    def __init__(self, store: StateStore | None = None, *, config: Callable[[], dict] = native_config,
                 idle: Callable[[], bool] = native_idle, supervisor=None, preflight: Callable | None = None):
        self.store = store or StateStore()
        self.config_provider, self.idle = config, idle
        self.supervisor = supervisor or ProcessSupervisor(self.store)
        self.tasks = TaskRepository(self.store)
        self.preflight_fn = preflight
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._guard = threading.RLock()

    def config(self) -> dict:
        return validate_settings(self.config_provider())

    def ensure_curated(self) -> None:
        curated = Path(__file__).resolve().parents[1] / "tasks" / "curated.json"
        if not curated.exists():
            return
        pack = json.loads(curated.read_text())
        digest = canonical_hash(pack)
        if self.store.read_json("curated-import.json", {}).get("sha256") != digest:
            self.tasks.import_pack(pack.get("tasks", []) if isinstance(pack, dict) else pack, source="curated")
            self.store.write_json("curated-import.json", {"sha256": digest, "at": utc_now()})

    def setup(self) -> dict:
        """Read-only environment checks; explicit setup never installs dependencies."""
        config = self.config()
        self.ensure_curated()
        if self.preflight_fn:
            receipt = self.preflight_fn(config)
        else:
            from usr.plugins.rrsi.helpers.sandbox import DockerSandbox
            from usr.plugins.rrsi.helpers.broker import FrozenProvider
            engine = importlib.import_module("usr.plugins.rrsi.helpers.engine")
            if not callable(getattr(engine, "operate", None)):
                raise ValueError("RRSI engine is unavailable")
            sandbox = DockerSandbox(self.store, CampaignConfig.from_dict(config), self.supervisor.framework_root)
            receipt = sandbox.preflight()
            receipt["models"] = FrozenProvider.from_agent_zero(overrides=config.get("model_roles"), store=self.store).identity()
            receipt["runtime_config"] = freeze_runtime_config()
            receipt["environment"] = engine.source_environment()
        if not isinstance(receipt, dict) or not receipt.get("image") or not receipt.get("framework_revision"):
            raise ValueError("Setup preflight returned incomplete environment evidence")
        receipt = {**receipt, "at": utc_now(), "config_hash": canonical_hash(config), "upstream_commit": UPSTREAM_COMMIT}
        self.store.write_json("setup.json", receipt)
        self.store.write_json("framework-identity.json", {
            "framework_revision": receipt["framework_revision"], "image": receipt["image"],
            "at": receipt["at"], "upstream_commit": UPSTREAM_COMMIT})
        self.store.event("setup_checked", framework_revision=receipt["framework_revision"], image=receipt["image"])
        return {"ready": True, "image": receipt["image"], "framework_revision": receipt["framework_revision"], "models": receipt.get("models", {})}

    def _new_campaign(self, operation: str = "campaign", args: dict | None = None) -> str:
        config = self.config()
        if not config.get("enabled", False):
            raise ValueError("Enable RRSI after completing setup")
        if not self.idle():
            raise BusyError("Agent Zero is busy; wait for foreground work to finish")
        # Resolve fresh image and model identity every campaign, never trust an old marker.
        self.setup()
        setup = self.store.read_json("setup.json")
        snapshot: TaskPack = self.tasks.snapshot()
        if not any(t["split"] == "evolve" for t in snapshot["tasks"]):
            raise ValueError("An independently scored evolve task is required")
        ident = "campaign-" + uuid.uuid4().hex
        config = {**CampaignConfig.from_dict(config).to_dict(),
                  "framework_image": setup["image"], "pricing": config.get("pricing", {}),
                  "model_roles": config.get("model_roles", {}), "runtime_config": setup.get("runtime_config", {})}
        record: CampaignRecord = {"id": ident, "status": "created", "created_at": utc_now(), "updated_at": utc_now(),
                  "kind": "campaign" if operation == "campaign" else "measurement",
                  "operation": operation, "operation_args": args or {}, "next_round": 0,
                  "framework_revision": setup["framework_revision"],
                  "starting_version": self.store.active_version(), "task_hash": snapshot["sha256"],
                  "task_ids": [t["id"] for t in snapshot["tasks"]],
                  "automatic_activation": self.config().get("automatic_activation", True),
                  "upstream_commit": UPSTREAM_COMMIT}
        self.store.write_json(f"campaigns/{ident}/config.json", config)
        if setup.get("models"):
            self.store.write_json(f"campaigns/{ident}/models.json", {
                "identity": setup["models"], "sha256": canonical_hash(setup["models"])})
        if setup.get("environment"):
            self.store.write_json(f"campaigns/{ident}/environment.json", setup["environment"])
        self.store.write_json(f"campaigns/{ident}/tasks.json", snapshot)
        self.store.write_json(f"campaigns/{ident}/campaign.json", record)
        self.store.write_json("latest-campaign.json", {"id": ident})
        self.store.event("campaign_created", campaign_id=ident, task_hash=snapshot["sha256"])
        return ident

    def launch(self, operation="campaign", campaign_id=None, args=None) -> dict:
        self.start_scheduler()
        with self._guard, self.store.lock("campaign-start", blocking=False):
            self.supervisor.recover()
            if self.supervisor.running():
                raise BusyError("One RRSI campaign may run at a time")
            if campaign_id is None:
                campaign_id = self._new_campaign(operation, args)
            else:
                identifier(campaign_id)
                if not self.config().get("enabled", False):
                    raise ValueError("Enable RRSI before resuming")
                if not self.store.read_json(f"campaigns/{campaign_id}/campaign.json"):
                    raise ValueError("Campaign does not exist")
                changes = {"operation": operation, "operation_args": args or {}}
                if operation == "campaign":
                    changes["kind"] = "campaign"
                update_campaign(self.store, campaign_id, **changes)
            self.store.write_json("activity.json", {"time": time.time(), "idle": bool(self.idle())})
            return self.supervisor.start(campaign_id)

    def _campaign(self, payload: dict) -> str:
        value = payload.get("campaign_id") or self.store.read_json("latest-campaign.json", {}).get("id")
        if not value:
            raise ValueError("No campaign selected")
        return identifier(value)

    def campaigns(self, limit=40) -> list[dict]:
        paths = sorted(self.store.path("campaigns").glob("*/campaign.json"), reverse=True)
        records = []
        for path in paths:
            record = self.store.read_json(path.relative_to(self.store.root))
            records.append(record)
        return sorted(records, key=lambda r: r.get("created_at", ""), reverse=True)[:limit]

    def status(self, campaign_id=None) -> dict:
        self.supervisor.recover()
        config = self.config()
        records = self.campaigns()
        selected = None
        if campaign_id:
            selected = self.store.read_json(f"campaigns/{identifier(campaign_id)}/campaign.json")
            if selected is None:
                raise ValueError("Campaign does not exist")
        elif records:
            selected = records[0]
        scientific = None
        last_operation = None
        if selected:
            from usr.plugins.rrsi.helpers.engine import status as engine_status
            scientific = engine_status(self.store, selected["id"])
            last_operation = self.store.read_json(f"campaigns/{selected['id']}/last-operation.json", {})
        tasks = self.tasks.list()
        captures = Counter()
        for path in self.store.path("captures").glob("*.json"):
            record = self.store.read_json(path.relative_to(self.store.root), {})
            captures[record.get("status", "unknown")] += 1
        return {"enabled": config.get("enabled", False), "worker_running": self.supervisor.running(),
                "idle": bool(self.idle()), "active_version": self.store.active_version(),
                "scheduler": self.store.read_json("scheduler.json", {}),
                "setup": self.store.read_json("setup.json", {}), "campaigns": records,
                "rollback": self.store.read_json("rollback-processed.json", {}),
                "campaign": selected, "scientific": display_evidence(scientific),
                "last_operation": display_evidence(last_operation),
                "task_counts": dict(Counter(t.split for t in tasks)),
                "capture_counts": dict(captures),
                "budget": BudgetLedger(self.store, config.get("daily_budget_usd", 0)).summary(),
                "pricing_roles": sorted(config.get("pricing", {})),
                "automatic_activation": config.get("automatic_activation", True)}

    def tick(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self.process_rollback()
        config = self.config()
        idle = bool(self.idle())
        self.store.write_json("activity.json", {"time": now, "idle": idle})
        self.supervisor.recover()
        if not config.get("enabled", False):
            if self.supervisor.running():
                self.supervisor.shutdown()
            return {"reason": "disabled"}
        if self.supervisor.running():
            return {"reason": "campaign_active"}
        if not config.get("automatic_campaigns", True):
            return {"reason": "automatic_campaigns_disabled"}
        schedule = self.store.read_json("scheduler.json", {})
        if now < schedule.get("next_check", 0):
            return {"reason": "not_due"}
        if not idle:
            return {"reason": "foreground_busy"}
        self.ensure_curated()
        self.tasks.expire_captures(config.get("sanitized_task_retention_days", 30))
        records = self.campaigns(limit=10000)
        unfinished = [r for r in records if r.get("status") in RESUMABLE]
        if unfinished:
            return {"reason": "campaign_needs_attention", "campaign_id": unfinished[0]["id"]}
        campaigns = [r for r in records if r.get("kind", r.get("operation")) == "campaign"]
        used = set(task for r in campaigns for task in r.get("task_ids", []))
        eligible = [t for t in self.tasks.list("evolve") if t.id not in used]
        reason = "insufficient_new_tasks"
        result = None
        if (not campaigns and eligible) or len(eligible) >= config.get("minimum_new_tasks", 10):
            try:
                result = self.launch()
                reason = "started"
            except (ValueError, BusyError, RuntimeError, ImportError, OSError) as exc:
                reason = "preflight_failed"
                self.store.event("automatic_campaign_blocked", error_type=type(exc).__name__)
        self.store.write_json("scheduler.json", {"last_check": now,
                             "next_check": now + config.get("hourly_interval_seconds", 3600),
                             "reason": reason, "new_eligible_tasks": len(eligible)})
        return result or {"reason": reason}

    def process_rollback(self) -> dict | None:
        """Consume a failed active version without reverting a newer activation."""
        with self.store.lock("runtime-rollback", blocking=True):
            request = self.store.read_json("rollback_requested.json")
            if not request:
                return None
            request_hash = canonical_hash(request)
            version = request.get("version")
            result = {"request_hash": request_hash, "version": version, "at": utc_now()}
            try:
                if version != self.store.active_version():
                    result["status"] = "superseded"
                else:
                    from usr.plugins.rrsi.helpers.artifacts import ArtifactStore
                    from usr.plugins.rrsi.helpers.runtime import Runtime
                    revision = Runtime(self.store).revision()
                    receipt = ArtifactStore(self.store, revision).rollback(
                        "Automatic rollback after harness runtime failure", expected_version=version)
                    result.update(status="processed", receipt=receipt)
                self.store.write_json("rollback-processed.json", result)
                current = self.store.read_json("rollback_requested.json")
                if current is not None and canonical_hash(current) == request_hash:
                    self.store.path("rollback_requested.json").unlink(missing_ok=True)
                self.store.event("runtime_rollback_processed", **result)
            except Exception as exc:
                # Retain an unsuccessful request for recovery on the next heartbeat.
                result.update(status="failed", error_type=type(exc).__name__)
                self.store.write_json("rollback-processed.json", result)
            return result

    def start_scheduler(self) -> None:
        with self._guard:
            if self._scheduler and self._scheduler.is_alive():
                return
            self._stop.clear()
            def watch():
                while not self._stop.is_set():
                    try:
                        self.tick()
                    except Exception as exc:
                        self.store.event("scheduler_error", error_type=type(exc).__name__)
                        # Unknown configuration/activity must never authorize evaluation.
                        self.store.write_json("activity.json", {"time": time.time(), "idle": False})
                    self._stop.wait(2)
            self._scheduler = threading.Thread(target=watch, name="rrsi-supervisor", daemon=True)
            self._scheduler.start()

    def shutdown(self) -> dict:
        self._stop.set()
        result = self.supervisor.shutdown()
        if self._scheduler and self._scheduler is not threading.current_thread():
            self._scheduler.join(timeout=15)
        return result

    def dispatch(self, action: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("Request body must be an object")
        if action == "status":
            return self.status(payload.get("campaign_id"))
        if action == "setup":
            return self.setup()
        if action == "start":
            return self.launch(campaign_id=payload.get("campaign_id"))
        if action in {"pause", "stop", "resume"}:
            campaign = self._campaign(payload)
            owner = self.store.read_json("worker.json", {}).get("campaign_id")
            if self.supervisor.running() and owner != campaign:
                raise BusyError("Another campaign owns the worker")
            if action == "stop" and owner == campaign:
                return self.supervisor.shutdown()
            if action == "resume":
                if self.supervisor.running():
                    if self.store.read_json("worker.json", {}).get("campaign_id") != campaign:
                        raise BusyError("Another campaign owns the worker")
                    return self.supervisor.request(campaign, "run")
                record = self.store.read_json(f"campaigns/{campaign}/campaign.json", {})
                return self.launch(record.get("operation", "campaign"), campaign_id=campaign,
                                   args=record.get("operation_args", {}))
            return self.supervisor.request(campaign, action)
        if action in SCIENTIFIC:
            args = {}
            if action == "calibrate" and payload.get("jobs") is not None:
                jobs = payload["jobs"]
                if (not isinstance(jobs, list) or not 3 <= len(jobs) <= 100
                        or any(not isinstance(job, str) for job in jobs) or len(set(jobs)) != len(jobs)):
                    raise ValueError("jobs must contain the complete unique baseline job identifiers")
                args["jobs"] = [identifier(job) for job in jobs]
            if action in {"round", "readjudicate", "reevaluate"}:
                t = payload.get("t")
                if type(t) is not int or t < 0:
                    raise ValueError("A nonnegative round t is required")
                args["t"] = t
            if action == "heldout":
                args["split"] = payload.get("split", "heldout")
                if args["split"] not in {"evolve", "heldout", "transfer"}:
                    raise ValueError("Unknown evaluation split")
                args["label"] = identifier(payload.get("label", "manual_heldout"))
                if payload.get("ref") is not None:
                    if not isinstance(payload["ref"], str) or not re.fullmatch(r"[0-9a-f]{7,40}", payload["ref"]):
                        raise ValueError("ref must be a saved immutable commit")
                    args["ref"] = payload["ref"]
            if action == "reevaluate" and payload.get("variants") is not None:
                values = payload["variants"]
                if not isinstance(values, list) or not 1 <= len(values) <= 8:
                    raise ValueError("variants must contain 1..8 candidate identifiers")
                args["variants"] = [identifier(v) for v in values]
            campaign = payload.get("campaign_id")
            if action != "baseline" and campaign is None:
                campaign = self._campaign(payload)
            return self.launch(action, campaign, args)
        if action == "tasks":
            return {"tasks": [{"id": t.id, "family": t.family, "split": t.split,
                               "source": t.source, "evaluator": t.evaluator["kind"]} for t in self.tasks.list()]}
        if action == "versions":
            versions = []
            for path in self.store.path("artifacts").glob("h_*/manifest.json"):
                record = self.store.read_json(path.relative_to(self.store.root), {})
                versions.append({"version": record.get("version"), "compatibility": record.get("compatibility"),
                                 "components": {k: sorted((record.get(k) or {}).keys()) for k in
                                                ("callbacks", "prompts", "config", "tools", "skills", "roles")}})
            return {"active": self.store.active_version(), "versions": versions}
        if action == "rollback":
            from usr.plugins.rrsi.helpers.artifacts import ArtifactStore
            from usr.plugins.rrsi.helpers.runtime import Runtime
            revision = Runtime(self.store).revision()
            return ArtifactStore(self.store, revision).rollback("User requested rollback")
        if action == "evidence":
            campaign = self._campaign(payload)
            kind = payload.get("kind", "summary")
            base = f"campaigns/{campaign}/"
            run = base + "runs/agent_zero/"
            relative = {"summary": base + "campaign.json", "last_operation": base + "last-operation.json",
                        "tasks": base + "tasks.json", "config": base + "config.json",
                        "frontier": run + "frontier.json", "calibration": run + "calibration.json"}.get(kind)
            if kind in {"decisions", "directives", "analysis", "diff", "proposal", "critic"}:
                t = payload.get("t")
                if type(t) is not int or t < 0 or t > 100000:
                    raise ValueError("A bounded nonnegative round t is required")
                suffix = {"decisions": "decisions.json", "directives": "directives.json",
                          "analysis": "analysis_report.json"}.get(kind)
                if suffix is None:
                    variant = payload.get("variant")
                    if variant not in tuple("ABCDEFGH"):
                        raise ValueError("A candidate variant A through H is required")
                    suffix = variant + "/" + {"diff": "diff.patch", "proposal": "proposal.json", "critic": "critic.json"}[kind]
                relative = run + f"r{t}/" + suffix
            if not relative:
                raise ValueError("Unknown evidence kind")
            path = self.store.path(relative)
            if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Evidence exceeds dashboard capacity; use the local campaign archive")
            data = path.read_text() if kind == "diff" and path.exists() else self.store.read_json(relative, {})
            if kind == "tasks":
                data = {"sha256": data.get("sha256"), "tasks": [{k: t[k] for k in
                    ("id", "family", "split", "source")} for t in data.get("tasks", [])]}
            # Display research output as inert text and sanitize accidental secret literals.
            data = display_evidence(data)
            return {"kind": kind, "data": data}
        raise ValueError("Unknown RRSI action")


_SERVICE: RRSIService | None = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> RRSIService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = RRSIService()
            atexit.register(_SERVICE.shutdown)
        return _SERVICE
