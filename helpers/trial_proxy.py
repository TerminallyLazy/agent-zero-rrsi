"""Model interface for network-disabled trials; talks only over the relay socket."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import socket
import threading


def broker_request(request: dict, *, payload: dict | None = None) -> dict:
    payload = payload or json.loads(Path("/rrsi-input/input.json").read_text())
    envelope = {"token": payload["broker_token"], "request": request}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(payload.get("timeout_seconds", 600))
        conn.connect("/rrsi-broker/broker.sock")
        conn.sendall(json.dumps(envelope, allow_nan=False).encode() + b"\n")
        with conn.makefile("rb") as stream:
            result = json.loads(stream.readline(32 * 1024 * 1024))
    if result["status"] != 200:
        raise RuntimeError("RRSI broker: " + result["body"].get("error", "call_failed"))
    return result["body"]


def embedding_response(texts: list[str], result: dict) -> dict:
    """Validate the accounting boundary before exposing standard OpenAI vectors."""
    vectors = result.get("vectors")
    receipt = result.get("rrsi_receipt") or {}
    if (receipt.get("role") != "embedding" or type(receipt.get("input_tokens")) is not int
            or receipt["input_tokens"] < 0 or receipt.get("output_tokens") != 0
            or receipt.get("reported") is not True):
        raise ValueError("Remote embedding result lacks a reported usage receipt")
    if not isinstance(vectors, list) or len(vectors) != len(texts) or not vectors:
        raise ValueError("Remote embedding count mismatch")
    dimension = len(vectors[0]) if isinstance(vectors[0], list) else 0
    if not 0 < dimension <= 65536 or any(not isinstance(v, list) or len(v) != dimension
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in v) for v in vectors):
        raise ValueError("Remote embedding dimensions or values are invalid")
    return {"object": "list", "model": "rrsi-embedding", "data": [
        {"object": "embedding", "index": i, "embedding": vector} for i, vector in enumerate(vectors)],
        "usage": {"prompt_tokens": receipt["input_tokens"], "total_tokens": receipt["input_tokens"]}}


class EmbeddingBridge:
    """Loopback OpenAI embedding API for native memory's unhooked model factory."""
    def __init__(self, payload: dict):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                status = 200
                try:
                    if self.path not in {"/v1/embeddings", "/embeddings"}:
                        raise ValueError("Unknown local embedding route")
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 16 * 1024 * 1024:
                        raise ValueError("Embedding payload is empty or oversized")
                    body = json.loads(self.rfile.read(length))
                    texts = body.get("input")
                    if isinstance(texts, str):
                        texts = [texts]
                    if (body.get("model") != "rrsi-embedding" or not isinstance(texts, list)
                            or not 0 < len(texts) <= 512 or any(not isinstance(t, str) for t in texts)):
                        raise ValueError("Invalid local embedding request")
                    result = broker_request({"role": "embedding", "texts": texts}, payload=payload)
                    response = embedding_response(texts, result)
                except Exception as exc:
                    status, response = 502, {"error": {"message": type(exc).__name__, "type": "rrsi_embedding_error"}}
                encoded = json.dumps(response, allow_nan=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class BrokerModel:
    def __init__(self, role: str, identity: dict):
        self.role = role
        self.model_name = f"{identity.get('provider')}/{identity.get('name')}"
        self.provider = identity.get("provider", "")
        self.kwargs = dict(identity.get("parameters") or {})
        self.kwargs.update(responses_state="local", responses_delete_on_chat_delete=False)
        self.a0_model_conf = None

    def _convert_messages(self, messages, explicit_caching=False):
        from models import LiteLLMChatWrapper
        return LiteLLMChatWrapper._convert_messages(self, messages, explicit_caching=explicit_caching)

    async def unified_turn(self, system_message="", user_message="", messages=None,
                           response_callback=None, reasoning_callback=None, **kwargs):
        from helpers.llm_result import LLMResult
        from langchain_core.messages import SystemMessage, HumanMessage
        values = list(messages or [])
        if system_message: values.insert(0, SystemMessage(content=system_message))
        if user_message: values.append(HumanMessage(content=user_message))
        safe = self._convert_messages(values)
        request = {
            "role":self.role,"messages":safe,"max_tokens":int(self.kwargs.get("max_tokens") or 8192),
            "kwargs":{k:v for k,v in kwargs.items() if k in {"a0_responses_function_tools","responses_local_input_items","responses_input_items","previous_response_id"}}}
        result_data = await asyncio.to_thread(broker_request, request)
        result = LLMResult.from_dict(result_data)
        if response_callback and result.response:
            await response_callback(result.response,result.response)
        if reasoning_callback and result.reasoning:
            await reasoning_callback(result.reasoning,result.reasoning)
        return result

    async def unified_call(self, **kwargs):
        result = await self.unified_turn(**kwargs)
        return result.response, result.reasoning


def model_for(role: str):
    payload = json.loads(Path("/rrsi-input/input.json").read_text())
    return BrokerModel(role, payload["model_identity"][role])


async def intercept_native_call(data: dict, method: str) -> None:
    """Cover direct native utility/vision wrappers as well as Agent getters.

    Unrecognized provider wrappers fail closed rather than attempting a request
    outside the frozen model capability. The container has no network regardless.
    """
    args = tuple(data.get("args", ()))
    if not args:
        raise RuntimeError("Native model invocation has no model")
    model = args[0]
    name = str(getattr(model, "model_name", ""))
    roles = {"openai/rrsi-" + role: role for role in ("policy", "utility", "vision")}
    role = roles.get(name)
    if role is None:
        raise RuntimeError("Trial model is outside the frozen provider configuration")
    names = ("system_message", "user_message", "messages", "response_callback", "reasoning_callback",
             "tokens_callback", "rate_limiter_callback", "explicit_caching")
    kwargs = dict(zip(names, args[1:]))
    kwargs.update(data.get("kwargs", {}))
    data["result"] = await getattr(model_for(role), method)(**kwargs)
    data["exception"] = None
