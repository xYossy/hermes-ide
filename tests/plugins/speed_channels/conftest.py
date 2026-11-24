"""Shared fixtures: an in-memory transport for the Shelly plugs and Discord,
a real-schema temp state.db, and a scripted 1.1.1.1 ping.

Follows the ``http_fn`` DI style of tests/plugins/quota_channels — no respx, no
real network. Every credential here is short, fake, and unroutable.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import datetime

import pytest

from plugins.speed_channels.core import (
    DISCORD_TOKEN_ENV_KEY,
    SpeedChannelsError,
)

# Short, obviously-fake values — well under any real credential length.
FAKE_TOKEN_VALUE = "tok" + "en-fake"
SHELLY_A = "10.0.0.11"
SHELLY_B = "10.0.0.12"
SHELLY_DEAD = "10.0.0.13"

# A fixed wall clock so "due" arithmetic and label timestamps are deterministic.
FIXED_NOW = 1_700_000_000.0
# Local midnight of FIXED_NOW's day, the cut-off for "today" in the databases.
TODAY = datetime.fromtimestamp(FIXED_NOW).replace(
    hour=0, minute=0, second=0, microsecond=0
).timestamp()

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


class FakeTransport:
    """Callable transport: Shelly plugs by LAN host and Discord."""

    def __init__(self):
        self.watts = {SHELLY_A: 40.0, SHELLY_B: 23.0}
        self.dead = {SHELLY_DEAD}  # raise like a timed-out plug

        self.channels = {
            "cp": {"id": "cp", "name": "Power"},
            "cw": {"id": "cw", "name": "WAN"},
            "cd": {"id": "cd", "name": "Spend"},
            "ca": {"id": "ca", "name": "Agents"},
            "cat": {"id": "cat", "name": "Speeds"},
        }
        self.patch_status = 200  # override to simulate 429/403 on PATCH

        self.requests = []  # (method, url, headers-lower, body)
        self.patched = []  # (channel_id, new_name)

    @property
    def patch_calls(self):
        return len(self.patched)

    def _record(self, req):
        headers = {k.lower(): v for k, v in req.headers.items()}
        body = req.data.decode() if req.data else ""
        self.requests.append((req.get_method().upper(), req.full_url, headers, body))
        return headers

    def __call__(self, req, timeout=20.0):
        self._record(req)
        parsed = urllib.parse.urlsplit(req.full_url)
        if parsed.netloc in self.dead:
            raise SpeedChannelsError(f"network error: timed out ({parsed.netloc})")
        if parsed.netloc in self.watts:
            return self._json({"switch:0": {"apower": self.watts[parsed.netloc]}})
        return self._discord(req, parsed.path)

    def _json(self, payload, status=200, headers=None):
        return status, json.dumps(payload).encode(), dict(headers or {})

    def _discord(self, req, path):
        if path.startswith("/api/v10"):
            path = path[len("/api/v10"):]
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "channels":
            return self._json({"message": "unrouted"}, status=404)
        chan = self.channels.get(parts[1])
        if chan is None:
            return self._json({"message": "Unknown Channel"}, status=404)
        if req.get_method().upper() == "PATCH":
            if self.patch_status != 200:
                return self.patch_status, b'{"message": "rate limited"}', {}
            chan["name"] = json.loads(req.data)["name"]
            self.patched.append((chan["id"], chan["name"]))
        return self._json(chan)


def add_usage(db_path, *, session, last_seen, calls=0, input_tokens=0, output_tokens=0):
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO session_model_usage (session_id, model, billing_provider,"
            " billing_base_url, billing_mode, task, api_call_count, input_tokens,"
            " output_tokens, last_seen) VALUES (?, 'm', 'p', 'u', 'b', 't', ?, ?, ?, ?)",
            (session, calls, input_tokens, output_tokens, last_seen),
        )
    con.close()


def add_delegation(db_path, *, delegation_id, state, completed_at=None):
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO async_delegations (delegation_id, state, dispatched_at,"
            " completed_at) VALUES (?, ?, ?, ?)",
            (delegation_id, state, FIXED_NOW - 60, completed_at),
        )
    con.close()


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    """A temp HERMES_HOME with the fake Discord token and a speed_channels config."""
    home = tmp_path / "hermes"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "discord.env").write_text(
        f"{DISCORD_TOKEN_ENV_KEY}={FAKE_TOKEN_VALUE}\n", encoding="utf-8"
    )
    (home / "config.yaml").write_text(
        "speed_channels:\n"
        "  guild_id: \"900000000000000001\"\n"
        "  category_id: cat\n"
        f"  shelly_hosts: [\"{SHELLY_A}\", \"{SHELLY_B}\", \"{SHELLY_DEAD}\"]\n"
        "  channel_ids: {power: cp, wan: cw, spend: cd, agents: ca}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def state_db(hermes):
    """Hermes's state.db under the temp HERMES_HOME, with the real table shapes."""
    path = hermes / "state.db"
    with sqlite3.connect(path) as con:
        con.execute(USAGE_SCHEMA)
        con.execute(DELEGATIONS_SCHEMA)
    con.close()
    return path


@pytest.fixture
def config(hermes):
    from plugins.speed_channels.core import load_speed_config

    return load_speed_config()  # reads HERMES_HOME/config.yaml from `hermes`


class FakePing:
    """Stand-in for ``default_ping``: scripted RTT in ms, counts calls, and
    never touches the network or a real ping binary."""

    def __init__(self, value=33.3):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def ping():
    return FakePing()


@pytest.fixture
def now():
    return lambda: FIXED_NOW
