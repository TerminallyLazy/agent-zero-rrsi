"""Version-pinned native Agent Zero overlays.

Only the fixed installed extension shims are discovered by Agent Zero. Evolved
assets live in immutable, non-served artifact directories and are resolved here.
This is trusted Python plugin execution, not a sandbox for admitted artifacts.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import threading
import types
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PIN_KEY = "rrsi_harness_version"
ROLE_KEY = "rrsi_role"
SKILLS_KEY = "rrsi_loaded_skills"
VERSION_RE = re.compile(r"^[a-f0-9]{64}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
SYNC_HOOKS = frozenset({
    "agent_init", "read_prompt", "parse_prompt",
})
ASYNC_HOOKS = frozenset({
    "system_prompt", "monologue_start", "message_loop_start",
    "message_loop_prompts_before", "message_loop_prompts_after",
    "before_main_llm_call", "message_loop_result", "message_loop_end",
    "chat_model_call_before", "chat_model_call_after", "util_model_call_before",
    "util_model_call_after", "tool_execute_before", "tool_execute_after",
    "monologue_before", "monologue_after", "prepare_prompt_after",
    "call_chat_model_turn_after", "process_tools_before", "process_tools_after",
})
CALLBACKS = SYNC_HOOKS | ASYNC_HOOKS
# Provider identity and authority are not evolvable configuration. The callback
# runtime remains trusted code; this allowlist constrains the declarative seam.
CONFIG_PLUGINS = frozenset({"_memory", "_skills", "_chat_compaction", "_model_config"})
MODEL_TUNING = frozenset({"ctx_history", "ctx_input"})
CONFIG_KEYS = {
    "_skills": {"max_active_skills"},
    "_chat_compaction": {"use_chat_model"},
    "_memory": {
        "memory_recall_enabled", "memory_recall_delayed", "memory_recall_interval",
        "memory_recall_history_len", "memory_recall_memories_max_search",
        "memory_recall_solutions_max_search", "memory_recall_memories_max_result",
        "memory_recall_solutions_max_result", "memory_recall_similarity_threshold",
        "memory_recall_query_prep", "memory_recall_post_filter",
        "memory_memorize_enabled", "memory_memorize_consolidation",
        "memory_memorize_replace_threshold",
    },
}
_LOCAL = threading.local()


class RuntimeFailure(RuntimeError):
    """A pinned artifact failed; never silently substitute another version."""


def manifest_hash(manifest: dict, files: dict[str, str] | None = None) -> str:
    """Hash a manifest without its self-reference and, optionally, file hashes.

    Publishers should include a ``files`` mapping of relative path -> SHA-256 in
    the manifest. ``manifest.json`` itself must not appear in that mapping.
    """
    payload = copy.deepcopy(manifest)
    payload.pop("version", None)
    if files is not None:
        payload["files"] = files
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def artifact_file(root: Path, relative: str) -> Path:
    """Resolve an existing regular file without traversal or symlink aliases."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Artifact paths must be relative POSIX paths")
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(part in {".", "..", "extensions"} for part in parts):
        raise ValueError("Invalid artifact path")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Artifact symlinks are not supported")
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("Artifact file is missing or outside its root")
    return resolved


def _symbol_file(root: Path, reference: str, *, sync: bool = False, kind: str | None = None) -> tuple[Path, str]:
    if not isinstance(reference, str) or reference.count(":") != 1:
        raise ValueError("Implementation must be runtime/file.py:symbol")
    relative, symbol = reference.split(":")
    path = artifact_file(root, relative)
    if not relative.startswith("runtime/") or path.suffix != ".py" or not symbol.isidentifier():
        raise ValueError("Invalid runtime implementation reference")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    definition = next((node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol), None)
    if definition is None or (sync and isinstance(definition, ast.AsyncFunctionDef)):
        raise ValueError("Implementation symbol is missing or incompatible with a sync hook")
    if kind == "callback" and isinstance(definition, ast.ClassDef) or kind == "tool" and not isinstance(definition, ast.ClassDef):
        raise ValueError("Callbacks must be functions and tools must be classes")
    return path, symbol


def validate_manifest(root: Path, manifest: dict, expected_revision: str | None = None) -> dict:
    """Validate references and the fixed runtime interface without executing code."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported artifact schema")
    if not VERSION_RE.fullmatch(str(manifest.get("version", ""))):
        raise ValueError("Artifact version must be a SHA-256 hex identifier")
    compatibility = manifest.get("compatibility")
    if not isinstance(compatibility, dict) or not compatibility.get("framework_revision"):
        raise ValueError("Artifact requires a framework revision")
    if expected_revision is not None and compatibility["framework_revision"] != expected_revision:
        raise ValueError("Artifact framework revision is incompatible")
    for field in ("callbacks", "prompts", "config", "tools", "skills", "roles"):
        if not isinstance(manifest.get(field, {}), dict):
            raise ValueError(f"{field} must be an object")
    for hook, reference in manifest.get("callbacks", {}).items():
        if hook not in CALLBACKS:
            raise ValueError(f"Unsupported callback hook: {hook}")
        _symbol_file(root, reference, sync=hook in SYNC_HOOKS, kind="callback")
    for name, relative in manifest.get("prompts", {}).items():
        if Path(name).name != name or not name.endswith(".md"):
            raise ValueError("Prompt keys must be Markdown basenames")
        artifact_file(root, relative)
    for name, item in manifest.get("tools", {}).items():
        if not NAME_RE.fullmatch(name) or name in {"response", "skills_tool", "call_subordinate"}:
            raise ValueError("Invalid or reserved generated tool name")
        if not isinstance(item, dict) or not isinstance(item.get("prompt"), str):
            raise ValueError("Tools require a prompt and implementation")
        schema = item.get("schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("Tool schemas must describe objects")
        _symbol_file(root, item.get("implementation"), kind="tool")
    for name, item in manifest.get("skills", {}).items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name) or not isinstance(item, dict):
            raise ValueError("Invalid generated skill")
        path = artifact_file(root, item.get("path"))
        if path.name != "SKILL.md" or not str(item["path"]).startswith("runtime/"):
            raise ValueError("Generated skills require runtime/.../SKILL.md")
    for name, item in manifest.get("roles", {}).items():
        if not NAME_RE.fullmatch(name) or not isinstance(item, dict):
            raise ValueError("Invalid generated role")
        if not isinstance(item.get("base_profile", "default"), str) or not isinstance(item.get("system", ""), str) or not isinstance(item.get("config", {}), dict):
            raise ValueError("Invalid generated role fields")
    _validate_config(manifest.get("config", {}))
    for item in manifest.get("roles", {}).values():
        _validate_config(item.get("config", {}))
    hashes = manifest.get("files")
    if hashes is not None:
        if not isinstance(hashes, dict) or "manifest.json" in hashes:
            raise ValueError("Artifact files must map non-manifest paths to hashes")
        for relative, expected in hashes.items():
            path = artifact_file(root, relative)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Artifact content hash mismatch")
        if manifest_hash(manifest) != manifest["version"]:
            raise ValueError("Artifact manifest hash mismatch")
        referenced = set(manifest.get("prompts", {}).values())
        referenced.update(value.split(":")[0] for value in manifest.get("callbacks", {}).values())
        referenced.update(value["implementation"].split(":")[0] for value in manifest.get("tools", {}).values())
        referenced.update(value["path"] for value in manifest.get("skills", {}).values())
        if not referenced <= set(hashes):
            raise ValueError("Artifact hash inventory omits referenced files")
        on_disk = {str(path.relative_to(root)) for path in root.rglob("*")
                   if path.is_file() and path.name != "manifest.json" and "__pycache__" not in path.parts}
        if on_disk != set(hashes):
            raise ValueError("Artifact hash inventory must cover the complete payload")
    json.dumps(manifest, allow_nan=False)
    return copy.deepcopy(manifest)


def _validate_config(config: dict) -> None:
    # The declarative configuration namespace is plugin name -> sparse settings.
    for name, values in config.items():
        if name not in CONFIG_PLUGINS or not isinstance(values, dict):
            raise ValueError("Configuration may only tune memory, skills, compaction and model context")
        if name in CONFIG_KEYS and not set(values) <= CONFIG_KEYS[name]:
            raise ValueError("Configuration cannot change visibility policies or memory isolation")
        if name == "_model_config":
            for slot, tuning in values.items():
                if slot not in {"chat_model", "utility_model"} or not isinstance(tuning, dict) or not set(tuning) <= MODEL_TUNING:
                    raise ValueError("Model provider, model identity and credentials are fixed")
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 1 for v in tuning.values()):
                    raise ValueError("Model context ratios must be greater than zero and at most one")


@dataclass(frozen=True)
class Artifact:
    version: str
    root: Path
    manifest: dict

    def file(self, relative: str) -> Path:
        path = artifact_file(self.root, relative)
        expected = self.manifest.get("files", {}).get(relative)
        if expected is not None and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeFailure("An immutable artifact asset changed on disk")
        return path


def _context_value(context: Any, key: str, default: Any = None) -> Any:
    getter = getattr(context, "get_data", None)
    value = getter(key) if callable(getter) else getattr(context, "data", {}).get(key)
    return default if value is None else value


def _set_context_value(context: Any, key: str, value: Any) -> None:
    setter = getattr(context, "set_data", None)
    if callable(setter):
        setter(key, value)
    else:
        context.data[key] = value


class Runtime:
    def __init__(self, store: Any = None, framework_revision: str | None = None):
        if store is None:
            from usr.plugins.rrsi.helpers.state import StateStore
            store = StateStore()
        self.store = store
        self.framework_revision = framework_revision
        self._artifacts: dict[str, Artifact] = {}
        self._modules: dict[tuple[str, str], tuple[str, Any]] = {}
        self._lock = threading.RLock()

    def revision(self) -> str:
        if self.framework_revision is not None:
            return self.framework_revision
        if os.environ.get("RRSI_TRIAL") == "1" and os.environ.get("RRSI_FRAMEWORK_REVISION"):
            return os.environ["RRSI_FRAMEWORK_REVISION"]
        # Persisted setup receipts describe past observations. They cannot attest
        # the framework currently executing a previously accepted artifact.
        from helpers import files
        try:
            result = subprocess.run(["git", "-C", files.get_abs_path(), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        return result.stdout.strip() if result.returncode == 0 else "unavailable"

    def pin(self, agent: Any, *, new: bool = False, inherited: str | None = None) -> str:
        context = agent.context
        with self._lock:
            superior = getattr(agent, "data", {}).get("_superior")
            if inherited is None and superior is not None:
                inherited = self.pin(superior)
            current = _context_value(context, PIN_KEY)
            if inherited is not None:
                # A newly constructed native child can have been provisionally
                # pinned before core assigns its superior relationship.
                if current != inherited:
                    _set_context_value(context, PIN_KEY, inherited)
                return inherited
            if current is not None:
                if current != "baseline" and not VERSION_RE.fullmatch(str(current)):
                    raise RuntimeFailure("Invalid persisted RRSI artifact pin")
                return current
            version = "baseline"
            if new and self.enabled(agent):
                active = self.store.read_json("active.json", {})
                version = active.get("version", "baseline")
                created = getattr(context, "created_at", None)
                activated = active.get("at")
                # A restored empty chat has no logs to identify it as a restore;
                # its creation timestamp still predates this deployment.
                if isinstance(created, datetime) and isinstance(activated, str):
                    activation_time = datetime.fromisoformat(activated.replace("Z", "+00:00"))
                    if created.timestamp() < activation_time.timestamp():
                        version = "baseline"
            _set_context_value(context, PIN_KEY, version)
            return version

    def enabled(self, agent: Any) -> bool:
        # DockerSandbox supplies this only to the disposable, network-disabled
        # framework process; it never changes an installed owner's config.
        if os.environ.get("RRSI_TRIAL") == "1":
            return True
        # Prevent recursive get_plugin_config -> runtime -> get_plugin_config.
        if getattr(_LOCAL, "config_depth", 0):
            return True
        from helpers import plugins
        _LOCAL.config_depth = 1
        try:
            if "rrsi" not in plugins.get_enabled_plugins(agent):
                return False
            config = plugins.get_plugin_config("rrsi", agent=agent) or {}
            return config.get("enabled", True) is not False
        finally:
            _LOCAL.config_depth = 0

    def resolve(self, agent: Any) -> Artifact | None:
        if agent is None or not self.enabled(agent):
            return None
        version = self.pin(agent)
        if version == "baseline":
            return None
        try:
            with self._lock:
                revision = self.revision()
                if not revision or revision == "unavailable":
                    raise ValueError("Current framework identity is unavailable")
                if version not in self._artifacts:
                    root = self.store.artifact_path(version)
                    manifest = json.loads(artifact_file(root, "manifest.json").read_text(encoding="utf-8"))
                    manifest = validate_manifest(root, manifest, revision)
                    if manifest["version"] != version:
                        raise ValueError("Pinned artifact identity mismatch")
                    self._artifacts[version] = Artifact(version, root, manifest)
                if self._artifacts[version].manifest["compatibility"]["framework_revision"] != revision:
                    raise ValueError("Framework changed after this artifact was loaded")
                return self._artifacts[version]
        except Exception as exc:
            self.failure(version, "resolve", exc)
            raise RuntimeFailure("Pinned RRSI artifact is missing, invalid or incompatible") from None

    def failure(self, version: str, hook: str, error: BaseException) -> None:
        payload = {"request_id": uuid.uuid4().hex, "version": version, "hook": hook, "error_type": type(error).__name__}
        with self.store.lock("runtime-rollback", blocking=True):
            self.store.write_json("rollback_requested.json", payload)
        self.store.event("runtime_failure", **payload)

    def symbol(self, artifact: Artifact, reference: str) -> Any:
        path, symbol = _symbol_file(artifact.root, reference)
        relative = path.relative_to(artifact.root)
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = artifact.manifest.get("files", {}).get(str(relative))
        if expected is not None and source_hash != expected:
            raise RuntimeFailure("An immutable runtime module changed on disk")
        key = (artifact.version, str(relative))
        with self._lock:
            cached = self._modules.get(key)
            if cached:
                if cached[0] != source_hash:
                    raise RuntimeFailure("An immutable runtime module changed on disk")
                return getattr(cached[1], symbol)
            prefix = f"usr.plugins.rrsi._versions.h_{artifact.version}"
            segments = relative.with_suffix("").parts
            # Explicit package namespaces allow artifact-relative imports without
            # modifying sys.path or reusing another version's module identities.
            package_paths = [("usr.plugins.rrsi._versions", artifact.root.parent), (prefix, artifact.root)]
            for count in range(1, len(segments)):
                package_paths.append((prefix + "." + ".".join(segments[:count]), artifact.root.joinpath(*segments[:count])))
            for package_name, package_path in package_paths:
                if package_name not in sys.modules:
                    package = types.ModuleType(package_name)
                    package.__path__ = [str(package_path)]
                    package.__package__ = package_name
                    sys.modules[package_name] = package
            module_name = prefix + "." + ".".join(segments)
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise RuntimeFailure("Runtime module could not be loaded")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(module_name, None)
                raise
            self._modules[key] = (source_hash, module)
            return getattr(module, symbol)

    def _invocation(self, hook: str, agent: Any, kwargs: dict) -> tuple[Artifact | None, Any, dict]:
        artifact = self.resolve(agent)
        reference = artifact.manifest.get("callbacks", {}).get(hook) if artifact else None
        if not reference:
            return artifact, None, {}
        callback = self.symbol(artifact, reference)
        # A callback can inspect metadata without accidentally mutating the
        # registry used by other conversations pinned to this version.
        callback_artifact = Artifact(artifact.version, artifact.root, copy.deepcopy(artifact.manifest))
        values = {"agent": agent, "runtime": self, "artifact": callback_artifact, **kwargs}
        signature = inspect.signature(callback)
        if not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            values = {key: value for key, value in values.items() if key in signature.parameters}
        return artifact, callback, values

    def dispatch_sync(self, hook: str, agent: Any, **kwargs: Any) -> Any:
        if hook not in SYNC_HOOKS:
            raise ValueError("Not a synchronous runtime hook")
        version = self.pin(agent) if agent is not None else "baseline"
        try:
            _, callback, values = self._invocation(hook, agent, kwargs)
            if callback is None:
                return None
            result = callback(**values)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("Synchronous runtime hook returned an awaitable")
            return result
        except Exception as exc:
            self.failure(version, hook, exc)
            raise RuntimeFailure(f"RRSI {hook} callback failed") from None

    async def dispatch_async(self, hook: str, agent: Any, **kwargs: Any) -> Any:
        if hook not in ASYNC_HOOKS:
            raise ValueError("Not an asynchronous runtime hook")
        version = self.pin(agent) if agent is not None else "baseline"
        try:
            _, callback, values = self._invocation(hook, agent, kwargs)
            if callback is None:
                return None
            result = callback(**values)
            return await result if inspect.isawaitable(result) else result
        except Exception as exc:
            self.failure(version, hook, exc)
            raise RuntimeFailure(f"RRSI {hook} callback failed") from None


_RUNTIME: Runtime | None = None
_RUNTIME_LOCK = threading.RLock()


def get_runtime() -> Runtime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = Runtime()
        return _RUNTIME
