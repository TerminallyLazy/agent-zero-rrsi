"""Authenticated, CSRF-protected native controller endpoint."""
import asyncio

from helpers.api import ApiHandler, Input, Output, Request, Response
from usr.plugins.rrsi.helpers.service import display_evidence, get_service
from usr.plugins.rrsi.helpers.state import BusyError


class Rrsi(ApiHandler):
    async def process(self, input: Input, request: Request) -> Output:
        # The framework caches wrapped handlers by path. Recheck the method here
        # so a cached POST route cannot turn a later GET into a mutation.
        if request.method != "POST":
            return Response("Method not allowed",405)
        if not isinstance(input, dict) or not isinstance(input.get("action"), str):
            return {"success": False, "error": "An action string is required", "error_type": "ValueError"}
        service = get_service()
        try:
            # Preflight, state locks and shutdown must not block the API event loop.
            result = await asyncio.to_thread(service.dispatch, input["action"], input)
            return {"success": True, "data": result}
        except (ValueError, BusyError) as exc:
            return {"success": False, "error": display_evidence(str(exc)), "error_type": type(exc).__name__}
        except Exception as exc:
            # Provider/subprocess exceptions may contain credentials or private paths.
            service.store.event("api_failure", error_type=type(exc).__name__)
            return {"success": False, "error": "RRSI operation failed; inspect local state", "error_type": type(exc).__name__}
