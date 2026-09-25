"""Use the native minute job tick to ensure the hourly RRSI supervisor is alive."""
import os
from helpers.extension import Extension


class RrsiSupervisor(Extension):
    async def execute(self, **kwargs):
        if os.environ.get("RRSI_TRIAL") == "1":
            return
        from usr.plugins.rrsi.helpers.service import get_service
        service = get_service()
        service.start_scheduler()
