"""Local, conservative replay compilation; no research model sees raw chats."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from usr.plugins.rrsi.helpers.state import StateStore, canonical_hash
from usr.plugins.rrsi.helpers.tasks import TaskRepository, sanitize


def compile_replay(prompt: str) -> dict | None:
    """Recognize bounded deterministic requests with an independently computed oracle.

    Unsupported prose is never guessed into a benchmark. In particular, the
    assistant's answer is not an input to this compiler or its expected result.
    Numeric arrays contain no person names, free text, files or external effects.
    """
    clean = sanitize(prompt)
    if clean["redactions"] or clean["quarantined"] or len(prompt) > 16384:
        return None
    match = re.fullmatch(
        r"\s*(?:please\s+)?(sort|sum|deduplicate)\s+(?:this\s+)?(?:numeric\s+)?(?:list|array)?\s*"
        r"(\[[\d\s,.+eE-]*\])\s*(?:ascending)?\s*[.!]?\s*"
        r"(?:return\s+(?:only\s+)?JSON\s*[.!]?)?\s*", prompt, re.I)
    if not match:
        return None
    try:
        data = json.loads(match[2])
    except ValueError:
        return None
    if not isinstance(data, list) or not 1 <= len(data) <= 512 or any(type(x) is not int or abs(x) > 10**9 for x in data):
        return None
    recipe = match[1].lower()
    expected = {"sort": lambda: sorted(data), "sum": lambda: sum(data),
                "deduplicate": lambda: list(dict.fromkeys(data))}[recipe]()
    body = json.dumps(data)
    prompt_text = {"sort": "Sort the integers in ascending order", "sum": "Sum the integers",
                   "deduplicate": "Remove duplicate integers, retaining first occurrence order"}[recipe]
    # A family has one permanently assigned split, across every campaign.
    split = {"sort": "evolve", "sum": "heldout", "deduplicate": "transfer"}[recipe]
    fingerprint = canonical_hash({"recipe": recipe, "values": data})
    return {"id": "learned_" + fingerprint[:32], "family": "replay_numeric_" + recipe,
            "split": split, "prompt": f"{prompt_text} in input.json. Return only JSON.",
            "fixtures": {"input.json": body}, "evaluator": {"kind": "json", "expected": expected},
            "source": "replay_fixture", "sanitized": True}


def completed_interaction(agent: Any, *, response: str) -> dict | None:
    if getattr(agent, "number", -1) != 0 or os.environ.get("RRSI_TRIAL") == "1":
        return None
    from helpers import plugins
    config = plugins.get_plugin_config("rrsi", agent=agent)
    if not config.get("enabled") or not config.get("automatic_capture", True):
        return None
    content = getattr(getattr(agent, "last_user_message", None), "content", "")
    if isinstance(content, dict):
        # Attachments, user files and system additions are never captured here.
        if content.get("attachments"):
            return None
        prompt = content.get("user_message", "")
    else:
        prompt = content
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    known_secrets = [value for key, value in os.environ.items()
                     if re.search(r"(?:API_KEY|TOKEN|PASSWORD|SECRET)$", key) and len(value) >= 8]
    task = compile_replay(prompt)
    return TaskRepository(StateStore()).capture(prompt=prompt, response=response,
                context_id=str(agent.context.id), known_secrets=known_secrets, replay_task=task)
