"""Load API credentials from Claude settings.json + shell env. Single source of truth."""
import json
import os
import pathlib
from typing import Optional

_SETTINGS_PATHS = [
    pathlib.Path.home() / ".claude/settings.json",
    pathlib.Path.home() / ".claude/settings.local.json",
]


def _load_settings_env() -> dict:
    merged = {}
    for p in _SETTINGS_PATHS:
        if p.exists():
            try:
                merged.update(json.loads(p.read_text()).get("env", {}) or {})
            except Exception:
                pass
    return merged


_SETTINGS = _load_settings_env()


def get(key: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve in order: process env, settings.json env, default."""
    return os.environ.get(key) or _SETTINGS.get(key) or default


def require(key: str) -> str:
    v = get(key)
    if not v:
        raise RuntimeError(
            f"Missing required credential: {key}. "
            f"Set it in ~/.claude/settings.json env or as shell env var."
        )
    return v


# Convenience accessors
def dataforseo_auth() -> str:
    """Returns the Basic-auth header value (already base64-encoded)."""
    return require("DATAFORSEO_BASIC_AUTH")


def apify_token() -> str:
    return require("APIFY_TOKEN")


def youtube_api_key() -> str:
    return require("YOUTUBE_API_KEY")


