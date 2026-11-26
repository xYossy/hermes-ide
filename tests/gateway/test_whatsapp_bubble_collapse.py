"""A platform without delete_message hands its progress bubble to the final answer.

WhatsApp cannot delete a message, so ``cleanup_progress`` is disabled for it in
``_run_agent_build_turn_context`` and the progress bubble outlives the turn. The runner
therefore registers that bubble with ``hermes_cli.progress_bridge``; the final-delivery
path resolves it, edits the answer into it and suppresses the second send.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.platforms.base import BasePlatformAdapter
from gateway.run_turn_runner import TurnRunner


class _ChatIdAdapter:
    """Edit-only adapter: has edit_message, inherits the base delete_message no-op."""

    name = "fake-wa"

    async def edit_message(self, *args, **kwargs):  # pragma: no cover - signature probe only
        raise NotImplementedError


class _DeletingAdapter(_ChatIdAdapter):
    name = "fake-dc"

    async def delete_message(self, chat_id, message_id):  # overrides the base no-op
        return True


def _runner(session_key="sess-1", chat_id="chat-1"):
    ctx = SimpleNamespace(session_key=session_key, source=SimpleNamespace(chat_id=chat_id))
    return SimpleNamespace(_ctx=ctx)


def _state(adapter, message_id):
    return SimpleNamespace(adapter=adapter, progress_msg_id=message_id)


@pytest.fixture(autouse=True)
def _clean_bridge():
    from hermes_cli.progress_bridge import clear_bubble
    clear_bubble(session_key="sess-1")
    yield
    clear_bubble(session_key="sess-1")


def test_edit_only_adapter_registers_its_bubble():
    from hermes_cli.progress_bridge import resolve_bubble

    TurnRunner._register_progress_bubble(_runner(), _state(_ChatIdAdapter(), "wamid.42"))

    assert resolve_bubble(session_key="sess-1") == ("chat-1", "wamid.42")


def test_deleting_adapter_registers_nothing():
    from hermes_cli.progress_bridge import resolve_bubble

    TurnRunner._register_progress_bubble(_runner(), _state(_DeletingAdapter(), "dc-7"))

    assert resolve_bubble(session_key="sess-1") is None


def test_adapter_with_only_the_base_delete_noop_still_registers():
    """The base delete_message is a no-op; inheriting it must not look like real deletion."""

    class _BaseNoopAdapter(_ChatIdAdapter):
        delete_message = BasePlatformAdapter.delete_message

    from hermes_cli.progress_bridge import resolve_bubble

    TurnRunner._register_progress_bubble(_runner(), _state(_BaseNoopAdapter(), "wamid.44"))

    assert resolve_bubble(session_key="sess-1") == ("chat-1", "wamid.44")


def test_no_message_id_registers_nothing():
    from hermes_cli.progress_bridge import resolve_bubble

    TurnRunner._register_progress_bubble(_runner(), _state(_ChatIdAdapter(), None))

    assert resolve_bubble(session_key="sess-1") is None


def test_registration_failure_never_raises(monkeypatch):
    import hermes_cli.progress_bridge as bridge

    def boom(**kwargs):
        raise RuntimeError("bridge unavailable")

    monkeypatch.setattr(bridge, "register_bubble", boom, raising=True)

    TurnRunner._register_progress_bubble(_runner(), _state(_ChatIdAdapter(), "wamid.43"))


# --- process-watcher status lines land in the bubble, not beside it -----------------

@pytest.fixture
def live_turn_queue():
    """A registered progress queue, as an in-flight turn would have."""
    from hermes_cli.progress_bridge import register_progress_queue, unregister_progress_queue

    queue = __import__("queue").Queue()
    register_progress_queue(queue=queue, session_key="sess-1")
    yield queue
    unregister_progress_queue(queue=queue, session_key="sess-1")


def _watcher_self(adapter):
    from gateway.run_notifications import GatewayNotificationsMixin

    source = SimpleNamespace(platform="whatsapp", chat_id="chat-1")
    fake = SimpleNamespace(
        _build_process_event_source=lambda watcher: source,
        _resolve_injection_adapter=lambda platform_name, src: adapter,
    )
    fake._send_watcher_message = lambda *a, **kw: GatewayNotificationsMixin._send_watcher_message(fake, *a, **kw)
    return fake


class _RecordingAdapter(_ChatIdAdapter):
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, **kwargs):
        self.sent.append(content)
        return SimpleNamespace(success=True, message_id="wamid.sent")


@pytest.mark.asyncio
async def test_watcher_status_goes_into_the_live_bubble(live_turn_queue):
    adapter = _RecordingAdapter()
    fake = _watcher_self(adapter)

    await fake._send_watcher_message("whatsapp", "chat-1", None, "Scan läuft\n65 GiB", {"session_key": "sess-1"})

    assert adapter.sent == [], "a live bubble must absorb the status line"
    assert live_turn_queue.get_nowait() == "\U0001F4DF Scan läuft 65 GiB"


@pytest.mark.asyncio
async def test_watcher_status_falls_back_to_a_normal_send_without_a_turn():
    adapter = _RecordingAdapter()
    fake = _watcher_self(adapter)

    await fake._send_watcher_message("whatsapp", "chat-1", None, "Scan läuft", {"session_key": "sess-1"})

    assert adapter.sent == ["Scan läuft"], "no turn queue means the message must still go out"
