"""Credential-owning model broker. Candidate workers receive scoped capabilities only."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, asdict, is_dataclass
from enum import Enum
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import secrets
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

from usr.plugins.rrsi.helpers.budget import BudgetLedger, reported_usage
from usr.plugins.rrsi.helpers.state import StateStore, canonical_hash

SEARCH_ROLES = {"proposer", "analyst", "critic", "digester"}
POLICY_ROLES = {"policy", "utility", "vision", "embedding"}
SAFE_TURN_KEYS = {"a0_responses_function_tools", "responses_local_input_items",
                  "responses_input_items", "previous_response_id"}


class Cancelled(RuntimeError):
    pass


class ContextCapacityError(RuntimeError):
    pass


def binding_material(value):
    """Canonicalize resolved settings without exposing secrets or unstable reprs."""
    if isinstance(value, Enum):
        return binding_material(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return binding_material(asdict(value))
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [binding_material(v) for v in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {k: binding_material(v) for k, v in value.items()}
    if isinstance(value, bytes):
        return {"_bytes": value.hex()}
    raise ValueError("Resolved provider setting cannot be frozen deterministically")


def public_model_config(config: dict) -> dict:
    # Credentials and arbitrary kwargs never enter the campaign manifest.
    safe = {k: config.get(k) for k in ("provider", "name", "ctx_length", "vision")}
    kwargs = config.get("kwargs") or {}
    safe["parameters"] = {k: kwargs[k] for k in ("temperature", "top_p", "seed", "max_tokens",
                              "max_output_tokens", "reasoning_effort", "a0_api_mode", "responses_state") if k in kwargs}
    safe["endpoint_identity"] = canonical_hash(str(config.get("api_base") or "default"))
    return safe


class FrozenProvider:
    """Build native model wrappers once; use their resolved transport and credentials."""
    def __init__(self, configs: dict[str, dict], embedding: dict | None = None, *, store: StateStore | None = None):
        self._binding_key = (store or StateStore()).model_binding_key()
        self.configs = deepcopy(configs)
        self.embedding = deepcopy(embedding or {})
        self.wrappers = {}
        self.embedding_wrapper = None
        import models
        from plugins._model_config.helpers.model_config import build_model_config
        for role, cfg in configs.items():
            if not cfg.get("provider") or not cfg.get("name"):
                raise ValueError(f"Model role {role} is not configured")
            mc = build_model_config(cfg, models.ModelType.CHAT)
            self.wrappers[role] = models.get_chat_model(mc.provider, mc.name,
                                                       model_config=mc, **mc.build_kwargs())
        if self.embedding and self.embedding.get("provider") != "huggingface":
            mc = build_model_config(self.embedding, models.ModelType.EMBEDDING)
            self.embedding_wrapper = models.get_embedding_model(mc.provider, mc.name,
                                                        model_config=mc, **mc.build_kwargs())

    @classmethod
    def from_agent_zero(cls, agent=None, overrides: dict | None = None, *, store: StateStore | None = None):
        from plugins._model_config.helpers.model_config import (
            get_chat_model_config, get_utility_model_config, get_vision_model_config,
            get_embedding_model_config)
        chat = get_chat_model_config(agent)
        utility = get_utility_model_config(agent) or chat
        configs = {role: deepcopy(chat) for role in SEARCH_ROLES | {"policy"}}
        configs["utility"] = deepcopy(utility)
        configs["vision"] = deepcopy(get_vision_model_config(agent) or chat)
        for role, override in (overrides or {}).items():
            if role not in configs or not isinstance(override, dict):
                raise ValueError("Unknown model role")
            configs[role].update(override)
        return cls(configs, embedding=get_embedding_model_config(agent), store=store)

    def _binding(self, role, config, wrapper=None):
        material = {"schema": 1, "role": role, "configuration": config,
                    "resolved_model": getattr(wrapper, "model_name", None),
                    "resolved_options": getattr(wrapper, "kwargs", None),
                    "native_model_config": getattr(wrapper, "a0_model_conf", None)}
        encoded = json.dumps(binding_material(material), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()
        return hmac.new(self._binding_key, encoded, hashlib.sha256).hexdigest()

    def identity(self) -> dict:
        values = {}
        for role,cfg in self.configs.items():
            # Provider defaults may select Responses even when the saved preset
            # has no kwargs. Freeze the effective native transport, not just UI input.
            effective = {**cfg, "kwargs":deepcopy(self.wrappers[role].kwargs)}
            effective["api_base"] = effective["kwargs"].get("api_base",cfg.get("api_base"))
            values[role] = public_model_config(effective)
            values[role]["effective_binding"] = self._binding(role, cfg, self.wrappers[role])
        if self.embedding:
            cfg = self.embedding
            if self.embedding_wrapper is not None:
                cfg = {**cfg,"kwargs":self.embedding_wrapper.kwargs,
                       "api_base":self.embedding_wrapper.kwargs.get("api_base",cfg.get("api_base"))}
            values["embedding"] = public_model_config(cfg)
            values["embedding"]["effective_binding"] = self._binding("embedding", self.embedding,
                                                                     self.embedding_wrapper)
        return values

    def turn(self, role: str, messages: list[dict], max_tokens: int, kwargs: dict) -> dict:
        import models
        from helpers.litellm_transport import LiteLLMTransport
        class SingleAttemptTransport(LiteLLMTransport):
            # Every attempt needs its own durable reservation and receipt. A native
            # invisible fallback/retry would conceal a potentially paid first call.
            def _recover(self, exc, *, got_any_chunk):
                return False
        wrapper = self.wrappers[role]
        options = deepcopy(wrapper.kwargs)
        options.update({k: v for k,v in kwargs.items() if k in SAFE_TURN_KEYS})
        for alias in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
            options.pop(alias,None)
        options.update(max_tokens=max_tokens, max_retries=0, num_retries=0,
                       a0_responses_fallback=False, responses_state="local")
        if str(options.get("a0_api_mode","")).lower() in {"responses","response"}:
            options["max_output_tokens"] = max_tokens

        async def invoke():
            await models.apply_rate_limiter(wrapper.a0_model_conf, json.dumps(messages))
            transport = SingleAttemptTransport(model=wrapper.model_name, messages=messages,
                                         kwargs=options)
            await transport.acomplete()
            if transport.last_result is None:
                raise RuntimeError("Provider returned no structured LLM result")
            result = transport.last_result.to_dict()
            result.pop("raw", None)
            # LiteLLM adds hidden response_cost estimates to usage. These are not
            # necessarily provider charges (notably for subscription-backed models).
            estimate = result.get("usage", {}).pop("cost", None)
            if estimate is not None:
                result["rrsi_native_cost_estimate_usd"] = estimate
            return result
        return asyncio.run(invoke())

    def embed(self, texts: list[str]) -> dict:
        if self.embedding_wrapper is None:
            raise ValueError("Local embeddings must execute offline in the pinned trial image")
        import models
        import litellm
        models.configure_litellm()
        wrapper = self.embedding_wrapper
        options = deepcopy(wrapper.kwargs)
        options.update(max_retries=0,num_retries=0)
        models.apply_rate_limiter_sync(wrapper.a0_model_conf," ".join(texts))
        result = litellm.embedding(model=wrapper.model_name,input=texts,**options)
        raw = result.model_dump() if hasattr(result,"model_dump") else dict(result)
        usage = raw.get("usage") or {}
        inp = usage.get("prompt_tokens",usage.get("input_tokens"))
        if type(inp) is not int or inp < 0:
            raise ValueError("Embedding provider did not report complete input token usage")
        values = sorted(raw.get("data") or [],key=lambda item:item.get("index",0))
        vectors = [value.get("embedding") for value in values]
        if len(vectors)!=len(texts) or any(not isinstance(v,list) or not v or len(v)>65536 or
            any(type(x) not in (int,float) or not math.isfinite(x) for x in v) for v in vectors):
            raise ValueError("Embedding provider returned invalid vectors")
        return {"vectors":vectors,"usage":{"input_tokens":inp,"output_tokens":0}}


@dataclass(frozen=True)
class Capability:
    roles: frozenset[str]
    trial_id: str | None
    expires: float


class ModelBroker:
    def __init__(self, store: StateStore, provider, *, daily_limit: float = 0,
                 pricing: dict | None = None, cancelled: Callable[[], bool] | None = None):
        self.store, self.provider = store, provider
        self.ledger = BudgetLedger(store, daily_limit)
        self.pricing = pricing or {}
        self.cancelled = cancelled or (lambda: False)
        self.capabilities: dict[str, Capability] = {}
        self._lock = threading.Lock()
        self.server = None
        self.thread = None
        self.identity = provider.identity()

    def grant(self, roles: set[str], *, trial_id: str | None = None, ttl: int = 3600) -> str:
        if not roles or not roles <= (SEARCH_ROLES | POLICY_ROLES):
            raise ValueError("Invalid broker capability roles")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.capabilities[token] = Capability(frozenset(roles), trial_id, time.monotonic() + ttl)
        return token

    def revoke(self, token: str) -> None:
        with self._lock:
            self.capabilities.pop(token, None)

    def request(self, token: str, request: dict) -> dict:
        with self._lock:
            capability = self.capabilities.get(token)
        if capability is None or capability.expires < time.monotonic():
            raise PermissionError("Expired or invalid model capability")
        if self.cancelled():
            raise Cancelled("Campaign stopped")
        role = request.get("role", "policy")
        if role not in capability.roles:
            raise PermissionError("Model role not available to this worker")
        if role == "embedding":
            texts = request.get("texts")
            if not isinstance(texts,list) or not 1 <= len(texts) <= 1024 or any(not isinstance(v,str) for v in texts):
                raise ValueError("Embedding requests require a bounded list of strings")
            upper_input=sum(len(v.encode()) for v in texts)+128*len(texts)
            if upper_input>8*1024*1024:
                raise ContextCapacityError("Embedding input exceeds the bounded batch size")
            return self._embedding(capability,texts,upper_input)
        messages = request.get("messages")
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) for m in messages):
            raise ValueError("Expected model messages")
        maximum = request.get("max_tokens", 8192)
        if type(maximum) is not int or maximum <= 0 or maximum > 32768:
            raise ValueError("max_tokens must be 1..32768")
        turn_kwargs = {k:v for k,v in (request.get("kwargs") or {}).items() if k in SAFE_TURN_KEYS}
        encoded = json.dumps({"messages":messages, "tools_and_input":turn_kwargs}, ensure_ascii=False).encode()
        config = self.identity[role]
        context = int(config.get("ctx_length") or 0)
        # Deliberately conservative: one UTF-8 byte per token plus message framing.
        upper_input = len(encoded) + 128 * len(messages)
        if context and upper_input + maximum > context:
            raise ContextCapacityError("Complete research context exceeds the frozen model capacity; choose a larger context or shorter campaign")
        price = self.pricing.get(role)
        estimate = None
        if price is not None:
            estimate = (upper_input * float(price["input_per_million"]) + maximum * float(price["output_per_million"])) / 1_000_000
        call_id = uuid.uuid4().hex
        self.ledger.reserve(call_id, role, estimate, trial_id=capability.trial_id)
        try:
            result = self.provider.turn(role, messages, maximum, turn_kwargs)
            receipt = reported_usage(result.get("usage") or {}, call_id=call_id, role=role,
                                     model=f"{config.get('provider')}/{config.get('name')}",
                                     trial_id=capability.trial_id, price=price)
            self.ledger.finish(receipt)
        except BaseException as exc:
            # An unknown provider outcome is not refunded or treated as free.
            self.store.event("model_call_unsettled", call_id=call_id, role=role,
                             trial_id=capability.trial_id, error_type=type(exc).__name__)
            raise
        result["rrsi_receipt"] = receipt.to_dict()
        return result

    def _embedding(self, capability: Capability, texts: list[str], upper_input: int) -> dict:
        role="embedding"
        config=self.identity.get(role)
        if not config:
            raise ValueError("No frozen embedding identity")
        price=self.pricing.get(role)
        estimate=None if price is None else upper_input*float(price["input_per_million"])/1_000_000
        call_id=uuid.uuid4().hex
        self.ledger.reserve(call_id,role,estimate,trial_id=capability.trial_id)
        try:
            result=self.provider.embed(texts)
            receipt=reported_usage(result.get("usage") or {},call_id=call_id,role=role,
                model=f"{config.get('provider')}/{config.get('name')}",trial_id=capability.trial_id,price=price)
            self.ledger.finish(receipt)
        except BaseException as exc:
            self.store.event("model_call_unsettled",call_id=call_id,role=role,
                trial_id=capability.trial_id,error_type=type(exc).__name__)
            raise
        result["rrsi_receipt"]=receipt.to_dict()
        return result

    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        broker = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                try:
                    if self.path != "/turn":
                        raise ValueError("Unknown broker endpoint")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 16 * 1024 * 1024:
                        raise ValueError("Invalid request size")
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                    result = broker.request(token, json.loads(self.rfile.read(size)))
                    payload, status = json.dumps(result).encode(), 200
                except Exception as exc:
                    payload = json.dumps({"error": type(exc).__name__}).encode()
                    status = 403 if isinstance(exc, PermissionError) else 400
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="rrsi-model-broker", daemon=True)
        self.thread.start()
        return f"http://{host}:{self.server.server_address[1]}/turn"

    def close(self):
        with self._lock:
            self.capabilities.clear()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)
