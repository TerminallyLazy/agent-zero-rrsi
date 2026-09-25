"""Explicit lifecycle hooks. Dependencies are never installed from these hooks."""
from usr.plugins.rrsi.helpers.service import get_service, validate_settings


def install(**kwargs):
    # Installation keeps evaluation disabled until configuration and explicit setup.
    get_service().store.event("plugin_installed", dependency_install=False)
    return {"installed": True, "setup_required": True, "dependency_install": False}


def save_plugin_config(settings=None, project_name="", agent_profile="", **kwargs):
    if project_name or agent_profile:
        raise ValueError("RRSI configuration is instance-wide")
    result = validate_settings(settings or {})
    if not result.get("enabled", False):
        get_service().shutdown()
    return result


def uninstall(**kwargs):
    service = get_service()
    result = service.shutdown()
    if not result.get("stopped"):
        raise RuntimeError("RRSI workers are still stopping; retry uninstall after shutdown")
    service.store.set_active("baseline", "RRSI plugin uninstalled")
    service.store.event("plugin_uninstalled", state_retained=True)
    return {**result, "state_retained": True,
            "recovery": "Artifacts and pinned conversation versions are retained. Reinstall RRSI to restore its versioned conversation behavior."}


def cleanup(**kwargs):
    return uninstall(**kwargs)
