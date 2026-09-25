"""Owned campaign processes and durable, cooperative lifecycle control."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

from usr.plugins.rrsi.helpers.contracts import identifier
from usr.plugins.rrsi.helpers.state import BusyError, StateStore, utc_now

MODULE = "usr.plugins.rrsi.helpers.supervisor"
TERMINAL = {"completed", "failed", "stopped", "awaiting_canary"}


class CampaignStopped(RuntimeError):
    pass


def update_campaign(store: StateStore, campaign_id: str, **updates) -> dict:
    identifier(campaign_id)
    with store.lock("campaign-metadata", blocking=True):
        relative = f"campaigns/{campaign_id}/campaign.json"
        record = store.read_json(relative)
        if not isinstance(record, dict):
            raise ValueError("Campaign does not exist")
        record.update(updates, updated_at=utc_now())
        store.write_json(relative, record)
        return record


def checkpoint(store: StateStore, campaign_id: str) -> None:
    """Wait between evaluation/model units; stale foreground state fails closed."""
    last_state = None
    while True:
        control = store.read_json(f"campaigns/{campaign_id}/control.json", {})
        if control.get("request") == "stop":
            raise CampaignStopped("Campaign stopped")
        activity = store.read_json("activity.json", {})
        current = time.time()
        fresh = (isinstance(activity.get("time"), (int, float)) and
                 0 <= current - activity["time"] <= 30)
        state = "paused" if control.get("request") == "pause" else (
            "running" if fresh and activity.get("idle") is True else "waiting_for_idle")
        if state != last_state:
            update_campaign(store, campaign_id, status=state)
            last_state = state
        if state == "running":
            return
        time.sleep(0.5)


def worker_cancelled(store: StateStore, campaign_id: str) -> bool:
    return store.read_json(f"campaigns/{campaign_id}/control.json", {}).get("request") == "stop"


def worker_idle(store: StateStore, campaign_id: str) -> bool:
    control = store.read_json(f"campaigns/{campaign_id}/control.json", {})
    activity = store.read_json("activity.json", {})
    measured = activity.get("time")
    return (control.get("request") not in {"pause", "stop"} and
            type(measured) in (int, float) and 0 <= time.time() - measured <= 30 and
            activity.get("idle") is True)


def run_worker(store: StateStore, campaign_id: str, nonce: str,
               operate: Callable | None = None, activate: Callable | None = None) -> int:
    """Invoke bounded source-backed operations; never emulate scientific results."""
    identifier(campaign_id)
    owned = store.read_json("worker.json", {})
    if owned.get("campaign_id") != campaign_id or owned.get("nonce") != nonce:
        raise ValueError("Worker ownership receipt mismatch")
    operate = operate or importlib.import_module("usr.plugins.rrsi.helpers.engine").operate
    cp = lambda: checkpoint(store, campaign_id)
    control_callbacks = {"checkpoint": cp,
                         "cancelled": lambda: worker_cancelled(store, campaign_id),
                         "idle": lambda: worker_idle(store, campaign_id)}
    with store.lock("campaign", blocking=False):
        try:
            record = store.read_json(f"campaigns/{campaign_id}/campaign.json")
            config = store.read_json(f"campaigns/{campaign_id}/config.json")
            operation = record.get("operation", "campaign")
            args = record.get("operation_args", {})
            if operation != "campaign":
                cp()
                result = operate(operation, campaign_id, store=store, **control_callbacks, **args)
                store.write_json(f"campaigns/{campaign_id}/last-operation.json",
                                 {"operation": operation, "result": result, "at": utc_now()})
                update_campaign(store, campaign_id, status="completed", operation_result=result)
                return 0
            if not record.get("baseline_complete"):
                cp()
                result = operate("baseline", campaign_id, store=store, **control_callbacks)
                update_campaign(store, campaign_id, baseline_complete=True, baseline=result)
            if not record.get("calibration_complete"):
                cp()
                result = operate("calibrate", campaign_id, store=store, **control_callbacks)
                update_campaign(store, campaign_id, calibration_complete=True, calibration=result)
            record = store.read_json(f"campaigns/{campaign_id}/campaign.json")
            for t in range(int(record.get("next_round", 0)), int(config["rounds"])):
                cp()
                result = operate("round", campaign_id, store=store, **control_callbacks, t=t)
                update_campaign(store, campaign_id, next_round=t + 1, last_round=result)
            record = store.read_json(f"campaigns/{campaign_id}/campaign.json")
            if not record.get("heldout_complete"):
                evaluations = {}
                tasks = store.read_json(f"campaigns/{campaign_id}/tasks.json", {}).get("tasks", [])
                for split in ("heldout", "transfer"):
                    if any(task["split"] == split for task in tasks):
                        cp()
                        evaluations[split] = operate("heldout", campaign_id, store=store,
                                                      **control_callbacks, split=split, label=f"final_{split}")
                update_campaign(store, campaign_id, heldout_complete=True, heldout=evaluations)
            if not record.get("automatic_activation", True):
                update_campaign(store, campaign_id, status="completed", activation="disabled")
                return 0
            cp()
            try:
                if activate is None:
                    activate = importlib.import_module("usr.plugins.rrsi.helpers.canary").validate_and_activate
            except (ImportError, AttributeError):
                update_campaign(store, campaign_id, status="awaiting_canary", activation="canary_unavailable")
                return 0
            receipt = activate(store, campaign_id)
            if not isinstance(receipt, dict) or not receipt.get("version"):
                raise ValueError("Activation returned no version receipt")
            update_campaign(store, campaign_id, status="completed", activation=receipt)
            return 0
        except CampaignStopped:
            update_campaign(store, campaign_id, status="stopped")
            return 0
        except Exception as exc:
            if worker_cancelled(store, campaign_id):
                # Domain/Run layers preserve their own exception types. The
                # durable stop request determines lifecycle state across modules.
                update_campaign(store, campaign_id, status="stopped", stop_error_type=type(exc).__name__)
                store.event("campaign_stopped", campaign_id=campaign_id, error_type=type(exc).__name__)
                return 0
            # Provider exceptions can contain request headers; persist types only.
            reason = {
                "ContextCapacityError": "Complete research history exceeds the frozen model context capacity; use a larger-context model or a shorter campaign. No history was discarded.",
                "BudgetExceeded": "The daily spending cap was reached. Unsettled reservations remain charged until reconciled.",
                "UnknownPricing": "A positive spending cap requires trustworthy pricing before another paid call.",
            }.get(type(exc).__name__, "The scientific operation failed; inspect its local evidence before resuming.")
            update_campaign(store, campaign_id, status="failed", error_type=type(exc).__name__, error_reason=reason)
            store.event("campaign_failed", campaign_id=campaign_id, error_type=type(exc).__name__)
            return 1
        finally:
            try:
                operate("close", campaign_id, store=store)
            except Exception as exc:
                store.event("campaign_cleanup_failed", campaign_id=campaign_id, error_type=type(exc).__name__)


class ProcessSupervisor:
    def __init__(self, store: StateStore, framework_root: Path | None = None):
        self.store = store
        self.framework_root = (framework_root or Path(__file__).resolve().parents[4]).resolve()
        self.process: subprocess.Popen | None = None
        self._guard = threading.RLock()

    def _matches(self, receipt: dict) -> bool:
        pid = receipt.get("pid")
        nonce = receipt.get("nonce")
        if type(pid) is not int or pid <= 1 or not isinstance(nonce, str) or len(nonce) < 32:
            return False
        try:
            proc = Path(f"/proc/{pid}/cmdline")
            if proc.exists():
                args = proc.read_bytes().split(b"\0")
                return MODULE.encode() in args and nonce.encode() in args
            result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "args="],
                                    capture_output=True, text=True, timeout=5)
            return result.returncode == 0 and MODULE in result.stdout and f"--nonce {nonce}" in result.stdout
        except (OSError, subprocess.SubprocessError):
            return False

    def running(self) -> bool:
        with self._guard:
            if self.process is not None:
                if self.process.poll() is None:
                    return True
                self.process = None
            return self._matches(self.store.read_json("worker.json", {}))

    def recover(self) -> dict:
        receipt = self.store.read_json("worker.json", {})
        campaign = receipt.get("campaign_id")
        if not campaign:
            return {"state": "idle"}
        if self.running():
            return {"state": "running", "campaign_id": campaign}
        record = self.store.read_json(f"campaigns/{identifier(campaign)}/campaign.json", {})
        if record.get("status") not in TERMINAL | {"interrupted"}:
            update_campaign(self.store, campaign, status="interrupted", error_type="WorkerExited")
            self.store.event("campaign_interrupted", campaign_id=campaign)
        cleaned = self.store.read_json("worker-cleanup.json", {})
        if receipt.get("nonce") and cleaned.get("nonce") != receipt["nonce"]:
            cleanup = self.cleanup_resources()
            if not cleanup.get("failed_kinds"):
                self.store.write_json("worker-cleanup.json", {"nonce": receipt["nonce"], "at": utc_now()})
        return {"state": "stopped", "campaign_id": campaign}

    def cleanup_resources(self) -> dict:
        """Recover Docker resources only after independently verifying owner labels."""
        owners = [hashlib.sha256(str(root).encode()).hexdigest()[:16]
                  for root in (self.store.root,self.store.path("grader"))]
        removed, failures = 0, []
        for owner in owners:
            for kind in ("container", "volume"):
                listing = ["ps", "-aq"] if kind == "container" else ["volume", "ls", "-q"]
                try:
                    result = subprocess.run(["docker", *listing, "--filter", f"label=a0.rrsi.owner={owner}"],
                                            capture_output=True, text=True, timeout=10)
                    if result.returncode:
                        failures.append(kind)
                        continue
                    for name in result.stdout.splitlines():
                        # Values originate from Docker inventory, still reject flags/control characters.
                        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
                            continue
                        inspect = ["inspect", name] if kind == "container" else ["volume", "inspect", name]
                        check = subprocess.run(["docker", *inspect], capture_output=True, text=True, timeout=10)
                        if check.returncode:
                            continue
                        record = json.loads(check.stdout)[0]
                        labels = record.get("Config", {}).get("Labels", {}) if kind == "container" else record.get("Labels", {})
                        if (labels or {}).get("a0.rrsi.owner") != owner:
                            continue
                        command = ["rm", "-f", name] if kind == "container" else ["volume", "rm", name]
                        result = subprocess.run(["docker", *command], capture_output=True, text=True, timeout=15)
                        if result.returncode:
                            failures.append(kind)
                        else:
                            removed += 1
                except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
                    failures.append(kind)
        self.store.event("owned_resources_cleanup", removed=removed, failed_kinds=sorted(set(failures)))
        return {"removed": removed, "failed_kinds": sorted(set(failures))}

    def start(self, campaign_id: str) -> dict:
        identifier(campaign_id)
        with self._guard, self.store.lock("supervisor", blocking=False):
            if self.running():
                raise BusyError("One RRSI campaign may run at a time")
            record = self.store.read_json(f"campaigns/{campaign_id}/campaign.json")
            if not isinstance(record, dict):
                raise ValueError("Campaign does not exist")
            nonce = secrets.token_hex(24)
            receipt = {"campaign_id": campaign_id, "nonce": nonce, "at": utc_now()}
            self.store.write_json("worker.json", receipt)
            self.store.write_json(f"campaigns/{campaign_id}/control.json", {"request": "run", "at": utc_now()})
            update_campaign(self.store, campaign_id, status="starting")
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", MODULE, "--worker", campaign_id, "--nonce", nonce],
                    cwd=self.framework_root, env={**os.environ, "RRSI_STATE_DIR": str(self.store.root)},
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True)
                receipt["pid"] = self.process.pid
                self.store.write_json("worker.json", receipt)
            except Exception:
                update_campaign(self.store, campaign_id, status="failed", error_type="WorkerLaunchFailed")
                raise
            return {"campaign_id": campaign_id, "status": "starting"}

    def request(self, campaign_id: str, action: str) -> dict:
        identifier(campaign_id)
        if action not in {"run", "pause", "stop"}:
            raise ValueError("Unsupported worker request")
        if not self.store.read_json(f"campaigns/{campaign_id}/campaign.json"):
            raise ValueError("Campaign does not exist")
        self.store.write_json(f"campaigns/{campaign_id}/control.json", {"request": action, "at": utc_now()})
        return {"campaign_id": campaign_id, "request": action}

    def shutdown(self, timeout: float = 10) -> dict:
        with self._guard:
            receipt = self.store.read_json("worker.json", {})
            campaign = receipt.get("campaign_id")
            if campaign:
                self.request(campaign, "stop")
            deadline = time.monotonic() + timeout
            while self.running() and time.monotonic() < deadline:
                time.sleep(0.1)
            if self.running():
                # An owned Popen handle or a matching nonce is required before signals.
                if self.process is not None and self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill(); self.process.wait(timeout=5)
                elif self._matches(receipt):
                    try:
                        os.kill(receipt["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    end = time.monotonic() + 5
                    while self._matches(receipt) and time.monotonic() < end:
                        time.sleep(0.1)
                    if self._matches(receipt):
                        os.kill(receipt["pid"], signal.SIGKILL)
            if campaign:
                record = self.store.read_json(f"campaigns/{campaign}/campaign.json", {})
                if record.get("status") not in TERMINAL:
                    update_campaign(self.store, campaign, status="stopped", error_type="ServiceShutdown")
            stopped = not self.running()
            cleanup = self.cleanup_resources() if stopped else {"skipped": "worker_running"}
            return {"stopped": stopped, "campaign_id": campaign, "cleanup": cleanup}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args()
    def terminate(signum, frame):
        raise CampaignStopped("Service termination requested")
    signal.signal(signal.SIGTERM, terminate)
    return run_worker(StateStore(), args.worker, args.nonce)


if __name__ == "__main__":
    raise SystemExit(main())
