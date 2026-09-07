#!/usr/bin/env python3
"""Shared runtime config for boss-zhipin-automation.

Reads personal settings from (highest priority first):
  1. environment variables
  2. ~/.config/boss-zhipin/config.json

Nothing personal is hard-coded here. Create your own config file:

  mkdir -p ~/.config/boss-zhipin
  cat > ~/.config/boss-zhipin/config.json <<'EOF'
  {
    "my_uid": 12345678,
    "city_code": "<BOSS城市码，如 101010100>",
    "preferred_districts": ["你的区"],
    "state_path": "~/.hermes/scripts/boss-zhipin-state.json"
  }
  EOF

Environment variable equivalents: BOSS_MY_UID, BOSS_CITY_CODE,
BOSS_PREFERRED_DISTRICTS (comma-separated), BOSS_STATE_PATH.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.environ.get("BOSS_CONFIG", Path.home() / ".config" / "boss-zhipin" / "config.json"))


def _load_file() -> dict[str, Any]:
    try:
        d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _get(key: str, env: str, default: Any) -> Any:
    v = os.environ.get(env)
    if v:
        return v
    return _load_file().get(key, default)


def my_uid() -> int:
    """Own BOSS geek uid (message-direction baseline)."""
    return int(_get("my_uid", "BOSS_MY_UID", 0))


def city_code() -> str:
    """BOSS city code for job search."""
    return str(_get("city_code", "BOSS_CITY_CODE", ""))


def preferred_districts() -> tuple[str, ...]:
    v = _get("preferred_districts", "BOSS_PREFERRED_DISTRICTS", None)
    if isinstance(v, str):
        return tuple(x.strip() for x in v.split(",") if x.strip())
    if isinstance(v, list):
        return tuple(str(x) for x in v)
    return ()


def state_path() -> Path:
    """Path to boss-zhipin-state.json (cookies store)."""
    p = str(_get("state_path", "BOSS_STATE_PATH",
                 Path.home() / ".hermes" / "scripts" / "boss-zhipin-state.json"))
    return Path(os.path.expanduser(p))
