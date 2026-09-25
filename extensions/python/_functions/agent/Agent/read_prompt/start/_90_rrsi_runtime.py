"""Fixed RRSI version-dispatch seam: read_prompt."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_sync


class RrsiRuntime(Extension):
    def execute(self, **kwargs):
        handle_sync("read_prompt", self.agent, **kwargs)

