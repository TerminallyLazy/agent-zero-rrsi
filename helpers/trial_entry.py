"""Entrypoint executed by the real framework interpreter inside a disposable trial."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import time
import traceback


def proxy_configuration(data: dict, embedding_url: str | None = None) -> dict:
    """Frozen provider identities expose only inert local endpoints in the sandbox."""
    model_cfg = {}
    for role, key in [("policy", "chat_model"), ("utility", "utility_model"), ("vision", "vision_model")]:
        identity = data["model_identity"][role]
        model_cfg[key] = {"provider": "openai", "name": "rrsi-" + role,
            "api_key": "not-a-provider-key", "api_base": "http://127.0.0.1:1",
            "ctx_length": identity.get("ctx_length") or 100000,
            "vision": identity.get("vision", False), "kwargs": identity.get("parameters") or {}}
    embedding = data["model_identity"].get("embedding", {})
    if not embedding.get("provider") or not embedding.get("name"):
        raise ValueError("Trials require a frozen configured embedding model")
    if embedding.get("provider") == "huggingface" and str(embedding["name"]).startswith("sentence-transformers/"):
        model_cfg["embedding_model"] = {"provider": "huggingface", "name": embedding["name"],
            "kwargs": {"device": "cpu", "cache_folder": "/rrsi-embeddings/hub", "trust_remote_code": False}}
    elif embedding_url:
        model_cfg["embedding_model"] = {"provider": "openai", "name": "rrsi-embedding",
            "api_key": "not-a-provider-key", "api_base": embedding_url,
            "kwargs": {"encoding_format": "float", "num_retries": 0, "max_retries": 0}}
    else:
        raise ValueError("Remote embedding model requires the scoped local broker bridge")
    return model_cfg


async def run():
    from usr.plugins.rrsi.helpers.state import StateStore, atomic_json
    data = json.loads(Path("/rrsi-input/input.json").read_text())
    output = Path("/rrsi-output")
    context = None
    embedding_bridge = None
    started = time.monotonic()
    version = ""
    try:
        store = StateStore()
        manifest = json.loads(Path("/rrsi-input/harness/manifest.json").read_text())
        version = manifest["version"]
        shutil.copytree("/rrsi-input/harness", store.artifact_path(version))
        store.set_active(version, "isolated evaluation")
        store.write_json("framework-identity.json", {"framework_revision": __import__('os').environ['RRSI_FRAMEWORK_REVISION']})
        from usr.plugins.rrsi.helpers.trial_proxy import EmbeddingBridge
        embedding = data["model_identity"].get("embedding", {})
        local_embedding = embedding.get("provider") == "huggingface" and str(embedding.get("name", "")).startswith("sentence-transformers/")
        if not local_embedding:
            embedding_bridge = EmbeddingBridge(data)
        model_cfg = proxy_configuration(data, embedding_bridge.start() if embedding_bridge else None)
        plugin_cfg = Path("/a0/usr/plugins/_model_config"); plugin_cfg.mkdir(parents=True, exist_ok=True)
        atomic_json(plugin_cfg / "config.json", model_cfg)
        for name in ("_telegram_integration", "_whatsapp_integration", "_email_integration", "_oauth"):
            folder = Path("/a0/usr/plugins") / name; folder.mkdir(parents=True, exist_ok=True)
            (folder / ".toggle-0").touch()
        from agent import AgentContext, UserMessage
        from initialize import initialize_agent
        from helpers import runtime as framework_runtime
        framework_runtime.initialize()
        framework_runtime.args["dockerized"] = True
        context = AgentContext(config=initialize_agent(override_settings={"mcp_servers": ""}))
        context.set_data("rrsi_harness_version", version)
        context.set_data("rrsi_evaluation", True)
        context.set_data("rrsi_trial_id", data["trial_id"])
        context.set_data("rrsi_frozen_config", {"_model_config": model_cfg})
        runtime_config = data.get("runtime_config", {})
        if not isinstance(runtime_config, dict) or set(runtime_config) - {"_memory", "_skills", "_chat_compaction"}:
            raise ValueError("Unsupported frozen runtime configuration")
        context.set_data("rrsi_runtime_config", runtime_config)
        task = context.communicate(UserMessage(message=data["prompt"] + "\n\nUse /work for task files. Complete with the response tool."))
        response = await task.result(timeout=data["timeout_seconds"])
        if not isinstance(response, str):
            raise ValueError("Agent did not complete with a response string")
        result = {"valid": True, "response": response}
        if data.get("canary"):
            from usr.plugins.rrsi.helpers.canary_probe import inspect_context
            result["canary"] = await inspect_context(context)
            required = ("compatibility", "runtime_canary", "tool_policy", "response_termination")
            if not isinstance(result["canary"], dict) or not all(result["canary"].get(key) is True for key in required):
                result.update(valid=False, error="functional_canary_failed")
    except BaseException as exc:
        result = {"valid": False, "response": "", "error": type(exc).__name__}
        (output / "error.txt").write_text(traceback.format_exc())
    finally:
        if embedding_bridge is not None:
            embedding_bridge.close()
    result.update(trial_id=data["trial_id"], version=version)
    if context is not None:
        try:
            atomic_json(output / "trace.json", {"messages": context.agent0.history.output(),
                "logs": asdict(context.log.output()), "context_data": {
                    k: context.get_data(k) for k in ("rrsi_harness_version", "rrsi_trial_id")}})
        except BaseException as exc:
            result.update(valid=False, error="trace_" + type(exc).__name__)
            (output / "error.txt").write_text(traceback.format_exc())
    atomic_json(output / "resources.json", {"elapsed_seconds": time.monotonic() - started,
        "embedding": data["model_identity"].get("embedding"),
        "embedding_execution": "scoped-broker" if embedding_bridge else "offline-local-cpu",
        "embedding_policy_llm_tokens": None if embedding_bridge else 0})
    atomic_json(output / "result.json", result)
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
