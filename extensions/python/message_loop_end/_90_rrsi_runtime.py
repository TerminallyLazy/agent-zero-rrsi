"""Fixed RRSI version-dispatch seam: message_loop_end."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("message_loop_end", self.agent, **kwargs)

