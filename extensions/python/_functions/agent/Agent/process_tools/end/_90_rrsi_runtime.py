"""Fixed RRSI version-dispatch seam: process_tools_after."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("process_tools_after", self.agent, **kwargs)

