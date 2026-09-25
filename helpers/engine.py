"""Campaign facade over vendored RRSI; operational adapters contain no new method."""
from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import math
from pathlib import Path
import re
import shutil
import threading

from usr.plugins.rrsi.helpers.artifacts import ArtifactStore, baseline_manifest
from usr.plugins.rrsi.helpers.broker import FrozenProvider, ModelBroker, SEARCH_ROLES
from usr.plugins.rrsi.helpers.contracts import CampaignConfig, identifier, UPSTREAM_COMMIT
from usr.plugins.rrsi.helpers.domain import AgentZeroDomain
from usr.plugins.rrsi.helpers.runtime import CALLBACKS, SYNC_HOOKS, CONFIG_KEYS, MODEL_TUNING
from usr.plugins.rrsi.helpers.state import StateStore, atomic_json, canonical_hash, utc_now
from usr.plugins.rrsi.vendor.rrsi import gitops as G
from usr.plugins.rrsi.vendor.rrsi.config import RRSIConfig
from usr.plugins.rrsi.vendor.rrsi.evaluate import EvalResult
from usr.plugins.rrsi.vendor.rrsi.llm import configure_provider
from usr.plugins.rrsi.vendor.rrsi.loop import Run
from usr.plugins.rrsi.vendor.rrsi.storage import HaltRun, atomic_text


def source_environment() -> dict:
    """Hash the ignored plugin code that a Git framework revision cannot bind."""
    root = Path(__file__).resolve().parents[1]
    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for folder in ("helpers", "vendor", "extensions", "api")
             for path in sorted((root / folder).rglob("*.py"))
             if not path.is_symlink()}
    return {"schema_version": 1, "files": files, "sha256": canonical_hash(files)}


def _campaign_paths(store, campaign_id):
    identifier(campaign_id)
    campaign = store.path(f"campaigns/{campaign_id}")
    if not (campaign / "campaign.json").is_file():
        raise ValueError("Unknown campaign")
    return campaign, campaign / "runs" / "agent_zero"


def status(store: StateStore, campaign_id: str) -> dict:
    campaign, runs = _campaign_paths(store, campaign_id)
    frontier = json.loads((runs / "frontier.json").read_text()) if (runs / "frontier.json").exists() else None
    return {"campaign_id": campaign_id, "frontier": frontier,
            "rounds_completed": len(frontier["trajectory"]) - 1 if frontier else 0,
            "engine": store.read_json(f"campaigns/{campaign_id}/engine.json", {}),
            "calibration": json.loads((runs / "calibration.json").read_text()) if (runs / "calibration.json").exists() else None,
            "source_revision": UPSTREAM_COMMIT}


class CampaignEngine:
    """One serialized worker session; provider and sandbox may be injected by tests."""
    def __init__(self, store: StateStore, campaign_id: str, *, checkpoint=None,
                 cancelled=None, idle=None, provider=None, sandbox=None, grader=None):
        self.store, self.campaign_id = store, identifier(campaign_id)
        self.campaign, self.runs_path = _campaign_paths(store, campaign_id)
        self.metadata = json.loads((self.campaign / "campaign.json").read_text())
        self.raw_config = json.loads((self.campaign / "config.json").read_text())
        self.config = CampaignConfig.from_dict(self.raw_config)
        self.tasks = json.loads((self.campaign / "tasks.json").read_text())
        self.checkpoint = checkpoint or (lambda: None)
        self.cancelled = cancelled or (lambda: False)
        self.idle = idle or (lambda: True)
        self.runtime_config = self.raw_config.get("runtime_config", {})
        self.environment = source_environment()
        self.repo = self.campaign / "experiment"
        self.provider = provider
        self.sandbox = sandbox
        self.grader = grader
        self.broker = self.domain = self.run = None
        self._prepared = False
        self._search_token = None
        self._input_hash = canonical_hash({"config": self.raw_config, "tasks": self.tasks,
                                          "framework_revision": self.metadata["framework_revision"],
                                          "starting_version": self.metadata.get("starting_version", "baseline")})

    def check(self):
        if self.cancelled():
            raise HaltRun("Campaign stopped by its controller")
        try:
            self.checkpoint()
        except HaltRun:
            raise
        except Exception as exc:
            raise HaltRun("Campaign interrupted by its controller") from exc
        current = {"config": json.loads((self.campaign / "config.json").read_text()),
                   "tasks": json.loads((self.campaign / "tasks.json").read_text()),
                   "framework_revision": self.metadata["framework_revision"],
                   "starting_version": self.metadata.get("starting_version", "baseline")}
        if canonical_hash(current) != self._input_hash:
            raise ValueError("Frozen campaign inputs changed")
        if source_environment() != self.environment:
            raise ValueError("Frozen plugin source changed; create a new campaign")

    def _progress(self, **values):
        relative = f"campaigns/{self.campaign_id}/engine.json"
        current = self.store.read_json(relative, {})
        self.store.write_json(relative, {**current, **values, "updated_at": utc_now()})

    def _setup_repo(self):
        if (self.repo / ".git").is_dir():
            receipt = self.store.read_json(f"campaigns/{self.campaign_id}/repository.json")
            if not receipt or receipt.get("input_hash") != self._input_hash:
                raise ValueError("Experiment repository does not match its frozen inputs")
            return
        if self.repo.exists() and any(self.repo.iterdir()):
            raise ValueError("Unrecognized incomplete experiment repository")
        staging = self.campaign / "experiment-staging"
        receipt = self.store.read_json(f"campaigns/{self.campaign_id}/repository.json")
        if receipt and staging.is_dir():
            if receipt.get("input_hash") != self._input_hash or G.rev(staging, "HEAD") != receipt.get("initial_commit"):
                raise ValueError("Interrupted setup does not match its frozen receipt")
            staging.rename(self.repo)
            return
        if staging.exists():
            # This directory is exclusively generated by a previous interrupted
            # setup before any candidate or model work; keep it for inspection.
            staging.rename(self.campaign / ("incomplete-setup-" + __import__("uuid").uuid4().hex))
        harness = staging / "domains/agent_zero/harness"
        harness.mkdir(parents=True, mode=0o700)
        version = self.metadata.get("starting_version", "baseline")
        if version == "baseline":
            atomic_json(harness / "manifest.json", baseline_manifest(self.metadata["framework_revision"]))
        else:
            source = self.store.artifact_path(version)
            ArtifactStore(self.store, self.metadata["framework_revision"]).materialize(source)
            for p in source.rglob("*"):
                if p.is_file():
                    dest = harness / p.relative_to(source)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(p, dest)
            manifest = json.loads((harness / "manifest.json").read_text())
            manifest.pop("version", None)
            manifest.pop("files", None)
            atomic_json(harness / "manifest.json", manifest)
        contract = self._constitution()
        atomic_text(harness.parent / "SKILL.md", contract)
        atomic_text(harness.parent / "PATTERNS.md", "# General mechanisms\nPrefer the smallest attributable change supported by actual traces. Preserve success habits, bounded termination, tool policies, and isolation. Add general capabilities only; remove machinery flagged by measured recent yield when justified.\n")
        G.git(staging, "init", "-q", check=True)
        G.git(staging, "add", "domains", check=True)
        G.git(staging, "commit", "-q", "-m", "RRSI frozen starting harness", check=True)
        self.store.write_json(f"campaigns/{self.campaign_id}/repository.json",
                              {"input_hash": self._input_hash, "initial_commit": G.rev(staging, "HEAD"),
                               "upstream_revision": UPSTREAM_COMMIT})
        staging.rename(self.repo)

    def _constitution(self):
        return "\n".join([
            "# Agent Zero RRSI overlay contract",
            "The editable harness is manifest.json plus assets. The framework, evaluation tasks/graders, model identity, budget, sandbox and broker remain fixed. No task-specific values or runtime data may persist across trials.",
            "manifest.json fields: schema_version=1; compatibility.framework_revision remains unchanged; callbacks, prompts, config, tools, skills and roles are objects. Do not author version/files: publication computes hashes.",
            "callbacks maps hook -> runtime/file.py:symbol. Functions accept agent, runtime, artifact and the available hook keyword arguments; use **kwargs for compatibility. Imports may use artifact-relative modules or existing Agent Zero modules. A callback must keep bounded termination and tool policy intact.",
            "Synchronous hooks: " + ", ".join(sorted(SYNC_HOOKS)),
            "Available hooks: " + ", ".join(sorted(CALLBACKS)),
            "prompts maps existing Markdown prompt basename to an artifact-relative text file. Tools map new names (excluding response, skills_tool, call_subordinate) to {implementation:'runtime/file.py:ToolSubclass', prompt:'...', schema:{type:'object',properties:{...}}}. Tool classes use helpers.tool.Tool and the existing Response contract.",
            "skills maps kebab names to {path:'runtime/skills/name/SKILL.md'}; roles maps names to {base_profile:'default',system:'general role instructions',config:{...}}. Skills and roles must be used by the harness to earn credit.",
            "config is plugin -> sparse permitted settings. Allowed settings: " + json.dumps({k: sorted(v) for k, v in CONFIG_KEYS.items()}),
            "_model_config may tune only chat_model/utility_model context ratios " + ", ".join(sorted(MODEL_TUNING)) + "; provider/model/API transport/credentials cannot change.",
            "All nine component types remain editable: prompt; control_flow via lifecycle callbacks; config; output_plumbing; context_mgmt; client_tool; skill; memory via generic procedures and local state; subagent via roles/callbacks. Runtime data is reset for every evaluation trial.",
            "No extensions directories, hooks.py, symlinks, grader reads, credential reads, network connections or filesystem escapes. Runtime Python exists only below runtime/. Use source files <=4MiB and at most512 files. Keep all source paths relative.",
        ]) + "\n"

    def prepare(self):
        if self._prepared:
            self.check()
            return
        self.check()
        environment_path = self.campaign / "environment.json"
        if environment_path.exists() and json.loads(environment_path.read_text()) != self.environment:
            raise ValueError("Frozen plugin source changed; create a new campaign")
        atomic_json(environment_path, self.environment)
        self._setup_repo()
        self.provider = self.provider or FrozenProvider.from_agent_zero(overrides=self.raw_config.get("model_roles"), store=self.store)
        identity = self.provider.identity()
        model_path = self.campaign / "models.json"
        descriptor = {"identity": identity, "sha256": canonical_hash(identity)}
        if model_path.exists() and json.loads(model_path.read_text()) != descriptor:
            raise ValueError("Frozen model identity changed; create a new campaign")
        atomic_json(model_path, descriptor)
        self.broker = ModelBroker(self.store, self.provider, daily_limit=self.config.daily_budget_usd,
                                  pricing=self.raw_config.get("pricing"), cancelled=self.cancelled)
        self._search_token = self.broker.grant(set(SEARCH_ROLES), ttl=7 * 86400)
        if self.sandbox is None:
            from usr.plugins.rrsi.helpers.sandbox import DockerSandbox
            self.sandbox = DockerSandbox(self.store, self.config, Path(__file__).resolve().parents[4],
                                         cancelled=self.cancelled, idle=self.idle)
        try:
            receipt = self.sandbox.prepare(self.broker.start(host="0.0.0.0"))
            if receipt.get("framework_revision") != self.metadata["framework_revision"]:
                raise ValueError("Frozen framework revision changed")
            pinned_image = receipt.get("image")
            image_path = self.campaign / "sandbox.json"
            if image_path.exists() and json.loads(image_path.read_text()) != receipt:
                raise ValueError("Frozen sandbox identity changed")
            atomic_json(image_path, receipt)
            self.domain = AgentZeroDomain(store=self.store, campaign_id=self.campaign_id, repo=self.repo,
                config=self.config, task_snapshot=self.tasks, model_identity=identity,
                broker=self.broker, sandbox=self.sandbox, checkpoint=self.check,
                cancelled=self.cancelled, runtime_config=self.runtime_config, grader=self.grader)
            if not self.domain.evolve_ids() or not self.domain.heldout_ids():
                raise ValueError("Campaign requires separate evolve and heldout task families")
            scientific = dict(self.raw_config.get("rrsi") or {})
            permitted = {"b_min", "b_max", "w", "m_draft", "delta_z", "beta0", "beta1", "w_s", "w_c", "w_n", "n_prune", "repair_rounds", "n_fail_traces", "n_success_traces"}
            if not set(scientific) <= permitted:
                raise ValueError("Unsupported scientific parameter")
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in scientific.values()):
                raise ValueError("Scientific parameters must be finite nonnegative numbers")
            cfg = RRSIConfig(T=self.config.rounds, k=self.config.trials_per_task,
                             m=self.config.candidates, eval_parallel=self.config.evaluation_parallelism,
                             delta=None, invalid_missing_frac=0.0, **scientific)
            for field in ("b_min", "b_max", "w", "m_draft", "n_prune", "repair_rounds", "n_fail_traces", "n_success_traces"):
                if type(getattr(cfg, field)) is not int:
                    raise ValueError(f"{field} must be an integer")
            if not 1 <= cfg.b_min <= cfg.b_max or cfg.w < 1 or cfg.n_prune < 1 or not 0 <= cfg.m_draft <= cfg.m or cfg.n_fail_traces < 1:
                raise ValueError("Invalid scientific window or edit budget")
            self.run = Run(self.domain, cfg, self.repo, self.campaign / "runs")
            self._recover()
            configure_provider(self._generate)
            self._prepared = True
            self._progress(source_revision=UPSTREAM_COMMIT, input_hash=self._input_hash,
                           model_hash=descriptor["sha256"], sandbox_image=pinned_image)
        except BaseException:
            self.close()
            raise

    def _generate(self, *, prompt, system=None, json_only=False, model=None,
                  max_tokens=20000, cache_prefix=None, role="proposer"):
        self.check()
        if role not in SEARCH_ROLES:
            raise ValueError("Unknown scientific role")
        if json_only:
            system = (system or "") + "\nOutput ONLY a single valid JSON object. No prose or markdown fences."
        messages = ([{"role": "system", "content": system}] if system else [])
        messages += [{"role": "user", "content": (cache_prefix + "\n\n" if cache_prefix else "") + prompt}]
        result = self.broker.request(self._search_token, {"role": role, "messages": messages, "max_tokens": max_tokens})
        text = result.get("response")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Scientific provider returned no text")
        return text

    @property
    def _journal_path(self):
        return self.campaign / "engine-journal.json"

    def _recover(self):
        if not self._journal_path.exists():
            return
        journal = json.loads(self._journal_path.read_text())
        if journal.get("state") != "running":
            return
        current = G.rev(self.repo, self.run.branch) if G.branch_exists(self.repo, self.run.branch) else None
        old = journal.get("branch_before")
        known = {old, self.store.read_json(f"campaigns/{self.campaign_id}/repository.json")["initial_commit"]}
        for prep in self.runs_path.glob("r*/[A-H]/prep.json"):
            data = json.loads(prep.read_text())
            if data.get("commit"):
                known.add(data["commit"])
        if current is not None and current not in known:
            raise ValueError("Experiment ref changed outside the journal; recovery stopped")
        if old:
            G.git(self.repo, "update-ref", f"refs/heads/{self.run.branch}", old,
                  current or "0" * 40, check=True)
        elif current:
            G.git(self.repo, "update-ref", "-d", f"refs/heads/{self.run.branch}", current, check=True)
        for filename, content in journal["before"].items():
            path = self.runs_path / filename
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_text(path, content)
        atomic_json(self._journal_path, {**journal, "state": "recovered", "recovered_at": utc_now()})
        self.store.event("science_recovered", campaign_id=self.campaign_id, action=journal["action"])

    @contextmanager
    def _transaction(self, action):
        self._recover()
        files = ("frontier.json", "history.jsonl", "attribution.jsonl", "global_analysis.json", "calibration.json")
        before = {name: (self.runs_path / name).read_text() if (self.runs_path / name).exists() else None for name in files}
        journal = {"state": "running", "action": action, "started_at": utc_now(), "before": before,
                   "branch_before": G.rev(self.repo, self.run.branch) if G.branch_exists(self.repo, self.run.branch) else None}
        atomic_json(self._journal_path, journal)
        try:
            yield
        except BaseException:
            self._recover()
            raise
        else:
            atomic_json(self._journal_path, {"state": "committed", "action": action, "finished_at": utc_now()})

    def baseline(self):
        self.prepare()
        with self._transaction("baseline"):
            if not self.run.frontier_path.exists():
                self.run.baseline("base")
            frontier = self.run.frontier()
            if len(frontier["trajectory"]) > 1:
                raise ValueError("Baseline is immutable after evolution begins")
            jobs = ["base"]
            for n in range(1, self.config.baseline_evaluations):
                self.check()
                label = f"calibration_{n}"
                job = f"heldout_{label}"
                path = self.run.eval_path(job)
                ev = EvalResult.load(path) if path.exists() else self.run.heldout(
                    label, self.domain.evolve_ids(), ref=frontier["trajectory"][0]["commit"])
                if ev.missing or not ev.extra.get("usage_complete"):
                    raise ValueError("Base calibration evaluation lacks complete usage")
                jobs.append(job)
            self._progress(baseline_jobs=jobs)
        return {**status(self.store, self.campaign_id), "baseline_jobs": jobs}

    def calibrate(self, jobs=None):
        self.prepare()
        known = self.store.read_json(f"campaigns/{self.campaign_id}/engine.json", {}).get("baseline_jobs", [])
        jobs = known if jobs is None else jobs
        if not isinstance(jobs, list) or len(jobs) < self.config.baseline_evaluations or set(jobs) != set(known):
            raise ValueError("Calibrate from the complete frozen baseline measurements")
        with self._transaction("calibrate"):
            result = self.run.calibrate(jobs)
        return {"campaign_id": self.campaign_id, "calibration": result}

    def round(self, t):
        self.prepare()
        if type(t) is not int or not 0 <= t < self.config.rounds:
            raise ValueError("Invalid round index")
        settled = len(self.run.frontier()["trajectory"]) - 1
        if t < settled:
            return {**status(self.store, self.campaign_id), "already_settled": True}
        if t != settled:
            raise ValueError("Run the next consecutive round")
        with self._transaction("round"):
            self.run.round(t)
        result = status(self.store, self.campaign_id)
        result["decisions"] = json.loads((self.runs_path / f"r{t}/decisions.json").read_text())
        return result

    def heldout(self, label, split="heldout", ref=None):
        self.prepare()
        identifier(label)
        ids = self.domain.ids_for_split(split)
        if not ids:
            raise ValueError("Selected task split is empty")
        known = {x["commit"] for x in self.run.frontier()["trajectory"]}
        if ref is not None and ref not in known:
            raise ValueError("Reference must be a recorded incumbent commit")
        with self._transaction("heldout"):
            result = self.run.heldout(label, ids, ref=ref)
        return {"campaign_id": self.campaign_id, "split": split, "evaluation": result.to_json()}

    def _latest(self, t):
        if type(t) is not int or t != len(self.run.frontier()["trajectory"]) - 2 or t < 0:
            raise ValueError("Only the latest settled round may be re-adjudicated or re-evaluated")

    def readjudicate(self, t):
        self.prepare()
        self._latest(t)
        with self._transaction("readjudicate"):
            self.run.readjudicate(t)
        return status(self.store, self.campaign_id)

    def reevaluate(self, t, variants=None):
        self.prepare()
        self._latest(t)
        labels = list("ABCDEFGH"[:self.config.candidates])
        if variants is not None and (not isinstance(variants, list) or not variants or any(v not in labels for v in variants) or len(set(variants)) != len(variants)):
            raise ValueError("Unknown or duplicate candidate variant")
        with self._transaction("reevaluate"):
            for variant in variants or labels:
                # Original attempts remain in immutable per-trial attempt directories.
                # Drop only the current record pointer to request an actual new trial.
                for record in (self.run.jobs / f"r{t}{variant}").glob("*/t*/record.json"):
                    record.unlink()
            self.run.reevaluate(t, variants)
        return status(self.store, self.campaign_id)

    def close(self):
        configure_provider(None)
        try:
            if self.sandbox:
                self.sandbox.close()
        finally:
            if self.broker:
                self.broker.close()
        self._prepared = False

    def final_artifact(self):
        """Materialize the settled native incumbent; canaries own activation."""
        self.prepare()
        frontier = self.run.frontier()
        if len(frontier["trajectory"]) - 1 != self.config.rounds:
            raise ValueError("Complete every configured round before deployment")
        ref = frontier["trajectory"][-1]["commit"]
        path, branch = self.campaign / "final-materialization", "deployment/agent_zero"
        G.worktree_add(self.repo, path, branch, ref)
        try:
            return ArtifactStore(self.store, self.metadata["framework_revision"]).materialize(
                self.domain.harness_dir(path))
        finally:
            G.worktree_remove(self.repo, path, branch)


_SESSIONS = {}
_SESSION_LOCK = threading.Lock()


def active_session(store: StateStore, campaign_id: str) -> CampaignEngine:
    with _SESSION_LOCK:
        engine = _SESSIONS.get((str(store.root), identifier(campaign_id)))
        if engine is None or not engine._prepared:
            raise ValueError("No prepared campaign worker session")
        return engine


def operate(action: str, campaign_id: str, *, store: StateStore, checkpoint=None,
            cancelled=None, idle=None, **arguments) -> dict:
    """Synchronous scientific operation; the controller owns jobs and lifecycle."""
    identifier(campaign_id)
    allowed = {"baseline": set(), "calibrate": {"jobs"}, "round": {"t"},
               "heldout": {"label", "split", "ref"}, "readjudicate": {"t"},
               "reevaluate": {"t", "variants"}, "status": set(), "close": set()}
    if action not in allowed or not set(arguments) <= allowed[action]:
        raise ValueError("Unknown scientific operation or arguments")
    if action == "status":
        return status(store, campaign_id)
    key = (str(store.root), campaign_id)
    with store.lock(f"science-{campaign_id}"):
        with _SESSION_LOCK:
            engine = _SESSIONS.get(key)
            if action == "close":
                if engine:
                    engine.close()
                    del _SESSIONS[key]
                return {"campaign_id": campaign_id, "closed": True}
            if engine is None:
                engine = _SESSIONS[key] = CampaignEngine(store, campaign_id, checkpoint=checkpoint,
                                                        cancelled=cancelled, idle=idle)
        return getattr(engine, action)(**arguments)
