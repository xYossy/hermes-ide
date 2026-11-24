#!/usr/bin/env python3
"""Offline self-check for speed_channels: no pytest, no network, no Discord.

Run ``python3 plugins/speed_channels/selfcheck.py``; exit 0 means every reader
and the config validator behave as documented.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path


def _bootstrap_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


_bootstrap_path()
HOME = Path(tempfile.mkdtemp(prefix="speed-selfcheck-"))
os.environ["HERMES_HOME"] = str(HOME)

from plugins.speed_channels import core  # noqa: E402

NOW = 1_700_000_000.0
TODAY = (
    datetime.fromtimestamp(NOW)
    .replace(hour=0, minute=0, second=0, microsecond=0)
    .timestamp()
)
PLUG_A, PLUG_B, PLUG_DEAD = "10.0.0.11", "10.0.0.12", "10.0.0.13"

USAGE_SCHEMA = """
CREATE TABLE session_model_usage (
    session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
    billing_mode TEXT, task TEXT,
    api_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL, actual_cost_usd REAL,
    cost_status TEXT, cost_source TEXT,
    first_seen REAL, last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
)
"""
DELEGATIONS_SCHEMA = """
CREATE TABLE async_delegations (
    delegation_id TEXT PRIMARY KEY,
    origin_session TEXT, origin_ui_session_id TEXT, parent_session_id TEXT,
    state TEXT, dispatched_at REAL, completed_at REAL, updated_at REAL
)
"""


class FakeHttp:
    """Routes by LAN/WAN host. A host mapped to an Exception raises it."""

    def __init__(self, routes):
        self.routes = routes

    def __call__(self, req, timeout=20.0):
        host = urllib.parse.urlsplit(req.full_url).netloc
        route = self.routes[host]
        if isinstance(route, Exception):
            raise route
        status, body = route
        return status, body, {}


def plug(watts: float) -> tuple:
    return 200, f'{{"switch:0": {{"apower": {watts}}}}}'.encode()


def check_fmt_tokens() -> None:
    assert core.fmt_tokens(999) == "999"
    assert core.fmt_tokens(12_400) == "12.4k"
    assert core.fmt_tokens(9_725_717) == "9.7M"


def check_shelly() -> None:
    http = FakeHttp({PLUG_A: plug(40.0), PLUG_B: plug(23.0), PLUG_DEAD: (500, b"busy")})
    watts, online = core.shelly_watts([PLUG_A, PLUG_B, PLUG_DEAD], http)
    assert abs(watts - 63.0) < 1e-9, watts
    assert online == 2, online

    assert core.power_name([], http) == "Power: no plugs configured"
    dead = FakeHttp({PLUG_DEAD: (500, b"busy")})
    assert core.power_name([PLUG_DEAD], dead) == "Power: unreachable"
    assert core.power_name([PLUG_A, PLUG_B, PLUG_DEAD], http) == "Power: 63W · 2/3 online"


def check_wan() -> None:
    slept = []
    counters = iter([(0, 0), (1_500_000, 375_000)])
    down, up = core.wan_rates(
        "eth0", counters_fn=lambda _iface: next(counters), sleep_fn=slept.append
    )
    assert abs(down - 4.0) < 1e-9, down
    assert abs(up - 1.0) < 1e-9, up
    assert slept == [core.WAN_WINDOW_SECONDS], slept

    assert core.wan_name("eth0", rates_fn=lambda _i: (24.4, 3.1)) == (
        "WAN: ↓ 24.4 ↑ 3.1 Mbps"
    )
    assert core.wan_name("eth0", rates_fn=lambda _i: (0.0, 0.0)) == "WAN: idle"
    assert core.wan_name("eth0", rates_fn=lambda _i: (0.075, 0.011)) == "WAN: ↓ 75 ↑ 11 kbit/s"
    assert core.wan_name("eth0", rates_fn=lambda _i: (0.012, 0.0)) == "WAN: ↓ 12 ↑ 0 kbit/s"
    assert core.wan_name("") == "WAN: no uplink found"



def check_spend_and_agents() -> None:
    db = HOME / core.STATE_DB_FILENAME
    with sqlite3.connect(db) as con:
        con.execute(USAGE_SCHEMA)
        con.execute(DELEGATIONS_SCHEMA)
        con.execute(
            "INSERT INTO session_model_usage (session_id, model, billing_provider,"
            " billing_base_url, billing_mode, task, api_call_count, input_tokens,"
            " output_tokens, last_seen) VALUES ('s1','m','p','u','b','t',10,9500000,225717,?)",
            (TODAY + 100,),
        )
        con.execute(
            "INSERT INTO session_model_usage (session_id, model, billing_provider,"
            " billing_base_url, billing_mode, task, api_call_count, last_seen)"
            " VALUES ('s2','m','p','u','b','t',1685,?)",
            (TODAY + 200,),
        )
        con.execute(
            "INSERT INTO async_delegations (delegation_id, state, completed_at)"
            " VALUES ('d1','running',NULL), ('d2','completed',?)",
            (TODAY + 50,),
        )
    con.close()

    assert core.spend_name(str(db), now_fn=lambda: NOW) == "Spend: 9.7M tok · 1695 calls"
    assert core.agents_name(str(db), now_fn=lambda: NOW) == "Agents: 1 running · 1 done"

    missing = str(HOME / "missing.db")
    assert core.spend_name(missing, now_fn=lambda: NOW) == "Spend: db unreadable"
    assert core.agents_name(missing, now_fn=lambda: NOW) == "Agents: db unreadable"


def check_config() -> None:
    full = {
        "guild_id": "900000000000000001",
        "category_id": "cat",
        "channel_ids": {"power": "cp", "wan": "cw", "spend": "cd", "agents": "ca"},
    }
    resolved = core.validate_speed_config(full)
    assert resolved["wan_interface"] == "", resolved
    assert resolved["channel_ids"]["agents"] == "ca"

    partial = dict(full, channel_ids={"power": "cp", "wan": "cw", "spend": "cd"})
    try:
        core.validate_speed_config(partial)
    except core.SpeedChannelsError as exc:
        assert "agents" in str(exc), exc
    else:
        raise AssertionError("a missing agents channel id must be rejected")


def main() -> int:
    try:
        check_fmt_tokens()
        check_shelly()
        check_wan()
        check_spend_and_agents()
        check_config()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print("speed_channels selfcheck: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
