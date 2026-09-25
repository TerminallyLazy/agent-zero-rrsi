"""Fixed RRSI version-dispatch seam: parse_prompt."""
from helpers.extension import Extension
from usr.plugins.rrsi.helpers.runtime_adapters import handle_sync


class RrsiRuntime(Extension):
    def execute(self, **kwargs):
        handle_sync("parse_prompt", self.agent, **kwargs)

