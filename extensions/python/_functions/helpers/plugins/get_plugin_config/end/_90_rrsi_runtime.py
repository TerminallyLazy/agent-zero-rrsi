"""Fixed RRSI version-dispatch seam: plugin_config."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_sync


class RrsiRuntime(Extension):
    def execute(self, **kwargs):
        handle_sync("plugin_config", self.agent, **kwargs)

