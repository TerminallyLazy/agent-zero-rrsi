"""Mark completed response-tool output; monologue_end performs local admission."""
from helpers.extension import Extension


class RrsiCaptureComplete(Extension):
    async def execute(self, response=None, tool_name="", **kwargs):
        if self.agent and response is not None and response.break_loop:
            self.agent.data["rrsi_completed_response"] = str(response.message)
