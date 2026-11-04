"""Shared paths and settings for claude-cost-proxy."""
import json
import os
from pathlib import Path
from typing import Optional

DEFAULT_UPSTREAM = "https://api.anthropic.com"


def base_dir() -> Path:
    """Directory holding config, logs and cache.

    Defaults to ~/.throttle (kept for backwards compatibility). Override with
    the CLAUDE_PROXY_HOME environment variable.
    """
    override = os.getenv("CLAUDE_PROXY_HOME")
    return Path(override).expanduser() if override else Path.home() / ".throttle"


def log_dir() -> Path:
    return base_dir() / "logs"


def cache_dir() -> Path:
    return base_dir() / "cache"


def config_file() -> Path:
    return base_dir() / "config.json"


def upstream_url() -> str:
    """Where requests are forwarded. Override with CLAUDE_PROXY_UPSTREAM."""
    return os.getenv("CLAUDE_PROXY_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/")


def load_api_key() -> Optional[str]:
    """API key from the environment, falling back to the saved config file."""
    env_key = os.getenv("ANTHROPIC_API_KEY")
    if env_key:
        return env_key
    path = config_file()
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            return json.load(f).get("api_key")
    except (json.JSONDecodeError, OSError):
        return None
