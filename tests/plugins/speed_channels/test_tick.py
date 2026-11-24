"""run_tick contracts: poll gating, the four vitals
renames, degradation per channel, the category label refresh that happens even
when the poll is skipped, and the best-effort 1.1.1.1 latency on that label."""

from __future__ import annotations

import json
import subprocess
import urllib.parse

import pytest

from conftest import FIXED_NOW, SHELLY_A, SHELLY_B, SHELLY_DEAD
from plugins.speed_channels import core
from plugins.speed_channels.core import (
    fmt_time,
    fmt_ts,
    load_state,
    run_tick,
)

VITALS = {"cp", "cw", "cd", "ca"}


class FakeWanRates:
    """Stand-in for ``wan_rates``: fixed down/up rates and a call counter."""

    def __init__(self):
        self.rates = (311.4, 3.1)
        self.calls = 0

    def __call__(self, iface):
        self.calls += 1
        return self.rates


@pytest.fixture(autouse=True)
def wan(monkeypatch):
    fake = FakeWanRates()
    monkeypatch.setattr(core, "wan_rates", fake)
    return fake


def test_first_tick_polls_and_renames_all_four_vitals_and_the_label(
    state_db, config, transport, now, ping, wan
):
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["success"] is True
    assert report["did_poll"] is True
    assert report["category"] == "renamed"
    assert report["latency_ms"] == 33.3
    assert set(report["names"]) == {"power", "wan", "spend", "agents"}

    patched = dict(transport.patched)
    assert patched["cp"] == "Power: 63W · 2/3 online"
    assert patched["cw"] == "WAN: ↓ 311.4 ↑ 3.1 Mbps"
    assert patched["cd"] == "Spend: 0 tok · 0 calls"
    assert patched["ca"] == "Agents: 0 running · 0 done"
    assert patched["cat"] == (
        f"Speeds • 33ms • {fmt_ts(now())}"
        f" • Next: {fmt_time(int(now()) + config['poll_interval_seconds'])}"
    )

    assert load_state()["last_poll_success"] == int(now())


def test_second_tick_within_interval_patches_nothing(state_db, config, transport, now, ping):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    patches_after_first = transport.patch_calls

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["did_poll"] is False
    # The ping still runs on a skipped-poll tick...
    assert ping.calls == 2
    # ...but the same 33.3ms sample is inside the 5ms hysteresis band, so the
    # label is byte-identical and zero PATCHes are issued this tick.
    assert transport.patch_calls == patches_after_first
    assert report["category"] == "unchanged"


def test_tick_after_interval_polls_again(state_db, config, transport, ping):
    clock = {"t": FIXED_NOW}
    now = lambda: clock["t"]  # noqa: E731

    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    clock["t"] += config["poll_interval_seconds"]

    assert run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)["did_poll"] is True


def test_force_polls_wan_even_when_not_due(
    state_db, config, transport, now, ping, wan
):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert wan.calls == 1

    report = run_tick(config, force=True, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["did_poll"] is True
    assert wan.calls == 2


def test_wan_is_reread_on_every_poll(state_db, config, transport, ping, wan):
    """Local counters are free, so WAN has no gate of its own any more."""
    clock = {"t": FIXED_NOW}
    now = lambda: clock["t"]  # noqa: E731

    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert wan.calls == 1

    clock["t"] += config["poll_interval_seconds"]
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert wan.calls == 2
    assert dict(transport.patched)["cw"] == "WAN: ↓ 311.4 ↑ 3.1 Mbps"


def test_broken_counters_degrade_only_the_wan_channel(
    state_db, config, transport, now, ping, wan, monkeypatch
):
    def boom(_iface):
        raise OSError("no such device")

    monkeypatch.setattr(core, "wan_rates", boom)

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["success"] is True
    patched = dict(transport.patched)
    assert patched["cw"] == "WAN: unreachable"
    assert patched["cp"] == "Power: 63W · 2/3 online"


def test_a_tick_never_leaves_the_box_for_the_wan_number(
    state_db, config, transport, now, ping
):
    """The rate comes from the kernel, so only LAN plugs and Discord are hit."""
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    hosts = {
        urllib.parse.urlsplit(url).netloc.split(":")[0]
        for _method, url, _headers, _body in transport.requests
    }

    assert hosts <= {SHELLY_A, SHELLY_B, SHELLY_DEAD, "discord.com"}


def test_dead_plugs_degrade_only_the_power_channel(state_db, config, transport, now, ping):
    transport.dead.update({SHELLY_A, SHELLY_B})

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["success"] is True
    patched = dict(transport.patched)
    assert patched["cp"] == "Power: unreachable"
    # Every other channel still renames.
    assert set(patched) == VITALS | {"cat"}
    assert patched["cw"].startswith("WAN: ↓ ")


def test_unreadable_database_degrades_only_spend_and_agents(
    config, transport, now, ping
):
    # No state_db fixture: the file does not exist, so the reads fail closed.
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["success"] is True
    patched = dict(transport.patched)
    assert patched["cd"] == "Spend: db unreadable"
    assert patched["ca"] == "Agents: db unreadable"
    assert patched["cp"] == "Power: 63W · 2/3 online"
    assert patched["cw"] == "WAN: ↓ 311.4 ↑ 3.1 Mbps"


def test_category_label_tracks_poll_success_even_when_poll_skipped(
    state_db, config, transport, now, ping
):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    label = dict(transport.patched)["cat"]
    assert label.startswith("Speeds • 33ms • ")
    assert label.endswith(f"Next: {fmt_time(int(now()) + config['poll_interval_seconds'])}")

    # Much later: the label flips to Due and the overdue poll runs.
    late = lambda: now() + 10 * config["poll_interval_seconds"]  # noqa: E731
    report = run_tick(config, now_fn=late, http_fn=transport, ping_fn=ping)
    assert report["did_poll"] is True
    assert dict(transport.patched)["cat"].endswith(
        f"Next: {fmt_time(int(late()) + config['poll_interval_seconds'])}"
    )


def test_latency_hysteresis_holds_small_jitter_and_holds_big_moves_until_the_next_poll(
    state_db, config, transport, ping
):
    clock = {"t": FIXED_NOW}
    now = lambda: clock["t"]  # noqa: E731

    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)  # polls, displays 33ms
    patches_after_first = transport.patch_calls

    # 36ms is within 5ms of the displayed 33.3 → keep 33, no category PATCH.
    ping.value = 36.0
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert report["did_poll"] is False
    assert report["latency_ms"] == 33.3
    assert report["pending_latency_ms"] is None
    assert "33ms" in dict(transport.patched)["cat"]
    assert transport.patch_calls == patches_after_first
    assert load_state()["last_latency_ms"] == 33.3

    # 40ms moves ≥5ms from the displayed value → accepted, but held: showing
    # it now would spend a latency-only PATCH the next timestamp update needs.
    ping.value = 40.0
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert report["did_poll"] is False
    assert report["latency_ms"] == 33.3  # label keeps the displayed value
    assert report["pending_latency_ms"] == 40.0
    assert "33ms" in dict(transport.patched)["cat"]
    assert transport.patch_calls == patches_after_first
    state = load_state()
    assert state["last_latency_ms"] == 33.3  # must NOT advance on a skipped poll
    assert state["pending_latency_ms"] == 40.0

    # Still no poll → the held value keeps waiting, still zero PATCHes.
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert report["did_poll"] is False
    assert report["pending_latency_ms"] == 40.0
    assert transport.patch_calls == patches_after_first

    # Next polling tick: the category PATCH carries the new timestamp AND the
    # held 40ms; the latency change rides the rename that happens anyway.
    clock["t"] += config["poll_interval_seconds"]
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert report["did_poll"] is True
    assert report["latency_ms"] == 40.0
    assert report["pending_latency_ms"] is None
    # The WAN channel keeps its reading on a poll (same rates), so only the
    # category PATCHes, carrying the held latency.
    new_patches = [cid for cid, _ in transport.patched[patches_after_first:]]
    assert new_patches == ["cat"]
    assert dict(transport.patched)["cat"] == (
        f"Speeds • 40ms • {fmt_ts(now())}"
        f" • Next: {fmt_time(int(now()) + config['poll_interval_seconds'])}"
    )


def test_ping_timeout_renders_but_never_fails_the_tick(
    state_db, config, transport, now, ping
):
    ping.value = None

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["success"] is True
    assert report["did_poll"] is True
    assert report["latency_ms"] is None
    # The vitals still rename; only the latency slot degrades.
    patched = dict(transport.patched)
    assert set(patched) == VITALS | {"cat"}
    assert patched["cat"].startswith("Speeds • timeout • ")
    state = load_state()
    assert state["last_poll_success"] == int(now())
    assert state["last_latency_ms"] is None


def test_state_file_is_written_under_hermes_home(state_db, hermes, config, transport, now, ping):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    data = json.loads((hermes / "speed_channels_state.json").read_text(encoding="utf-8"))
    assert data == {
        "last_poll_success": int(now()),
        "last_latency_ms": 33.3,
        "pending_latency_ms": None,  # a polling tick flushes, nothing held
    }


def test_skipped_poll_still_pings_and_holds_latency(state_db, config, transport, now, ping):
    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    ping.value = 60.0  # ≥5ms away → accepted move, but no poll to flush it on

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    assert report["did_poll"] is False
    assert ping.calls == 2
    assert report["latency_ms"] == 33.3  # the label keeps the displayed value
    assert report["pending_latency_ms"] == 60.0
    # Neither last_poll_success nor last_latency_ms advance on a skipped poll.
    state = load_state()
    assert state["last_poll_success"] == int(now())
    assert state["last_latency_ms"] == 33.3
    assert state["pending_latency_ms"] == 60.0


def test_timeout_on_a_skipped_tick_keeps_the_old_number_until_the_next_poll(
    state_db, config, transport, ping
):
    clock = {"t": FIXED_NOW}
    now = lambda: clock["t"]  # noqa: E731

    run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)  # displays 33ms
    ping.value = None

    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)

    # A timeout between polls is held like any other accepted move: the label
    # keeps showing the last measured number rather than spending a rename.
    assert report["did_poll"] is False
    assert report["latency_ms"] == 33.3
    assert report["pending_latency_ms"] is None
    assert "33ms" in dict(transport.patched)["cat"]
    assert load_state()["last_latency_ms"] == 33.3

    clock["t"] += config["poll_interval_seconds"]
    report = run_tick(config, now_fn=now, http_fn=transport, ping_fn=ping)
    assert report["latency_ms"] is None  # the timeout finally shows on the poll
    assert dict(transport.patched)["cat"].startswith("Speeds • timeout • ")


# -- default_ping (fake subprocess.run; never a real binary or network) ------


def _fake_ping_run(monkeypatch, stdout="", returncode=0, exc=None):
    """Patch core's subprocess.run; return the argv/kwargs it was called with."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout)

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    return seen


IPUTILS_LINE = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=33.338 ms\n"


def test_default_ping_parses_iputils_output(monkeypatch):
    seen = _fake_ping_run(monkeypatch, stdout=IPUTILS_LINE)

    assert core.default_ping() == pytest.approx(33.338)
    # One packet, ~2s wait, hardcoded host; argument list, no shell.
    assert seen["argv"] == ["ping", "-c", "1", "-W", "2", core.PING_HOST]
    assert seen["kwargs"]["timeout"] == 3


@pytest.mark.macos_only
def test_default_ping_darwin_drops_the_wait_flag(monkeypatch):
    seen = _fake_ping_run(monkeypatch, stdout=IPUTILS_LINE)

    assert core.default_ping() == pytest.approx(33.338)
    # macOS ping's -W is milliseconds, not seconds — "2" would wait 2ms and
    # time every probe out, so only the outer subprocess timeout waits.
    assert seen["argv"] == ["ping", "-c", "1", core.PING_HOST]
    assert seen["kwargs"]["timeout"] == 3


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ("time=33.3ms\n", 33.3),          # iputils, no space
        ("time<1ms\n", 0.5),              # ceiling → pinned inside [0, 1)
        ("time<1 ms\n", 0.5),             # Windows-style spacing
        ("Reply from 1.1.1.1: bytes=32 time=33ms TTL=57\n", 33.0),  # Windows
        ("Zeit=33ms\n", 33.0),            # German Windows label
        ("Zeit<1ms\n", 0.5),              # German Windows ceiling
        ("Antwort von 1.1.1.1: Bytes=32 Zeit=33ms TTL=57\n", 33.0),  # full line
    ],
)
def test_default_ping_accepts_the_known_output_shapes(monkeypatch, stdout, expected):
    _fake_ping_run(monkeypatch, stdout=stdout)
    assert core.default_ping() == pytest.approx(expected)


@pytest.mark.parametrize(
    "returncode, exc",
    [(1, None), (2, None), (0, FileNotFoundError("ping")), (0, OSError("boom"))],
)
def test_default_ping_never_raises(monkeypatch, returncode, exc):
    _fake_ping_run(monkeypatch, stdout=IPUTILS_LINE, returncode=returncode, exc=exc)
    assert core.default_ping() is None


def test_default_ping_with_unparseable_output_is_none(monkeypatch):
    _fake_ping_run(monkeypatch, stdout="PING 1.1.1.1 56(84) bytes of data.\n")
    assert core.default_ping() is None
