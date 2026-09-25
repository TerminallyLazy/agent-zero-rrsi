"""Independent graders; candidate Python executes only in a fresh Docker sandbox."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import time

from usr.plugins.rrsi.helpers.contracts import CampaignConfig, TaskSpec
from usr.plugins.rrsi.helpers.state import StateStore, confined
from usr.plugins.rrsi.helpers.tasks import grade

RUNNER = r'''
import importlib.util,json,pathlib
spec=json.loads(pathlib.Path('/grader-input/case.json').read_text())
path=(pathlib.Path('/grader-input/files')/spec['module']).resolve()
assert path.is_relative_to(pathlib.Path('/grader-input/files'))
module_spec=importlib.util.spec_from_file_location('candidate_solution',path)
module=importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(module)
result=getattr(module,spec['function'])(*spec['args'],**spec['kwargs'])
print(json.dumps({'result':result},allow_nan=False))
'''


def grade_task(task: TaskSpec, response: str, output_root: Path | None = None,
               image: str = "", *, store: StateStore | None = None,
               cancelled=None, **kwargs) -> dict:
    if task.evaluator["kind"] != "python_tests":
        return grade(task, response, output_root)
    if output_root is None or not image:
        raise ValueError("Python grading requires candidate outputs and an immutable Docker image")
    from usr.plugins.rrsi.helpers.sandbox import DockerSandbox
    from usr.plugins.rrsi.helpers.broker import Cancelled
    rule = task.evaluator
    module = str(rule["module"])
    confined(output_root, module, existing=True)
    if not module.endswith(".py") or not str(rule["function"]).isidentifier():
        raise ValueError("Invalid Python oracle entrypoint")
    cases = rule["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Python oracle requires 1..100 hidden cases")
    with tempfile.TemporaryDirectory(prefix="rrsi-grader-") as temp:
        root = Path(temp)
        # Separate journal namespace prevents collision with active trial resources.
        grader_store = StateStore(store.path("grader") if store else root / "state")
        sandbox = DockerSandbox(grader_store, CampaignConfig(framework_image=image),
                                Path(__file__).resolve().parents[4], cancelled=cancelled)
        try:
            passed = 0
            for case in cases:
                if sandbox.cancelled():
                    raise Cancelled("Grading cancelled")
                inp = root / "input"
                inp.mkdir()
                files = inp / "files"; files.mkdir()
                for source in output_root.rglob("*"):
                    if source.is_symlink():
                        raise ValueError("Candidate output contains a symlink")
                    if source.is_file():
                        target = confined(files, source.relative_to(output_root))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
                # Expected outputs remain in this controller. The untrusted program
                # receives only arguments, and cannot forge the parent comparison.
                (inp / "case.json").write_text(json.dumps({"module":module,"function":rule["function"],
                    "args":case.get("args",[]),"kwargs":case.get("kwargs",{})}))
                volume = sandbox.volume()
                sandbox.populate(volume, inp)
                container = sandbox._container("--read-only", "--user","65534:65534", "--network","none",
                    "--cap-drop","ALL","--security-opt","no-new-privileges","--pids-limit","32",
                    "--memory","256m","--cpus","1", "--log-opt","max-size=1m","--log-opt","max-file=1",
                    "--tmpfs","/tmp:rw,nosuid,nodev,size=16m,mode=1777",
                    "--mount",f"type=volume,source={volume},target=/grader-input,readonly",
                    "--entrypoint","/usr/bin/env", image, "-i", "PATH=/usr/bin:/bin", "HOME=/tmp",
                    "PYTHONDONTWRITEBYTECODE=1", "/opt/venv-a0/bin/python","-I","-c",RUNNER)
                sandbox.docker("start",container)
                deadline = time.monotonic()+10
                while True:
                    state = json.loads(sandbox.docker("inspect","--format","{{json .State}}",container).stdout)
                    if not state["Running"]:
                        break
                    if sandbox.cancelled():
                        raise Cancelled("Grading cancelled")
                    if time.monotonic()>deadline:
                        sandbox.docker("kill",container,check=False)
                        break
                    time.sleep(.1)
                if not state["Running"] and state["ExitCode"] == 0:
                    logs=sandbox.docker("logs","--tail","1",container).stdout
                    try:
                        result=json.loads(logs)
                        passed += len(logs)<1024*1024 and result == {"result":case["expected"]}
                    except (ValueError,TypeError):
                        pass
                sandbox.docker("rm","-f",container,check=False)
                sandbox.resources.remove(("container",container))
                sandbox.docker("volume","rm",volume,check=False)
                sandbox.resources.remove(("volume",volume))
                shutil.rmtree(inp)
            return {"reward":passed/len(cases), "valid":True,
                    "reason":"passed" if passed==len(cases) else "incorrect_output"}
        finally:
            sandbox.close()
