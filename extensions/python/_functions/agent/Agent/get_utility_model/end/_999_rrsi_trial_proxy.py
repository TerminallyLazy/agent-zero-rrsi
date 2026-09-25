import os
from helpers.extension import Extension

class TrialModel(Extension):
    def execute(self, data, **kwargs):
        if os.environ.get("RRSI_TRIAL") == "1":
            from usr.plugins.rrsi.helpers.trial_proxy import model_for
            data["result"] = model_for('utility')
            data["exception"] = None
