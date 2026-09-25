"""Opt-in grader regression using the controller's real Docker daemon.

Run in Agent Zero's framework Python with RRSI_TEST_DOCKER_IMAGE set to an
existing immutable Agent Zero image. No model calls or user data are required.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usr.plugins.rrsi.helpers.contracts import TaskSpec
from usr.plugins.rrsi.helpers.grader import grade_task
from usr.plugins.rrsi.helpers.sandbox import DockerSandbox
from usr.plugins.rrsi.helpers.state import StateStore


@unittest.skipUnless(os.environ.get("RRSI_TEST_DOCKER_IMAGE"), "opt-in Docker grader test")
class GraderDockerTests(unittest.TestCase):
    def test_grader_starts_with_compressed_logs_and_scores_independently(self):
        original = DockerSandbox.docker
        inspected = []

        def observe(sandbox, *args, **kwargs):
            result = original(sandbox, *args, **kwargs)
            if args[0] == "create" and "/usr/bin/env" in args:
                info = json.loads(original(sandbox, "inspect", result.stdout.strip()).stdout)[0]
                host = info["HostConfig"]
                self.assertEqual(host["LogConfig"]["Type"], "local")
                self.assertEqual(host["LogConfig"]["Config"]["max-size"], "1m")
                self.assertGreaterEqual(int(host["LogConfig"]["Config"]["max-file"]), 2)
                self.assertEqual(host["NetworkMode"], "none")
                self.assertTrue(host["ReadonlyRootfs"])
                self.assertEqual(info["Config"]["User"], "65534:65534")
                inspected.append(info["Id"])
            return result

        task = TaskSpec("grader_probe", "grader_probe", "evolve", "Synthetic grading check", {
            "kind": "python_tests", "module": "solution.py", "function": "add",
            "cases": [{"args": [2, 3], "expected": 5}, {"args": [-4, 4], "expected": 0}],
        })
        with tempfile.TemporaryDirectory(prefix="rrsi-grader-test-") as temp:
            root = Path(temp)
            files = root / "files"
            files.mkdir()
            store = StateStore(root / "state")
            with patch.object(DockerSandbox, "docker", observe):
                for code, reward in (
                    ("def add(a, b): return a + b\n", 1.0),
                    ("def add(a, b): return -99\n", 0.0),
                    ("def add(a, b): raise ValueError('fixture')\n", 0.0),
                ):
                    with self.subTest(code=code):
                        (files / "solution.py").write_text(code)
                        result = grade_task(task, "", files, os.environ["RRSI_TEST_DOCKER_IMAGE"], store=store)
                        self.assertTrue(result["valid"])
                        self.assertEqual(result["reward"], reward)
                        self.assertEqual(store.read_json("grader/docker-resources.json")["resources"], [])
            self.assertEqual(len(inspected), 6)


if __name__ == "__main__":
    unittest.main()
