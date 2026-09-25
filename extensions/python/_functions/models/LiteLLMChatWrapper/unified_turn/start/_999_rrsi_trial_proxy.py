import os
from helpers.extension import Extension

class TrialModelCall(Extension):
    async def execute(self, data, **kwargs):
        if os.environ.get("RRSI_TRIAL") == "1":
            from usr.plugins.rrsi.helpers.trial_proxy import intercept_native_call
            await intercept_native_call(data, 'unified_turn')
