"""Fixed Agent Zero seams for the versioned RRSI runtime registry.

Framework imports deliberately occur at call time so registry validation and
standalone tests do not initialize Agent Zero or read an owner's runtime state.
"""
from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from typing import Any

from usr.plugins.rrsi.helpers.runtime import (
    Artifact, CONFIG_PLUGINS, PIN_KEY, ROLE_KEY, SKILLS_KEY, Runtime,
    RuntimeFailure, _context_value, _set_context_value, artifact_file, get_runtime,
)


def _args(data: dict, names: tuple[str, ...]) -> dict:
    positional = data.get("args", ())
    result = dict(zip(names, positional[1:]))
    result.update(data.get("kwargs", {}))
    return result


def higher_priority_asset(agent: Any, kind: str, filename: str) -> bool:
    """Honor project/profile/user files above an ordinary plugin contribution."""
    from helpers import files, plugins, subagents
    rrsi_root = plugins.find_plugin_dir("rrsi")
    roots = subagents.get_paths(agent, kind, must_exist_completely=False)
    for root in roots:
        if rrsi_root and Path(root).resolve() == (Path(rrsi_root) / kind).resolve():
            break
        if Path(root).resolve() == Path(files.get_abs_path(kind)).resolve():
            break
        if (Path(root) / filename).is_file():
            return True
    return False


def _tool_allowed(agent: Any, name: str) -> bool:
    from helpers.tool_policy import resolve_tool
    return bool(resolve_tool(agent, name).allowed)


def visible_tools(agent: Any, artifact: Artifact) -> dict:
    return {name: item for name, item in artifact.manifest.get("tools", {}).items()
            if not higher_priority_asset(agent, "tools", name + ".py") and _tool_allowed(agent, name)}


def visible_skills(agent: Any, artifact: Artifact) -> dict:
    from helpers import skills
    result = {}
    for name, item in artifact.manifest.get("skills", {}).items():
        # An explicit ordinary skill with this name retains ownership, including
        # hidden native skills; do not re-expose one through the virtual catalog.
        if skills.find_skill(name, agent=agent, include_hidden=True):
            continue
        skill = skills.skill_from_markdown(artifact.file(item["path"]), include_content=False)
        if skill is None or skill.name != name or skills._skill_is_hidden_for_agent(agent, skill):
            continue
        result[name] = item
    return result


def visible_roles(agent: Any, artifact: Artifact) -> dict:
    from helpers import projects, subagents
    project = projects.get_context_project_name(agent.context)
    # Include disabled native names so an evolved role cannot bypass that policy.
    native = subagents.get_agents_dict(project)
    available = subagents.get_available_agents_dict(project)
    return {name: item for name, item in artifact.manifest.get("roles", {}).items()
            if name not in native and item.get("base_profile", "default") in available}


def _role(agent: Any, artifact: Artifact) -> dict:
    return artifact.manifest.get("roles", {}).get(_context_value(agent.context, ROLE_KEY, ""), {})


def _merge(base: dict, overlay: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def apply_plugin_config(agent: Any, data: dict, runtime: Runtime) -> None:
    """Sparse tuning only; settings authority and model identity stay external."""
    values = dict(zip(("plugin_name", "agent", "project_name", "agent_profile", "caller"), data.get("args", ())))
    values.update(data.get("kwargs", {}))
    name = values.get("plugin_name")
    agent = agent or values.get("agent")
    if agent is None or name not in CONFIG_PLUGINS:
        return
    # Evaluation runner supplies this out-of-band. Candidates never own it.
    frozen = _context_value(agent.context, "rrsi_frozen_config", {})
    if name in frozen:
        data["result"] = copy.deepcopy(frozen[name])
        if name != "_model_config":
            return
    baseline = _context_value(agent.context, "rrsi_runtime_config", {})
    if name in baseline and name not in frozen:
        data["result"] = copy.deepcopy(baseline[name])
    artifact = runtime.resolve(agent)
    if artifact is None:
        return
    from helpers import plugins, projects
    scoped = plugins.find_plugin_asset(name, "config.json",
        project_name=projects.get_context_project_name(agent.context) or "",
        agent_profile=getattr(agent.config, "profile", ""))
    # Preserve project/profile overrides, including native profile assets.
    if scoped and (scoped.get("project_name") or scoped.get("agent_profile") or
                   "/.a0proj/" in scoped.get("path", "") or "/agents/" in scoped.get("path", "")):
        return
    tuning = _merge(artifact.manifest.get("config", {}).get(name, {}), _role(agent, artifact).get("config", {}).get(name, {}))
    if tuning and isinstance(data.get("result"), dict):
        data["result"] = _merge(data["result"], tuning)


def apply_prompt(agent: Any, data: dict, runtime: Runtime, *, parse: bool = False) -> None:
    values = _args(data, ("_prompt_file" if parse else "file",))
    name = values.pop("_prompt_file" if parse else "file", "")
    artifact = runtime.resolve(agent)
    if artifact is None or name not in artifact.manifest.get("prompts", {}) or higher_priority_asset(agent, "prompts", name):
        return
    from helpers import files, subagents
    path = artifact.file(artifact.manifest["prompts"][name])
    directories = [str(path.parent), *subagents.get_paths(agent, "prompts")]
    # Use the native renderer (conditions, includes and template variables).
    function = files.parse_file if parse else files.read_prompt_file
    result = function(path.name, _directories=directories, _agent=agent, **values)
    if not parse and files.is_full_json_template(result):
        result = files.remove_code_fences(result)
    data["result"] = result


def _generated_tool(agent: Any, values: dict, runtime: Runtime, artifact: Artifact, item: dict) -> Any:
    from helpers.tool import Tool, Response
    implementation = runtime.symbol(artifact, item["implementation"])
    if not inspect.isclass(implementation) or not issubclass(implementation, Tool):
        raise TypeError("Generated tool implementation must subclass helpers.tool.Tool")

    class VersionedTool(implementation):
        async def execute(self, **kwargs):
            try:
                result = super().execute(**kwargs)
                response = await result if inspect.isawaitable(result) else result
                if not isinstance(response, Response) or response.break_loop:
                    raise TypeError("Generated tools must return a nonterminal Tool Response")
                return response
            except Exception as exc:
                runtime.failure(artifact.version, "generated_tool", exc)
                raise RuntimeFailure("RRSI generated tool failed") from None

    return VersionedTool(agent=agent, **values)


def _skills_tool(agent: Any, values: dict, runtime: Runtime, artifact: Artifact) -> Any:
    from helpers import skills
    from helpers.tool import Response
    from tools.skills_tool import SkillsTool

    class VersionedSkills(SkillsTool):
        async def execute(self, **kwargs):
            action = self._current_action(**kwargs)
            name = self._normalize_skill_name(str(kwargs.get("skill_name") or self.args.get("skill_name") or ""))
            catalog = visible_skills(agent, artifact)
            if action in {"list", "search"}:
                response = await super().execute(**kwargs)
                query = str(kwargs.get("query") or self.args.get("query") or "").strip().lower()
                entries = [f"- {key}: {item.get('description', '')}" for key, item in catalog.items()
                           if action == "list" or query and query in (key + " " + str(item.get("description", ""))).lower()]
                if entries:
                    response.message += "\n\nRRSI skills for this conversation:\n" + "\n".join(entries)
                return response
            if name not in catalog:
                return await super().execute(**kwargs)
            item = catalog[name]
            if action == "load":
                content = skill_content(artifact, name, item)
                loaded = list(agent.get_data(SKILLS_KEY) or [])
                limit = skills.get_max_active_skills(agent=agent)
                agent.set_data(SKILLS_KEY, ([key for key in loaded if key != name] + [name])[-limit:])
                return Response(message=content, break_loop=False, additional={"skill_instructions": {
                    "name": name, "path": item["path"], "source": "rrsi:load",
                    "content_included": True, "rrsi_version": artifact.version,
                }})
            if action == "read_file":
                path = str(kwargs.get("file_path") or self.args.get("file_path") or "")
                if not path:
                    return Response(message="Error: file_path is required", break_loop=False)
                try:
                    root = artifact.file(item["path"]).parent
                    target = artifact_file(root, path)
                    artifact.file(str(target.relative_to(artifact.root)))
                    return Response(message=target.read_text(encoding="utf-8"), break_loop=False)
                except (OSError, ValueError):
                    return Response(message="Error: skill file is missing or outside this skill", break_loop=False)
            return await super().execute(**kwargs)

    return VersionedSkills(agent=agent, **values)


def skill_content(artifact: Artifact, name: str, item: dict) -> str:
    path = artifact.file(item["path"])
    return f"Skill: {name}\nPath: {path.parent}\nRRSI version: {artifact.version}\n\n" + path.read_text(encoding="utf-8")


def _subordinate_tool(agent: Any, values: dict, runtime: Runtime, artifact: Artifact) -> Any:
    from tools.call_subordinate import Delegation, get_or_create_subordinate, run_subordinate
    from helpers.tool import Response

    class VersionedDelegation(Delegation):
        async def execute(self, message="", reset="", context_id="", **kwargs):
            name = str(kwargs.get("profile", kwargs.get("agent_profile", "")) or "")
            role = visible_roles(agent, artifact).get(name)
            child = get_or_create_subordinate(
                agent, profile=role.get("base_profile", "default") if role else name,
                reset=reset, context_id=context_id or kwargs.get("agent_id", ""),
                name=kwargs.get("name", ""), message=message,
                slot="rrsi:" + name if role else "default",
            )
            current_role = _context_value(child.context, ROLE_KEY)
            if current_role and current_role != name:
                raise RuntimeFailure("Continuing a subordinate requires the same RRSI role")
            runtime.pin(child, inherited=artifact.version)
            if role:
                _set_context_value(child.context, ROLE_KEY, name)
            frozen = _context_value(agent.context, "rrsi_frozen_config")
            if frozen is not None:
                _set_context_value(child.context, "rrsi_frozen_config", copy.deepcopy(frozen))
            baseline = _context_value(agent.context, "rrsi_runtime_config")
            if baseline is not None:
                _set_context_value(child.context, "rrsi_runtime_config", copy.deepcopy(baseline))
            attachments = kwargs.get("attachments")
            result = await run_subordinate(agent, child, message, attachments if isinstance(attachments, list) else [])
            additional = {"context_id": child.context.id}
            from extensions.python.hist_add_tool_result import _90_save_tool_call_file
            if len(result) >= _90_save_tool_call_file.LEN_MIN:
                hint = agent.read_prompt("fw.hint.call_sub.md")
                if hint:
                    additional["hint"] = hint
            return Response(message=result, break_loop=False, additional=additional)

    return VersionedDelegation(agent=agent, **values)


def apply_tool(agent: Any, data: dict, runtime: Runtime) -> None:
    values = _args(data, ("name", "method", "args", "message", "loop_data"))
    name = values.get("name", "")
    artifact = runtime.resolve(agent)
    if artifact is None or higher_priority_asset(agent, "tools", name + ".py"):
        return
    from helpers.tool_policy import ensure_tool_allowed
    if name in artifact.manifest.get("tools", {}) or name in {"skills_tool", "call_subordinate"}:
        ensure_tool_allowed(agent, name)
        try:
            if name in artifact.manifest.get("tools", {}):
                data["result"] = _generated_tool(agent, values, runtime, artifact, artifact.manifest["tools"][name])
            elif name == "skills_tool" and artifact.manifest.get("skills"):
                data["result"] = _skills_tool(agent, values, runtime, artifact)
            elif name == "call_subordinate":
                data["result"] = _subordinate_tool(agent, values, runtime, artifact)
        except Exception as exc:
            runtime.failure(artifact.version, "get_tool", exc)
            raise RuntimeFailure("RRSI tool construction failed") from None


def merge_responses_tools(agent: Any, artifact: Artifact, call_data: dict) -> None:
    from helpers.responses_tools import _native_tool_name
    tools = visible_tools(agent, artifact)
    existing = list(call_data.get("a0_responses_function_tools") or [])
    mapping = dict(agent.get_data("responses_tool_name_map") or {})
    names = {_native_tool_name(name) for name in tools}
    existing = [item for item in existing if item.get("name") not in names]
    for name, item in tools.items():
        native = _native_tool_name(name)
        existing.append({"type": "function", "name": native, "description": item["prompt"][:1024], "parameters": copy.deepcopy(item["schema"])})
        mapping[native] = name
    call_data["a0_responses_function_tools"] = existing
    agent.set_data("responses_tool_name_map", mapping)


def add_system_prompt(agent: Any, artifact: Artifact, system_prompt: list) -> None:
    tools = visible_tools(agent, artifact)
    if tools:
        system_prompt.append("RRSI tools for this conversation:\n" + "\n\n".join(
            f"### {name}\n{item['prompt']}\nArguments: {json.dumps(item['schema'], sort_keys=True)}"
            for name, item in tools.items()))
    if _tool_allowed(agent, "skills_tool"):
        catalog = visible_skills(agent, artifact)
        if catalog:
            system_prompt.append("RRSI skills available through skills_tool:\n" + "\n".join(f"- {name}: {item.get('description', '')}" for name, item in catalog.items()))
    if _tool_allowed(agent, "call_subordinate"):
        roles = visible_roles(agent, artifact)
        if roles:
            system_prompt.append("RRSI roles available through call_subordinate(profile=...):\n" + "\n".join(f"- {name}" for name in roles))
    role_system = _role(agent, artifact).get("system")
    if role_system:
        system_prompt.append(role_system)


def reattach_skills(agent: Any, artifact: Artifact, loop_data: Any) -> None:
    from helpers import skills, tokens
    if loop_data is None:
        return
    visible = {skills.skill_instruction_name(message) for message in loop_data.history_output}
    catalog = visible_skills(agent, artifact)
    budget = 12000
    for name in agent.get_data(SKILLS_KEY) or []:
        if name in visible or name not in catalog:
            continue
        content = skill_content(artifact, name, catalog[name])
        count = tokens.approximate_tokens(content)
        if count > budget:
            continue
        budget -= count
        message = agent.hist_add_tool_result("skills_tool", content, skill_instructions={
            "name": name, "path": catalog[name]["path"], "source": "rrsi:reattach",
            "content_included": True, "rrsi_version": artifact.version,
        })
        loop_data.history_output.extend(message.output())


def _model_identity(call_data: dict) -> tuple:
    model = call_data.get("model")
    return (id(model), getattr(model, "model_name", None), copy.deepcopy(getattr(model, "kwargs", None)), copy.deepcopy(getattr(model, "a0_model_conf", None)))


def handle_sync(hook: str, agent: Any, runtime: Runtime | None = None, **kwargs) -> None:
    runtime = runtime or get_runtime()
    if hook == "plugin_config":
        apply_plugin_config(agent, kwargs.get("data", {}), runtime)
        return
    if agent is None:
        return
    if hook == "agent_init":
        # Restoring chats creates an Agent after its persisted log was attached.
        # The early fixed hook runs before A0 appends its initial greeting.
        restoring = bool(getattr(agent.context.log, "logs", []))
        runtime.pin(agent, new=not restoring)
    if hook == "read_prompt":
        name = _args(kwargs["data"], ("file",)).get("file", "")
        if higher_priority_asset(agent, "prompts", name):
            return
        apply_prompt(agent, kwargs["data"], runtime)
    elif hook == "parse_prompt":
        name = _args(kwargs["data"], ("_prompt_file",)).get("_prompt_file", "")
        if higher_priority_asset(agent, "prompts", name):
            return
        apply_prompt(agent, kwargs["data"], runtime, parse=True)
    elif hook == "get_tool":
        apply_tool(agent, kwargs["data"], runtime)
        return
    runtime.dispatch_sync(hook, agent, **kwargs)


async def handle_async(hook: str, agent: Any, runtime: Runtime | None = None, **kwargs) -> None:
    runtime = runtime or get_runtime()
    if agent is None:
        return
    artifact = runtime.resolve(agent)
    if artifact is None:
        return
    if hook == "handle_exception":
        values = _args(kwargs.get("data", {}), ("location", "exception"))
        error = values.get("exception")
        if isinstance(error, Exception):
            runtime.failure(artifact.version, str(values.get("location", hook)), error)
        # Reporting must not clear A0's exception handling or retry safeguards.
        return
    if hook == "system_prompt":
        add_system_prompt(agent, artifact, kwargs["system_prompt"])
    elif hook == "message_loop_prompts_after":
        reattach_skills(agent, artifact, kwargs.get("loop_data"))
    call_data = kwargs.get("call_data")
    identity = _model_identity(call_data) if isinstance(call_data, dict) else None
    original_model = call_data.get("model") if isinstance(call_data, dict) else None
    # Function start callbacks may alter arguments, but must still enter A0's
    # original loop/dispatch so its security and completion contracts execute.
    function_data = kwargs.get("data")
    preserve_result = hook in {"monologue_before", "process_tools_before"} and isinstance(function_data, dict)
    original_result = function_data.get("result") if preserve_result else None
    try:
        await runtime.dispatch_async(hook, agent, **kwargs)
    except BaseException:
        # A failing callback must not leave a cached provider mutated for a retry.
        if preserve_result:
            function_data["result"] = original_result
        if identity is not None:
            call_data["model"] = original_model
            if original_model is not None:
                for key, value in zip(("model_name", "kwargs", "a0_model_conf"), identity[1:]):
                    if hasattr(original_model, key):
                        setattr(original_model, key, value)
        raise
    if preserve_result and function_data.get("result") is not original_result:
        function_data["result"] = original_result
        error = RuntimeFailure("Evolved callbacks must preserve native loop and tool dispatch")
        runtime.failure(artifact.version, hook, error)
        raise error
    if identity is not None and _model_identity(call_data) != identity:
        call_data["model"] = original_model
        if original_model is not None:
            for key, value in zip(("model_name", "kwargs", "a0_model_conf"), identity[1:]):
                if hasattr(original_model, key):
                    setattr(original_model, key, value)
        error = RuntimeFailure("Evolved callbacks cannot change the configured model identity or provider parameters")
        runtime.failure(artifact.version, hook, error)
        raise error
    if hook == "chat_model_call_before":
        merge_responses_tools(agent, artifact, call_data)
