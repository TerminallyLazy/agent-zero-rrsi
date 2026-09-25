"""Fixed RRSI version-dispatch seam: get_tool."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_sync


class RrsiRuntime(Extension):
    def execute(self, **kwargs):
        handle_sync("get_tool", self.agent, **kwargs)

