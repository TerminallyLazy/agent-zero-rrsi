"""Fixed RRSI version-dispatch seam: agent_init."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_sync


class RrsiRuntime(Extension):
    def execute(self, **kwargs):
        handle_sync("agent_init", self.agent, **kwargs)

