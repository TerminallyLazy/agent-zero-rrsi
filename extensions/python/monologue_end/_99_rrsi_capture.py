"""Capture only completed future foreground interactions, after sanitation."""
from helpers.extension import Extension


class RrsiCapture(Extension):
    async def execute(self, **kwargs):
        if self.agent is None:
            return
        response = self.agent.data.pop("rrsi_completed_response", None)
        if response is None:
            return
        from usr.plugins.rrsi.helpers.capture import completed_interaction
        from usr.plugins.rrsi.helpers.state import StateStore
        try:
            completed_interaction(self.agent, response=response)
        except Exception as exc:
            # Capture must not break ordinary conversations or log private text.
            StateStore().event("capture_failed", error_type=type(exc).__name__)
