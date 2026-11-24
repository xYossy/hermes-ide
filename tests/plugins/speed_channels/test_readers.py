"""Vitals reader contracts against fake transports and a real-schema temp
state.db. Each reader returns its channel name and never raises: a dead plug,
an unreadable database or a failed speed sample degrades only its own channel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    FIXED_NOW,
    SHELLY_A,
    SHELLY_B,
    SHELLY_DEAD,
    TODAY,
    add_delegation,
    add_usage,
)
from plugins.speed_channels.core import (
    WAN_WINDOW_SECONDS,
    agents_name,
    default_iface,
    power_name,
    shelly_watts,
    spend_name,
    wan_name,
    wan_rates,
)


def _fixed(_now=FIXED_NOW):
    return lambda: _now


# -- power -------------------------------------------------------------------


def test_shelly_sums_the_plugs_that_answered_and_skips_the_dead_one(transport):
    watts, online = shelly_watts([SHELLY_A, SHELLY_B, SHELLY_DEAD], transport)

    assert watts == pytest.approx(63.0)
    assert online == 2


def test_power_name_states(transport):
    assert power_name([], transport) == "Power: no plugs configured"
    assert power_name([SHELLY_DEAD], transport) == "Power: unreachable"
    assert (
        power_name([SHELLY_A, SHELLY_B, SHELLY_DEAD], transport)
        == "Power: 63W · 2/3 online"
    )


# -- WAN ---------------------------------------------------------------------

def _counters(*samples):
    """A counters_fn that returns the given (rx, tx) byte pairs in order."""
    queue = list(samples)

    def read(iface):
        assert iface == "enp1s0f0"
        return queue.pop(0)

    return read


def test_wan_rates_derives_mbps_from_the_counter_delta():
    # 1.5 MB down and 375 kB up across the 3 s window.
    counters = _counters((0, 0), (1_500_000, 375_000))

    down, up = wan_rates(
        "enp1s0f0",
        window=WAN_WINDOW_SECONDS,
        counters_fn=counters,
        sleep_fn=lambda _s: None,
    )

    assert down == pytest.approx(4.0)
    assert up == pytest.approx(1.0)


def test_wan_rates_blocks_for_the_whole_window():
    slept = []

    wan_rates(
        "enp1s0f0",
        counters_fn=_counters((0, 0), (0, 0)),
        sleep_fn=slept.append,
    )

    assert slept == [WAN_WINDOW_SECONDS]


def test_wan_name_reports_the_measured_rates():
    assert wan_name("eth0", rates_fn=lambda _i: (24.4, 3.1)) == (
        "WAN: ↓ 24.4 ↑ 3.1 Mbps"
    )


def test_wan_name_calls_a_quiet_line_idle():
    assert wan_name("eth0", rates_fn=lambda _i: (0.0, 0.004)) == "WAN: idle"


def test_wan_name_without_an_interface_says_so():
    assert wan_name("") == "WAN: no uplink found"


def test_wan_name_survives_an_unreadable_interface():
    def boom(_iface):
        raise OSError("no such device")

    assert wan_name("eth9", rates_fn=boom) == "WAN: unreachable"


def test_default_iface_resolves_to_a_real_interface():
    if not Path("/proc/net/route").exists():
        pytest.skip("no /proc/net/route on this platform")

    iface = default_iface()

    assert iface, "default route has no device"
    assert Path(f"/sys/class/net/{iface}").is_dir()


# -- spend -------------------------------------------------------------------


def test_spend_sums_todays_tokens_and_calls(state_db):
    add_usage(state_db, session="s1", last_seen=TODAY + 100,
              calls=10, input_tokens=9_500_000, output_tokens=225_717)
    add_usage(state_db, session="s2", last_seen=TODAY + 200, calls=1685)

    assert spend_name(str(state_db), now_fn=_fixed()) == "Spend: 9.7M tok · 1695 calls"


def test_spend_excludes_yesterday(state_db):
    add_usage(state_db, session="old", last_seen=TODAY - 100,
              calls=500, input_tokens=1_000_000)

    assert spend_name(str(state_db), now_fn=_fixed()) == "Spend: 0 tok · 0 calls"


def test_spend_on_a_missing_database_degrades_its_own_channel(tmp_path):
    missing = tmp_path / "nope.db"

    assert spend_name(str(missing), now_fn=_fixed()) == "Spend: db unreadable"
    assert not missing.exists()  # read-only open must never create the file


def test_spend_on_a_database_without_the_table_degrades(tmp_path):
    import sqlite3

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    assert spend_name(str(empty), now_fn=_fixed()) == "Spend: db unreadable"


# -- agents ------------------------------------------------------------------


def test_agents_counts_running_and_todays_completed(state_db):
    add_delegation(state_db, delegation_id="d1", state="running")
    add_delegation(state_db, delegation_id="d2", state="finalizing")
    add_delegation(state_db, delegation_id="d3", state="completed", completed_at=TODAY + 10)
    add_delegation(state_db, delegation_id="d4", state="completed", completed_at=TODAY + 20)
    add_delegation(state_db, delegation_id="d5", state="completed", completed_at=TODAY - 10)
    add_delegation(state_db, delegation_id="d6", state="failed", completed_at=TODAY + 30)

    assert agents_name(str(state_db), now_fn=_fixed()) == "Agents: 2 running · 2 done"


def test_agents_on_a_missing_database_degrades_its_own_channel(tmp_path):
    assert agents_name(str(tmp_path / "nope.db"), now_fn=_fixed()) == "Agents: db unreadable"
