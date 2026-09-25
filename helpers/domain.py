"""Agent Zero's execution/evidence adapter for the pinned upstream RRSI Domain."""
from __future__ import annotations

import json
from pathlib import Path
import re
import uuid

from usr.plugins.rrsi.helpers.artifacts import ArtifactStore
from usr.plugins.rrsi.helpers.broker import POLICY_ROLES
from usr.plugins.rrsi.helpers.contracts import CampaignConfig, TaskSpec, TrialMeasurement, identifier
from usr.plugins.rrsi.helpers.state import StateStore, atomic_json, canonical_hash
from usr.plugins.rrsi.vendor.rrsi.domain import Domain
from usr.plugins.rrsi.vendor.rrsi.evaluate import TaskResult
from usr.plugins.rrsi.vendor.rrsi.storage import HaltRun


class AgentZeroDomain(Domain):
    name = "agent_zero"
    harness_path = "harness"
    source_exts = {".py", ".txt", ".md", ".json"}
    # Only changed lines establish category evidence; unchanged manifest keys
    # cannot earn structural novelty. Empty declarations are not capabilities.
    component_signals = [
        ("memory", [r'(?m)^[+-](?![+-]).*(?:"_memory"|\.remember\(|\.recall\()']),
        ("skill", [r'(?m)^[+-](?![+-]).*(?:"skills"\s*:\s*\{(?!\s*\})|SKILL\.md|skill_use)']),
        ("client_tool", [r'(?m)^[+-](?![+-]).*(?:"tools"\s*:\s*\{(?!\s*\})|class \w+\(Tool\)|tool_execute)']),
        ("subagent", [r'(?m)^[+-](?![+-]).*(?:"roles"\s*:\s*\{(?!\s*\})|call_subordinate|subagent)']),
        ("context_mgmt", [r'(?m)^[+-](?![+-]).*(?:compaction|ctx_history|prepare_prompt|history)']),
        ("output_plumbing", [r'(?m)^[+-](?![+-]).*(?:process_tools|truncat|message_loop_result)']),
        ("control_flow", [r'(?m)^[+-](?![+-]).*(?:"callbacks"\s*:\s*\{(?!\s*\})|message_loop|monologue|async def)']),
        ("config", [r'(?m)^[+-](?![+-]).*(?:"config"\s*:\s*\{(?!\s*\})|threshold|max_active_skills)']),
        ("prompt", [r'(?m)^[+-](?![+-]).*(?:"prompts"|system_prompt|\.md)']),
    ]
    briefs = {
        "analyst": "Analyze frozen-model Agent Zero trajectories and independent evaluator evidence; distinguish behavior, missing capabilities, and successful habits.",
        "digester": "Inspect the supplied Agent Zero trace only. The final GRADER section is independent evaluation evidence. Return source-anchored general mechanisms.",
        "critic": "The harness is an Agent Zero overlay. Reject task/family IDs, expected answers, grader access, token-accounting changes, credential access, cross-trial task memory, disabling response termination, or bypassing tool policy. Generic callbacks, tools, skills, memory procedures and subordinate roles are allowed when wired through manifest.json.",
        "proposer": "Improve Agent Zero through manifest.json and runtime assets. All nine RRSI component classes are available. Freeze provider identity, task sets, evaluator, broker, sandbox, resource limits and framework source. Follow the exact overlay contract in the constitution. New tools/skills/roles must be declared and reachable; unchanged model/task execution limits cannot buy improvement.",
    }

    def __init__(self, *, store: StateStore, campaign_id: str, repo: Path,
                 config: CampaignConfig, task_snapshot: dict, model_identity: dict,
                 broker, sandbox, checkpoint=None, cancelled=None, runtime_config=None, grader=None):
        self.store, self.campaign_id = store, identifier(campaign_id)
        self.root = repo / "domains" / self.name
        self.config, self.broker, self.sandbox = config, broker, sandbox
        self.checkpoint = checkpoint or (lambda: None)
        self.cancelled = cancelled or (lambda: False)
        self.runtime_config = runtime_config or {}
        self.model_identity = model_identity
        self.tasks = {value["id"]: TaskSpec(**value) for value in task_snapshot["tasks"]}
        self.suite_hash = canonical_hash(task_snapshot["tasks"])
        if task_snapshot.get("sha256") != self.suite_hash or len(self.tasks) != len(task_snapshot["tasks"]):
            raise ValueError("Frozen task snapshot is inconsistent")
        self.revision = store.read_json(f"campaigns/{campaign_id}/campaign.json")["framework_revision"]
        self.artifacts = ArtifactStore(store, self.revision)
        self.grader = grader
        self.critic_patterns = [
            (r"usage\.sqlite|rrsi_receipt|broker_token|/rrsi-(?:input|output|broker)|/var/run/docker\.sock",
             "reaches frozen evaluator or metering infrastructure"),
            (r"campaigns/|tasks\.json|grader\.py|test_oracle|expected_answer", "grader or frozen task access"),
        ] + [(rf"(?<![\w-]){re.escape(value)}(?![\w-])", "frozen task/family identity in diff")
             for value in sorted({*self.tasks, *(task.family for task in self.tasks.values())})]

    def _check(self):
        try:
            self.checkpoint()
        except HaltRun:
            raise
        except Exception as exc:
            raise HaltRun("Campaign stopped or suspended by its controller") from exc

    def evolve_ids(self):
        return [t.id for t in self.tasks.values() if t.split == "evolve"]

    def heldout_ids(self):
        return [t.id for t in self.tasks.values() if t.split == "heldout"]

    def ids_for_split(self, split):
        if split not in {"evolve", "heldout", "transfer"}:
            raise ValueError("Unknown evaluation split")
        return [t.id for t in self.tasks.values() if t.split == split]

    def smoke_ids(self, incumbent_per_task=None):
        ids = self.evolve_ids()
        if incumbent_per_task:
            ids.sort(key=lambda key: -incumbent_per_task[key].mean)
        return ids[:min(2, len(ids))]

    def _record_path(self, runs_dir, job, task_id, index):
        identifier(job)
        identifier(task_id)
        if type(index) is not int or index < 0:
            raise ValueError("Invalid trial index")
        return Path(runs_dir) / "jobs" / job / task_id / f"t{index}" / "record.json"

    def _usage(self, trial_id):
        result = self.broker.ledger.trial_usage(trial_id)
        receipts = result.get("receipts") or []
        complete = bool(result.get("complete") and receipts and result.get("tokens", 0) > 0)
        complete = complete and all(r.get("reported") is True and r.get("role") in POLICY_ROLES
                                    and r.get("trial_id") == trial_id for r in receipts)
        return {**result, "complete": complete}

    def run(self, root, runs_dir, job, ids, k, log_prefix=""):
        identifier(job)
        if type(k) is not int or k < 1 or not ids or len(set(ids)) != len(ids):
            raise ValueError("Evaluation requires unique tasks and positive trial count")
        if any(t not in self.tasks for t in ids):
            raise ValueError("Task is outside the frozen campaign")
        self._check()
        version, artifact = self.artifacts.materialize(self.harness_dir(root))
        manifest = {"harness_version": version, "suite_hash": self.suite_hash,
                    "model_hash": canonical_hash(self.model_identity), "ids": ids, "k": k,
                    "config_hash": canonical_hash(self.config.to_dict())}
        job_path = Path(runs_dir) / "jobs" / job
        marker = job_path / "manifest.json"
        if marker.exists() and json.loads(marker.read_text()) != manifest:
            raise ValueError("Saved evaluation belongs to a different frozen manifest")
        atomic_json(marker, manifest)
        for task_id in ids:
            for index in range(k):
                self._check()
                record_path = self._record_path(runs_dir, job, task_id, index)
                if record_path.exists():
                    previous = json.loads(record_path.read_text())
                    if previous.get("manifest_hash") != canonical_hash(manifest):
                        raise ValueError("Saved trial belongs to another evaluation")
                    if previous.get("complete") and self._usage(previous["trial_id"])["complete"]:
                        continue
                trial_id = f"{self.campaign_id}-{job}-{task_id}-{index}-{uuid.uuid4().hex}"
                token = self.broker.grant(set(POLICY_ROLES), trial_id=trial_id,
                                          ttl=self.config.trial_timeout_seconds + 120)
                attempt = record_path.parent / "attempts" / trial_id
                # The sandbox exclusively claims a new/empty output directory.
                # Keep pre-launch evidence beside it, never inside that directory.
                atomic_json(attempt.with_suffix(".started.json"),
                            {"trial_id": trial_id, "manifest_hash": canonical_hash(manifest)})
                result = {}
                try:
                    result = self.sandbox.trial(self.tasks[task_id], artifact, attempt,
                                                broker_token=token, trial_id=trial_id,
                                                model_identity=self.model_identity,
                                                runtime_config=self.runtime_config)
                except HaltRun:
                    raise
                except Exception as exc:
                    self._check()
                    result = {"valid": False, "response": "", "error": type(exc).__name__}
                finally:
                    self.broker.revoke(token)
                self._check()
                usage = self._usage(trial_id)
                trace_path = attempt / "trace.json"
                trace = json.loads(trace_path.read_text()) if trace_path.exists() else {}
                if self.grader is None:
                    from usr.plugins.rrsi.helpers.grader import grade_task
                    grader = grade_task
                else:
                    grader = self.grader
                grade = (grader(self.tasks[task_id], str(result.get("response", "")), attempt / "files",
                                self.config.framework_image, store=self.store,
                                cancelled=self.cancelled) if result.get("valid") else
                         {"reward": 0.0, "valid": True, "reason": result.get("error", "trial_failed")})
                # Missing metering/trace evidence invalidates the evaluation rather
                # than producing a free, unobservable apparent improvement.
                complete = bool(usage["complete"] and trace and grade.get("valid"))
                record: TrialMeasurement = {"task_id": task_id, "trial": index, "trial_id": trial_id,
                          "manifest_hash": canonical_hash(manifest), "harness_version": version,
                          "complete": complete, "runtime_valid": result.get("valid") is True,
                          "response": str(result.get("response", "")), "trace": trace,
                          "grade": grade, "usage": usage, "error": result.get("error")}
                atomic_json(attempt / "record.json", record)
                atomic_json(record_path, record)

    def score(self, runs_dir, job, ids, k):
        per, complete, valid, submitted = {}, True, 0, 0
        for task_id in ids:
            rewards, tokens, missing = [], [], 0
            for index in range(k):
                path = self._record_path(runs_dir, job, task_id, index)
                rec = json.loads(path.read_text()) if path.exists() else {}
                usage = self._usage(rec["trial_id"]) if rec.get("trial_id") else {"complete": False}
                ok = bool(rec.get("complete") and usage["complete"])
                reward = rec.get("grade", {}).get("reward", 0.0) if ok else 0.0
                if type(reward) not in (int, float) or not 0 <= reward <= 1:
                    raise ValueError("Evaluator returned an invalid reward")
                rewards.append(float(reward))
                tokens.append(usage["tokens"] if ok else None)
                missing += int(not ok)
                complete = complete and ok
                valid += int(rec.get("runtime_valid") is True)
                submitted += int(bool(rec.get("response")))
            per[task_id] = TaskResult(rewards=rewards, tokens=tokens, missing=missing)
        n = len(ids) * k
        return per, {"usage_complete": complete, "valid_rate": valid / n,
                     "no_submission_rate": 1 - submitted / n, "suite_hash": self.suite_hash,
                     "model_hash": canonical_hash(self.model_identity)}

    def guards(self, incumbent, candidate):
        issues = []
        if not candidate.extra.get("usage_complete") or candidate.C is None or candidate.missing:
            issues.append("All trials require authoritative complete policy usage")
        if candidate.extra.get("suite_hash") != incumbent.extra.get("suite_hash"):
            issues.append("Frozen task suite changed")
        if candidate.extra.get("model_hash") != incumbent.extra.get("model_hash"):
            issues.append("Frozen provider identity changed")
        if candidate.extra.get("valid_rate", 0) < incumbent.extra.get("valid_rate", 0):
            issues.append("Runtime validity regressed")
        return issues

    def load_trial(self, runs_dir, job, task_id, trial):
        path = self._record_path(runs_dir, job, task_id, trial)
        return json.loads(path.read_text()) if path.exists() else None

    def render_trace(self, rec, detail=False):
        messages = rec.get("trace", {}).get("messages", [])
        if not isinstance(messages, list):
            messages = [messages]
        lines = [f"[step {i}] {json.dumps(message, ensure_ascii=False)}" for i, message in enumerate(messages)]
        lines += ["=== FINAL RESPONSE ===", rec.get("response", ""), "=== GRADER ===",
                  json.dumps(rec.get("grade", {}), ensure_ascii=False), "=== USAGE ===",
                  json.dumps({k: rec.get("usage", {}).get(k) for k in ("complete", "tokens", "pending_calls")})]
        return "\n".join(lines)

    def task_row(self, task_id, rec, tr):
        return f"{task_id} | score={tr.mean:.5f} | runtime_valid={rec.get('runtime_valid')} | complete={rec.get('complete')}"

    def smoke(self, root, runs_dir, job, ids):
        try:
            self.artifacts.materialize(self.harness_dir(root))
            self.run(root, runs_dir, job, ids, 1)
            per, extra = self.score(runs_dir, job, ids, 1)
            ok = extra["usage_complete"] and extra["valid_rate"] == 1.0
            return ok, {"stage": "isolated_native_runtime", "n": len(ids), **extra}
        except HaltRun:
            raise
        except Exception as exc:
            return False, {"stage": "isolated_native_runtime", "error_type": type(exc).__name__}
