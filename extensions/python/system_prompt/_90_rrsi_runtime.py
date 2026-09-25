"""Fixed RRSI version-dispatch seam: system_prompt."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("system_prompt", self.agent, **kwargs)

