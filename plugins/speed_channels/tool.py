"""speed_channels_tick tool registration and handler."""

from __future__ import annotations

import json
from typing import Any

from plugins.speed_channels.core import (
    SpeedChannelsError,
    check_minimum_config_from_mapping,
    load_speed_config,
    run_tick,
)

SPEED_CHANNELS_TICK_SCHEMA = {
    "name": "speed_channels_tick",
    "description": (
        "Run one Discord vitals tick: rewrite the Power, WAN, Spend and Agents "
        "voice-channel names (self-gated; the WAN rate comes from this host's "
        "uplink counters) and refresh the Speeds category label (live 1.1.1.1 "
        "ICMP latency plus the next-poll countdown)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "force": {
                "type": "boolean",
                "description": "Bypass the channel-poll gate.",
            }
        },
        "additionalProperties": False,
    },
}


def _load_config_mapping() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly()
    except Exception:
        return {}


def check_speed_channels_requirements() -> bool:
    """True when minimum speed_channels config exists (no network/secrets)."""
    return check_minimum_config_from_mapping(_load_config_mapping())


def handle_speed_channels_tick(args: dict, **_: Any) -> str:
    force = bool(args.get("force"))
    try:
        config = load_speed_config()
        result = run_tick(config, force=force)
        return json.dumps(result, separators=(",", ":"))
    except SpeedChannelsError as exc:
        return json.dumps({"success": False, "error": str(exc)}, separators=(",", ":"))
