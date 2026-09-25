"""Fixed RRSI version-dispatch seam: util_model_call_after."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("util_model_call_after", self.agent, **kwargs)

