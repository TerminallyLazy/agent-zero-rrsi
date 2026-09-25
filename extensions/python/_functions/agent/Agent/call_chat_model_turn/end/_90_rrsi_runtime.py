"""Fixed RRSI version-dispatch seam: call_chat_model_turn_after."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("call_chat_model_turn_after", self.agent, **kwargs)

