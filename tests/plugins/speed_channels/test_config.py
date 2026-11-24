"""Config validation and loading contracts."""

from __future__ import annotations

import pytest

from plugins.speed_channels.core import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    SpeedChannelsError,
    check_minimum_config_from_mapping,
    load_speed_config,
    validate_speed_config,
)


def _section(**overrides):
    section = {
        "guild_id": "900000000000000001",
        "category_id": "cat",
        "channel_ids": {"power": "cp", "wan": "cw", "spend": "cd", "agents": "ca"},
    }
    section.update(overrides)
    return section


def test_valid_section_resolves_with_defaults(hermes):
    resolved = validate_speed_config(_section())

    assert resolved["guild_id"] == "900000000000000001"
    assert resolved["category_id"] == "cat"
    assert resolved["channel_ids"] == {"power": "cp", "wan": "cw", "spend": "cd", "agents": "ca"}
    assert resolved["poll_interval_seconds"] == DEFAULT_POLL_INTERVAL_SECONDS
    assert resolved["wan_interface"] == ""
    assert resolved["shelly_hosts"] == []
    assert resolved["state_db"] == str(hermes / "state.db")


def test_wan_interface_is_optional_and_passed_through(hermes):
    """Empty means "resolve the default route at tick time"."""
    assert validate_speed_config(_section())["wan_interface"] == ""
    assert validate_speed_config(_section(wan_interface=" enp1s0f0 "))[
        "wan_interface"
    ] == "enp1s0f0"


def test_poll_interval_is_respected(hermes):
    resolved = validate_speed_config(_section(poll_interval_seconds=60))

    assert resolved["poll_interval_seconds"] == 60


def test_shelly_hosts_are_stripped_and_blanks_dropped(hermes):
    resolved = validate_speed_config(_section(shelly_hosts=[" 10.0.0.11 ", "", "10.0.0.12"]))

    assert resolved["shelly_hosts"] == ["10.0.0.11", "10.0.0.12"]


def test_state_db_override_is_home_expanded(hermes):
    resolved = validate_speed_config(_section(state_db="~/elsewhere/state.db"))

    assert not resolved["state_db"].startswith("~")
    assert resolved["state_db"].endswith("elsewhere/state.db")


@pytest.mark.parametrize(
    "section, complaint",
    [
        (_section(guild_id=""), "guild_id"),
        (_section(category_id=None), "category_id"),
        (_section(channel_ids={"power": "cp", "wan": "cw", "spend": "cd"}), "agents"),
        (_section(channel_ids={"wan": "cw", "spend": "cd", "agents": "ca"}), "power"),
        (_section(channel_ids="nope"), "channel_ids"),
        (_section(shelly_hosts="10.0.0.11"), "shelly_hosts"),
        ("not a mapping", "mapping"),
    ],
)
def test_missing_pieces_are_named(hermes, section, complaint):
    with pytest.raises(SpeedChannelsError, match=complaint):
        validate_speed_config(section)


def test_load_from_hermes_home_config(hermes):
    resolved = load_speed_config()
    assert resolved["channel_ids"]["power"] == "cp"


def test_missing_section_is_an_error(hermes):
    (hermes / "config.yaml").write_text("model:\n  default: x\n", encoding="utf-8")
    with pytest.raises(SpeedChannelsError, match="speed_channels section"):
        load_speed_config()


def test_check_minimum_config_gates_dashboard_probes(hermes):
    assert check_minimum_config_from_mapping({"speed_channels": _section()}) is True
    assert check_minimum_config_from_mapping({"speed_channels": _section(guild_id="")}) is False
    assert check_minimum_config_from_mapping({}) is False
    assert check_minimum_config_from_mapping({"speed_channels": 3}) is False
