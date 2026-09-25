"""Fixed RRSI version-dispatch seam: message_loop_prompts_before."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("message_loop_prompts_before", self.agent, **kwargs)

