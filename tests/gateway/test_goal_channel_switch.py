"""Per-chat goal kill switch via goals.disabled_channels."""

import asyncio
from unittest.mock import patch

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource


class _Dummy:
    pass


def _make_runner(tmp_path, monkeypatch):
    from gateway.run_goals import GatewayGoalsMixin
    from gateway.run_goal_intent import GatewayGoalIntentMixin
    from gateway.slash_commands_goals import GatewayGoalCommandsMixin

    class Runner(GatewayGoalsMixin, GatewayGoalIntentMixin, GatewayGoalCommandsMixin):
        pass

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    inst = object.__new__(Runner)
    inst._resolve_profile_home_for_source = lambda source: tmp_path
    inst._clear_goal_continuations = lambda event, verb: None
    inst._adapter_and_key_for = lambda event: (None, None)

    async def _exec(fn, *a, **kw):
        return fn(*a, **kw)

    inst._run_in_executor_with_context = _exec

    async def _gm(event):
        return None, None

    inst._get_goal_manager_for_event = _gm
    return inst


def _source(chat_id="c1", parent=None):
    return SessionSource(platform=Platform.DISCORD, chat_id=chat_id, user_id="u1",
                        user_name="Alice", parent_chat_id=parent)


@pytest.mark.asyncio
async def test_off_writes_chat_id_and_keeps_unrelated_keys(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("foo: bar\ngoals:\n  max_turns: 7\n", encoding="utf-8")
    runner = _make_runner(tmp_path, monkeypatch)
    source = _source("c1")
    assert runner._set_goals_disabled_for_source(source, True) is True
    import yaml
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert data["foo"] == "bar"
    assert data["goals"]["max_turns"] == 7
    assert data["goals"]["disabled_channels"] == ["c1"]


@pytest.mark.asyncio
async def test_on_removes_and_drops_empty_key(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("goals:\n  disabled_channels:\n  - c1\n", encoding="utf-8")
    runner = _make_runner(tmp_path, monkeypatch)
    source = _source("c1")
    assert runner._set_goals_disabled_for_source(source, False) is True
    import yaml
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "disabled_channels" not in (data.get("goals") or {})


@pytest.mark.asyncio
async def test_thread_inherits_parent_disabled(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("goals:\n  disabled_channels:\n  - parent1\n", encoding="utf-8")
    runner = _make_runner(tmp_path, monkeypatch)
    source = _source("thread1", parent="parent1")
    assert runner._goals_disabled_for_source(source) is True


@pytest.mark.asyncio
async def test_auto_goals_enabled_false_when_disabled(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("goals:\n  disabled_channels:\n  - c1\n", encoding="utf-8")
    runner = _make_runner(tmp_path, monkeypatch)
    source = _source("c1")
    assert runner._auto_goals_enabled(source) is False


@pytest.mark.asyncio
async def test_on_off_never_reach_dispatch(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    runner = _make_runner(tmp_path, monkeypatch)
    with patch("hermes_cli.goal_command.dispatch_goal_command") as dispatch:
        for arg in ("on", "off"):
            event = MessageEvent(text=f"/goal {arg}", message_type=MessageType.TEXT,
                                 source=_source("c1"))
            event.get_command_args = lambda a=arg: a
            await runner._handle_goal_command(event)
        dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_other_arg_in_disabled_chat_returns_notice(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("goals:\n  disabled_channels:\n  - c1\n", encoding="utf-8")
    runner = _make_runner(tmp_path, monkeypatch)
    with patch("hermes_cli.goal_command.dispatch_goal_command") as dispatch:
        event = MessageEvent(text="/goal foo", message_type=MessageType.TEXT, source=_source("c1"))
        event.get_command_args = lambda: "foo"
        result = await runner._handle_goal_command(event)
        assert result == "Goals are disabled in this channel. /goal on re-enables."
        dispatch.assert_not_called()
