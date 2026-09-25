"""Fixed RRSI version-dispatch seam: before_main_llm_call."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("before_main_llm_call", self.agent, **kwargs)

