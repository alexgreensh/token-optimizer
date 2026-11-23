"""Dispatch for the POC savings commands. One lazy import per command."""
import importlib

COMMANDS = {
    "counted-total": "counted_headline",
    "apply-safe": "apply_safe",
    "autocompact-advice": "autocompact_advice",
    "cold-guard": "cold_guard",
    "keepwarm-decide": "keepwarm_auto",
    "next": "next_actions",
}

def run(facade, args):
    """Run args[0]. Returns an exit code. Never raises."""
    try:
        module = importlib.import_module("." + COMMANDS[args[0]], __package__)
        return int(module.main(facade, list(args[1:])) or 0)
    except Exception as err:
        print("Token Optimizer: {} failed ({}).".format(args[0], type(err).__name__))
        return 1

REFRESH_MODULES = ("counted_closes", "cold_guard", "keepwarm_auto", "next_actions")

def on_savings_refresh(facade, summary):
    """Give each lane one isolated call after a full savings summary was computed. Never raises."""
    for name in REFRESH_MODULES:
        try:
            hook = getattr(importlib.import_module("." + name, __package__), "on_savings_refresh", None)
            if hook is not None:
                hook(facade, summary)
        except Exception:
            pass

STATUS_KEYS = (("counted_headline", "counted_headline", "cache_value"), ("next_action", "next_actions", "status_action"))

def status_extras(facade, session_id):
    """Top-level keys for the status-bar cache. A missing function or any error gives None for that key."""
    out = {}
    for key, name, attr in STATUS_KEYS:
        try:
            out[key] = getattr(importlib.import_module("." + name, __package__), attr)(facade, session_id)
        except Exception:
            out[key] = None
    return out
