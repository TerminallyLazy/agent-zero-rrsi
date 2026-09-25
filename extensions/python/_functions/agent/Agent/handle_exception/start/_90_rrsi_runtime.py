"""Fixed RRSI version-dispatch seam: handle_exception."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_async


class RrsiRuntime(Extension):
    async def execute(self, **kwargs):
        await handle_async("handle_exception", self.agent, **kwargs)

