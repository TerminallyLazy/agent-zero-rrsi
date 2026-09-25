"""Deployment checks run after scientific selection, using separate fixed tasks."""
from __future__ import annotations

import json
import secrets
import uuid

from usr.plugins.rrsi.helpers.artifacts import ArtifactStore
from usr.plugins.rrsi.helpers.broker import POLICY_ROLES
from usr.plugins.rrsi.helpers.contracts import TaskSpec, identifier
from usr.plugins.rrsi.helpers.state import StateStore, canonical_hash, utc_now
from usr.plugins.rrsi.helpers.tasks import grade


def validate_and_activate(store: StateStore, campaign_id: str, *, engine=None) -> dict:
    identifier(campaign_id)
    if engine is None:
        from usr.plugins.rrsi.helpers.engine import active_session
        engine = active_session(store,campaign_id)
    with store.lock("deployment",blocking=False):
        engine.check()
        version,artifact=engine.final_artifact()
        expected_environment=store.read_json(f"campaigns/{campaign_id}/sandbox.json")
        current_environment=engine.sandbox.preflight()
        if expected_environment != current_environment:
            raise ValueError("Framework or Docker environment changed before activation")
        prefix=f"campaigns/{campaign_id}/deployment"
        measurements=[]
        # Fixed functional shape with fresh payload; neither task nor score enters
        # the upstream evolution history or the held-out performance report.
        nonce=secrets.token_hex(8)
        tasks=[TaskSpec("canary_response","canary_response","transfer",
                    f'Return exactly JSON {{"nonce":"{nonce}"}} using the response tool.',
                    {"kind":"json","expected":{"nonce":nonce}},source="activation_canary"),
               TaskSpec("canary_file","canary_file","transfer",
                    "Read /work/canary.json and return only its payload object as JSON.",
                    {"kind":"json","expected":{"value":19,"state":"ready"}},
                    fixtures={"canary.json":'{"payload":{"value":19,"state":"ready"}}'},source="activation_canary")]
        for task in tasks:
            engine.check()
            trial_id="canary-"+uuid.uuid4().hex
            token=engine.broker.grant(set(POLICY_ROLES),trial_id=trial_id,ttl=engine.config.trial_timeout_seconds+120)
            output=store.path(f"{prefix}/{trial_id}")
            try:
                result=engine.sandbox.trial(task,artifact,output,broker_token=token,trial_id=trial_id,
                    model_identity=engine.provider.identity(),runtime_config=engine.raw_config.get("runtime_config",{}),canary=True)
            finally:
                engine.broker.revoke(token)
            usage=engine.broker.ledger.trial_usage(trial_id)
            evaluation=grade(task,result.get("response",""),output/"files")
            checks=result.get("canary") or {}
            passed=(result.get("valid") is True and usage["complete"] and usage["tokens"]>0 and
                    evaluation["valid"] and evaluation["reward"]==1 and checks.get("version")==version and
                    all(checks.get(key) is True for key in ("compatibility","runtime_canary","tool_policy","response_termination")))
            measurement={"trial_id":trial_id,"task_hash":canonical_hash(task.to_dict()),"artifact":version,
                "model_hash":canonical_hash(engine.provider.identity()),"result":result,"usage":usage,
                "grade":evaluation,"passed":bool(passed)}
            store.write_json(f"{prefix}/{trial_id}/canary-receipt.json",measurement)
            measurements.append(measurement)
            if not passed:
                store.event("activation_canary_failed",campaign_id=campaign_id,version=version,trial_id=trial_id)
                raise ValueError("Functional activation canary failed; incumbent remains unpublished")
        engine.check()
        checks={key:all(m["result"]["canary"][key] is True for m in measurements)
                for key in ("compatibility","runtime_canary","tool_policy","response_termination")}
        checks.update(evidence_hash=canonical_hash(measurements),environment_hash=canonical_hash(current_environment))
        store.write_json(f"{prefix}/receipt.json",{"at":utc_now(),"version":version,"checks":checks,
                         "trials":[m["trial_id"] for m in measurements]})
        receipt=ArtifactStore(store,engine.metadata["framework_revision"]).activate(version,campaign=campaign_id,
            checks=checks,expected_version=engine.metadata.get("starting_version","baseline"))
        if receipt.get("skipped"):
            raise ValueError("Active version changed during the campaign; deployment was not applied")
        return receipt
