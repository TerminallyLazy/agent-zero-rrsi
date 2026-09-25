"""Disposable Docker trials. Named volumes work from Docker Desktop and containers."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from typing import Callable
import uuid

from usr.plugins.rrsi.helpers.contracts import CampaignConfig, TaskSpec
from usr.plugins.rrsi.helpers.state import StateStore, atomic_json, confined

TRIAL_VOLUME_BYTES = 64 * 1024 * 1024
DOCKER_LOG_OPTIONS = ("--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=2")

# This is a capability boundary, not a packaging ignore list. New plugin files
# are absent from candidates unless explicitly admitted here after review.
RUNTIME_PLUGIN_FILES = frozenset({
    "__init__.py", "plugin.yaml", "default_config.yaml", "helpers/__init__.py",
    "helpers/runtime.py", "helpers/runtime_adapters.py", "helpers/state.py",
    "helpers/trial_entry.py", "helpers/trial_proxy.py", "helpers/canary_probe.py",
    "extensions/python/agent_init/_01_rrsi_runtime.py",
    "extensions/python/tool_execute_after/_98_rrsi_canary.py",
    *{f"extensions/python/{point}/_90_rrsi_runtime.py" for point in (
        "system_prompt", "monologue_start", "message_loop_start",
        "message_loop_prompts_before", "message_loop_prompts_after",
        "before_main_llm_call", "message_loop_result", "message_loop_end",
        "chat_model_call_before", "chat_model_call_after", "util_model_call_before",
        "util_model_call_after", "tool_execute_before", "tool_execute_after",
        "_functions/agent/Agent/call_chat_model_turn/end",
        "_functions/agent/Agent/get_tool/start", "_functions/agent/Agent/handle_exception/start",
        "_functions/agent/Agent/monologue/start", "_functions/agent/Agent/monologue/end",
        "_functions/agent/Agent/parse_prompt/start", "_functions/agent/Agent/read_prompt/start",
        "_functions/agent/Agent/prepare_prompt/end", "_functions/agent/Agent/process_tools/start",
        "_functions/agent/Agent/process_tools/end",
        "_functions/helpers/plugins/get_plugin_config/end",
    )},
    *{f"extensions/python/{point}/_999_rrsi_trial_proxy.py" for point in (
        "_functions/agent/Agent/get_chat_model/end", "_functions/agent/Agent/get_utility_model/end",
        "_functions/models/LiteLLMChatWrapper/unified_call/start",
        "_functions/models/LiteLLMChatWrapper/unified_turn/start",
    )},
})


def runtime_plugin_files(root: Path) -> dict[str, str]:
    """Hash only the reviewed native dispatch dependency closure."""
    files = {}
    for relative in sorted(RUNTIME_PLUGIN_FILES):
        path = root / relative
        if not path.is_file() or any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
            raise ValueError("A required runtime-only worker file is missing or symlinked")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def copy_runtime_plugin(root: Path, destination: Path, expected: dict[str, str]) -> None:
    """Exclude tasks, oracles, controllers, APIs, UI, vendor and owner settings."""
    if runtime_plugin_files(root) != expected:
        raise ValueError("RRSI worker runtime changed during snapshot creation")
    for relative in sorted(RUNTIME_PLUGIN_FILES):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)

RELAY_SCRIPT = r'''
import http.client,json,os,socketserver,sys,urllib.parse
target=urllib.parse.urlsplit(sys.argv[1]); sock='/socket/broker.sock'
class Handler(socketserver.StreamRequestHandler):
 def handle(self):
  self.connection.settimeout(900)
  line=self.rfile.readline(16*1024*1024+1)
  if len(line)>16*1024*1024: return
  try:
   req=json.loads(line)
   conn=http.client.HTTPConnection(target.hostname,target.port,timeout=900)
   conn.request('POST','/turn',json.dumps(req['request']),{'Authorization':'Bearer '+req['token'],'Content-Type':'application/json'})
   response=conn.getresponse(); body=response.read(32*1024*1024)
   result={'status':response.status,'body':json.loads(body)}
   conn.close()
  except Exception as e: result={'status':502,'body':{'error':type(e).__name__}}
  self.wfile.write(json.dumps(result).encode()+b'\n')
class Server(socketserver.ThreadingUnixStreamServer): daemon_threads=True
os.makedirs('/socket',exist_ok=True)
if os.path.exists(sock): os.unlink(sock)
server=Server(sock,Handler); os.chmod(sock,0o666); server.serve_forever()
'''


class DockerError(RuntimeError):
    pass


class DockerSandbox:
    def __init__(self, store: StateStore, config: CampaignConfig,
                 framework_root: Path, *, cancelled: Callable[[], bool] | None = None,
                 idle: Callable[[], bool] | None = None, source_snapshot: dict | None = None):
        self.store, self.config = store, config
        self.framework_root = framework_root.resolve()
        self.cancelled = cancelled or (lambda: False)
        self.idle = idle or (lambda: True)
        self.source_snapshot = source_snapshot
        self.owner = hashlib.sha256(str(store.root).encode()).hexdigest()[:16]
        self.resources: list[tuple[str, str]] = []
        self._resources_lock = threading.RLock()
        self.source_volume = self.plugin_volume = self.socket_volume = None
        self.embedding_volume = None
        self.framework_revision = None

    def docker(self, *args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            # Avoid echoing command arguments or provider capabilities in exception messages.
            raise DockerError(f"Docker {args[0] if args else 'operation'} failed ({result.returncode})")
        return result

    def _owned(self, kind: str, value: str):
        with self._resources_lock:
            self.resources.append((kind, value))
            self.store.write_json("docker-resources.json", {"owner": self.owner, "resources": self.resources})
        return value

    def _forget(self, kind: str, value: str):
        with self._resources_lock:
            if (kind, value) in self.resources:
                self.resources.remove((kind, value))
            self.store.write_json("docker-resources.json", {"owner": self.owner, "resources": self.resources})

    def volume(self, *, limit_bytes: int | None = None) -> str:
        name = f"rrsi-{self.owner}-{uuid.uuid4().hex[:12]}"
        options = []
        if limit_bytes is not None:
            if type(limit_bytes) is not int or limit_bytes <= 0:
                raise ValueError("A tmpfs volume limit must be a positive integer")
            options = ["--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs",
                       "--opt", f"o=size={limit_bytes},mode=0777,uid=65534,gid=65534"]
        self.docker("volume", "create", "--label", f"a0.rrsi.owner={self.owner}", *options, name)
        return self._owned("volume", name)

    def _container(self, *args: str) -> str:
        ident = self.docker("create", "--label", f"a0.rrsi.owner={self.owner}", *DOCKER_LOG_OPTIONS, *args).stdout.strip()
        return self._owned("container", ident)

    def _hold_trial_storage(self, work_volume: str, output_volume: str) -> str:
        # A driver tmpfs is unmounted when its last consumer stops. Keep one
        # inert read-only consumer alive until post-exit export has completed.
        holder = self._container("--read-only", "--user", "65534:65534", "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "32m",
            "--pids-limit", "16", "--mount", f"type=volume,source={work_volume},target=/work,readonly",
            "--mount", f"type=volume,source={output_volume},target=/rrsi-output,readonly",
            "--entrypoint", "/bin/sleep", self.config.framework_image, "infinity")
        self.docker("start", holder)
        return holder

    def populate(self, volume: str, source: Path, *, writable: bool = False):
        helper = self._container("--network", "none", "--entrypoint", "/bin/sh",
                                 "--mount", f"type=volume,source={volume},target=/stage",
                                 self.config.framework_image, "-c",
                                 "chown -R 65534:65534 /stage && chmod -R u+rwX,go+rX /stage")
        self.docker("cp", str(source) + "/.", f"{helper}:/stage", timeout=120)
        self.docker("start", "-a", helper, timeout=120)
        self.docker("rm", helper)
        self._forget("container", helper)

    def preflight(self) -> dict:
        if not self.config.framework_image:
            raise ValueError("Select a pinned Agent Zero Docker image in RRSI setup")
        image = json.loads(self.docker("image", "inspect", self.config.framework_image).stdout)[0]
        if not image.get("Id", "").startswith("sha256:"):
            raise ValueError("Framework image cannot be resolved to an immutable digest")
        if self.source_snapshot is not None:
            revision = self.source_snapshot["framework_revision"]
            hashes = self.source_snapshot["files"]
            for relative, expected in hashes.items():
                source = confined(self.framework_root, relative, existing=True)
                if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                    raise ValueError("Trusted framework snapshot changed after capture")
        else:
            revision = subprocess.run(["git", "-C", str(self.framework_root), "rev-parse", "HEAD"],
                                      capture_output=True, text=True, check=True).stdout.strip()
            tracked = subprocess.run(["git", "-C", str(self.framework_root), "ls-files", "-z"],
                                     capture_output=True, check=True).stdout.decode().split("\0")
            hashes = {}
            for relative in tracked:
                if not relative or relative.startswith(("usr/", "tmp/", ".git/")) or Path(relative).name == ".env":
                    continue
                source = confined(self.framework_root, relative)
                if source.is_file() and not (self.framework_root / relative).is_symlink():
                    hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
        self.snapshot_files = dict(hashes)
        self.worker_files = runtime_plugin_files(Path(__file__).resolve().parents[1])
        from usr.plugins.rrsi.helpers.state import canonical_hash
        return {"image": image["Id"], "framework_revision": revision,
                "framework_source_sha256": canonical_hash(hashes),
                "worker_plugin_sha256": canonical_hash(self.worker_files),
                "trial_storage_bytes": {"work": TRIAL_VOLUME_BYTES, "output": TRIAL_VOLUME_BYTES},
                "trial_storage_backend": "kernel-bounded-tmpfs",
                "container_log_limit": {"driver": "local", "max-size": "1m", "max-file": "2"},
                "isolation": "docker-network-none-with-scoped-unix-model-relay"}

    def prepare(self, broker_url: str) -> dict:
        receipt = self.preflight()
        self.framework_revision = receipt["framework_revision"]
        image = receipt["image"]
        # Replace mutable tags with the immutable image selected by preflight.
        object.__setattr__(self.config, "framework_image", image)
        self.source_volume, self.plugin_volume, self.socket_volume = self.volume(), self.volume(), self.volume()
        # The immutable image's public model cache is copied, never a host/user cache.
        # Offline local inference can therefore initialize native memory without secrets.
        self.embedding_volume = self.volume()
        helper = self._container("--network", "none", "--entrypoint", "/bin/sh",
            "--mount", f"type=volume,source={self.embedding_volume},target=/cache",
            image, "-c", "if [ -d /root/.cache/huggingface ]; then cp -a /root/.cache/huggingface/. /cache/; fi; chmod -R a+rX /cache")
        self.docker("start", "-a", helper, timeout=120)
        self.docker("rm", helper)
        self._forget("container", helper)
        with tempfile.TemporaryDirectory(prefix="rrsi-source-") as temp:
            source = Path(temp) / "source"; source.mkdir()
            (source / "usr/plugins/rrsi").mkdir(parents=True)
            (source / "tmp").mkdir()
            tracked = self.snapshot_files.keys()
            for relative in tracked:
                if not relative or relative.startswith(("usr/", "tmp/", ".git/")) or Path(relative).name == ".env":
                    continue
                src = confined(self.framework_root, relative)
                if not src.is_file() or (self.framework_root / relative).is_symlink():
                    continue
                if hashlib.sha256(src.read_bytes()).hexdigest() != self.snapshot_files[relative]:
                    raise ValueError("Framework source changed during snapshot creation")
                dest = confined(source, relative); dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            self.populate(self.source_volume, source)
            plugin = Path(temp) / "plugin"; plugin.mkdir()
            root = Path(__file__).resolve().parents[1]
            copy_runtime_plugin(root, plugin, self.worker_files)
            self.populate(self.plugin_volume, plugin)
        parsed = __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(broker_url)
        network = "bridge"
        target_host = "host.docker.internal"
        controller = self.docker("inspect", socket.gethostname(), check=False)
        if controller.returncode == 0:
            networks = json.loads(controller.stdout)[0].get("NetworkSettings", {}).get("Networks", {})
            for name, cfg in networks.items():
                if cfg.get("IPAddress"):
                    network, target_host = name, cfg["IPAddress"]
                    break
        target = f"http://{target_host}:{parsed.port}/turn"
        relay = self._container("--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                                "--memory", "256m", "--pids-limit", "64", "--network", network,
                                "--add-host", "host.docker.internal:host-gateway",
                                "--mount", f"type=volume,source={self.socket_volume},target=/socket",
                                "--entrypoint", "/opt/venv-a0/bin/python", image,
                                "-c", RELAY_SCRIPT, target)
        self.docker("start", relay)
        for _ in range(30):
            ready = self.docker("exec", relay, "/bin/test", "-S", "/socket/broker.sock", check=False)
            if ready.returncode == 0:
                return receipt
            time.sleep(.1)
        raise DockerError("Model relay did not become ready")

    def trial(self, task: TaskSpec, artifact: Path, output_dir: Path,
              *, broker_token: str, trial_id: str, model_identity: dict,
              runtime_config: dict | None = None, canary: bool = False) -> dict:
        from usr.plugins.rrsi.helpers.broker import Cancelled
        if output_dir.exists() and any(output_dir.iterdir()):
            raise ValueError("Trial output directory must be new or empty")
        while not self.idle():
            if self.cancelled():
                raise Cancelled("Campaign stopped while waiting for idle")
            time.sleep(.25)
        if self.cancelled():
            raise Cancelled("Campaign stopped")
        if not self.source_volume:
            raise RuntimeError("Sandbox is not prepared")
        input_volume = self.volume()
        work_volume, output_volume = self.volume(limit_bytes=TRIAL_VOLUME_BYTES), self.volume(limit_bytes=TRIAL_VOLUME_BYTES)
        container = None
        holder = None
        try:
            holder = self._hold_trial_storage(work_volume, output_volume)
            with tempfile.TemporaryDirectory(prefix="rrsi-trial-") as temp:
                root = Path(temp)
                inp = root / "input"; inp.mkdir()
                shutil.copytree(artifact, inp / "harness", symlinks=False)
                atomic_json(inp / "input.json", {"trial_id": trial_id, "task_id": task.id,
                            "prompt": task.prompt, "broker_token": broker_token,
                            "model_identity": model_identity,
                            "runtime_config": runtime_config or {},
                            "canary": bool(canary),
                            "timeout_seconds": self.config.trial_timeout_seconds})
                self.populate(input_volume, inp)
                work = root / "work"; work.mkdir()
                for name, content in task.fixtures.items():
                    dest = confined(work, name); dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content)
                self.populate(work_volume, work, writable=True)
                out = root / "out"; out.mkdir()
                self.populate(output_volume, out, writable=True)
            args = ["--read-only", "--user", "65534:65534", "--network", "none",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--pids-limit", "256", "--memory", f"{self.config.trial_memory_mb}m",
                    "--cpus", str(self.config.trial_cpus), "--workdir", "/a0",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
                    "--tmpfs", "/a0/tmp:rw,nosuid,nodev,size=256m,mode=1777",
                    "--tmpfs", "/a0/usr:rw,nosuid,nodev,size=512m,mode=1777",
                    "--tmpfs", "/a0/usr/plugins:rw,nosuid,nodev,size=32m,mode=1777"]
            for volume, target, ro in [(self.source_volume,"/a0",True), (self.plugin_volume,"/a0/usr/plugins/rrsi",True),
                                       (input_volume,"/rrsi-input",True),(self.socket_volume,"/rrsi-broker",True),
                                       (self.embedding_volume,"/rrsi-embeddings",True),
                                       (work_volume,"/work",False),(output_volume,"/rrsi-output",False)]:
                args += ["--mount", f"type=volume,source={volume},target={target}" + (",readonly" if ro else "")]
            args += ["--entrypoint", "/usr/bin/env", self.config.framework_image,
                     "-i", "PATH=/opt/venv-a0/bin:/usr/bin:/bin", "HOME=/tmp", "TMPDIR=/tmp",
                     "PYTHONDONTWRITEBYTECODE=1", "RRSI_TRIAL=1", "RRSI_STATE_DIR=/a0/usr/rrsi",
                     "HF_HOME=/rrsi-embeddings", "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1",
                     "LITELLM_LOCAL_MODEL_COST_MAP=True", "TOKENIZERS_PARALLELISM=false",
                     f"RRSI_FRAMEWORK_REVISION={self.framework_revision}",
                     "/opt/venv-a0/bin/python", "-m", "usr.plugins.rrsi.helpers.trial_entry"]
            container = self._container(*args)
            self.docker("start", container)
            deadline = time.monotonic() + self.config.trial_timeout_seconds
            timed_out = False
            paused_at = None
            while True:
                state = json.loads(self.docker("inspect", "--format", "{{json .State}}", container).stdout)
                if not state["Running"]:
                    break
                if self.cancelled() or (paused_at is None and time.monotonic() >= deadline):
                    timed_out = True
                    self.docker("kill", container, check=False)
                    self.docker("wait", container, timeout=30, check=False)
                    break
                if not self.idle() and paused_at is None:
                    self.docker("pause", container)
                    paused_at = time.monotonic()
                elif self.idle() and paused_at is not None:
                    self.docker("unpause", container)
                    deadline += time.monotonic() - paused_at
                    paused_at = None
                time.sleep(.25)
            output_dir.mkdir(parents=True, exist_ok=True)
            state = json.loads(self.docker("inspect", "--format", "{{json .State}}", container).stdout)
            self._export_directory(holder, "/rrsi-output", output_dir)
            files_dir = output_dir / "files"; files_dir.mkdir(exist_ok=True)
            self._export_directory(holder, "/work", files_dir)
            result_path = output_dir / "result.json"
            result = json.loads(result_path.read_text()) if result_path.exists() else {"response":"", "valid":False, "error":"missing_result"}
            if timed_out:
                result.update(valid=False, error="cancelled" if self.cancelled() else "timeout")
            result["container_exit_code"] = state.get("ExitCode")
            if not result.get("valid"):
                # Local diagnostic only. Never copy this into model/search feedback.
                logs = self.docker("logs", "--tail", "150", container, check=False)
                (output_dir / "runner.log").write_text((logs.stdout + logs.stderr)[-64000:])
            atomic_json(output_dir / "execution.json", {"trial_id":trial_id,"container":container,
                        "network":"none","image":self.config.framework_image,
                        "timed_out":timed_out,"valid":result.get("valid",False)})
            return result
        finally:
            if container:
                self.docker("rm", "-f", container, check=False)
                self._forget("container", container)
            if holder:
                self.docker("rm", "-f", holder, check=False)
                self._forget("container", holder)
            for volume in (input_volume,work_volume,output_volume):
                self.docker("volume", "rm", volume, check=False)
                self._forget("volume", volume)

    def _export_directory(self, container: str, source: str, output: Path):
        process = subprocess.Popen(["docker", "cp", f"{container}:{source}/.", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        total = count = 0
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
                for item in archive:
                    count += 1; total += item.size
                    if count > 10000 or total > 64*1024*1024:
                        raise ValueError("Trial output exceeds export limit")
                    if item.isdir(): continue
                    if not item.isfile(): raise ValueError("Trial output cannot contain links or special files")
                    dest = confined(output, item.name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(item) as src, dest.open("wb") as dst:
                        shutil.copyfileobj(src,dst)
            if process.wait(timeout=30): raise DockerError("Trial output export failed")
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            if process.stdout is not None:
                process.stdout.close()

    def close(self):
        for kind, value in reversed(self.resources):
            if kind == "container": self.docker("rm", "-f", value, check=False)
            elif kind == "volume": self.docker("volume", "rm", value, check=False)
        self.resources.clear()
        self.store.write_json("docker-resources.json", {"owner":self.owner,"resources":[]})
