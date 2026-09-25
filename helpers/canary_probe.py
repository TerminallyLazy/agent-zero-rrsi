"""Trusted functional checks executed only in disposable activation trials."""
from __future__ import annotations

import json
from pathlib import Path


async def inspect_context(context) -> dict:
    from helpers import plugins, tool_policy, extension
    from helpers.errors import RepairableException
    from usr.plugins.rrsi.helpers.runtime import get_runtime
    from usr.plugins.rrsi.helpers.state import StateStore, atomic_json
    runtime = get_runtime()
    artifact = runtime.resolve(context.agent0)
    if artifact is None or artifact.version != context.get_data("rrsi_harness_version"):
        raise RuntimeError("Activation canary did not resolve the requested version")
    if StateStore().read_json("rollback_requested.json"):
        raise RuntimeError("Harness failed during activation canary")
    terminal = context.get_data("rrsi_canary_terminal") or {}
    if terminal.get("tool_name") != "response" or terminal.get("break_loop") is not True:
        raise RuntimeError("Activation canary did not terminate through native response")
    # Exercise actual native tool policy with a temporary instance-level deny.
    # This is isolated trial state; no production configuration is accessible.
    policy_path = Path("/a0/usr/plugins/_tool_access/config.json")
    previous = policy_path.read_bytes() if policy_path.exists() else None
    blocked_names = ["code_execution_tool", *artifact.manifest.get("tools", {})]
    checked = []
    try:
        atomic_json(policy_path,{"mode":"custom","default":"block","mcp_default":"block","allowed":[],"blocked":[]})
        plugins.clear_plugin_cache(["_tool_access"])
        for name in blocked_names:
            if tool_policy.resolve_tool(context.agent0,name).allowed:
                raise RuntimeError("Native tool policy unexpectedly allowed a blocked tool")
            denied = False
            try:
                await extension.call_extensions_async("tool_execute_before",context.agent0,
                                                      tool_name=name,tool_args={})
            except RepairableException:
                denied = True
            if not denied:
                raise RuntimeError("Native validation failed to enforce tool policy")
            checked.append(name)
        if not tool_policy.resolve_tool(context.agent0,"response").allowed:
            raise RuntimeError("Native response tool was blocked")
    finally:
        if previous is None:
            policy_path.unlink(missing_ok=True)
        else:
            policy_path.write_bytes(previous)
        plugins.clear_plugin_cache(["_tool_access"])
    return {"version":artifact.version,"compatibility":True,"runtime_canary":True,
            "tool_policy":True,"response_termination":True,"blocked_tools_checked":checked}
