"""Fixed RRSI version-dispatch seam: monologue_start."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("monologue_start", self.agent, **kwargs)

