"""Observe native response termination for isolated activation checks."""
import os
from helpers.extension import Extension


class RrsiCanaryTerminal(Extension):
    async def execute(self, response=None, tool_name="", **kwargs):
        if os.environ.get("RRSI_TRIAL") == "1" and self.agent and response is not None and response.break_loop:
            self.agent.context.set_data("rrsi_canary_terminal",{
                "tool_name":tool_name,"break_loop":bool(response.break_loop)})
