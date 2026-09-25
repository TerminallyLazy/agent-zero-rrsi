"""Fixed RRSI version-dispatch seam: chat_model_call_before."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("chat_model_call_before", self.agent, **kwargs)

