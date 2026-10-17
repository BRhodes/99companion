"""The launcher's settings (launcher.local.json, beside the README): what start.cmd's window and the dashboard's
Settings > Game set up.

    {"tint": false}      the relay runs with --tint-store (vendor backgrounds); the supervisor restarts it on a change
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "launcher.local.json"


def settings(path=None):
    try:
        got = json.loads(Path(path or SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        got = {}
    return got if isinstance(got, dict) else {}


def save_settings(values, path=None):
    now = settings(path)
    now.update(values)
    Path(path or SETTINGS).write_text(json.dumps(now, indent=1), encoding="utf-8")
    return now


def relay_args(values=None):
    """The relay's command-line flags for these settings."""
    values = settings() if values is None else values
    return ["--tint-store"] if values.get("tint") else []
