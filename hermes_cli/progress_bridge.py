"""Local patch (Huhn): bridge the gateway's native turn-progress queue to plugins.

Why this exists
---------------
The native per-turn progress bubble (`gateway/run_turn_runner.py`) is the only progress
surface Hermes owns end to end: it edits one message in place, throttles its own edits
(``EDIT_INTERVAL``), splits overflow at the platform text limit, honours
``cleanup_progress`` and survives mid-run restarts. Plugins had no way to feed it:
there is no outbound-delivery hook, ``send_message`` is deliberately not registered as a
tool, and ``ctx.platform_actions`` v1 only knows ``add_reaction`` / ``set_thread_title``.

This module registers the running turn's queue so a plugin hook can append
pre-formatted lines to that same bubble via ``ctx.progress(text)``.

Scope
-----
Local-only patch, not upstream. Re-apply after ``hermes update`` (see
``~/.hermes/local-patches/apply-local-patches.sh``). Fail-open by design: with no queue
registered every call is a no-op returning ``False``, so an unpatched or freshly updated
tree keeps working.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes_cli.progress_bridge")

_LOCK = threading.Lock()
# session_id (agent hooks) / session_key (gateway routing) -> the turn's progress queue
_BY_SESSION: Dict[str, Any] = {}
# Fallback for callers that cannot name a session: the most recently started turn.
_LAST: Dict[str, Any] = {"queue": None, "at": 0.0}
# session_key -> (chat_id, bubble_message_id). Written by the progress lane whenever it
# creates/edits the bubble; read by the final-delivery path so an edit-only platform
# (WhatsApp) can put the ANSWER into the bubble instead of sending a second message.
_BUBBLE_BY_SESSION: Dict[str, Any] = {}


def register_bubble(*, session_key: Optional[str] = None, chat_id: Any = None,
                    message_id: Any = None) -> None:
    """Remember the live progress bubble of a turn (chat_id + message_id).

    Fail-open: a call without both keys is a no-op, so an unpatched caller changes nothing.
    """
    if not session_key or chat_id in (None, "") or message_id in (None, ""):
        return
    with _LOCK:
        _BUBBLE_BY_SESSION[str(session_key)] = (str(chat_id), str(message_id))


def resolve_bubble(*, session_key: Optional[str] = None) -> Any:
    """Return ``(chat_id, message_id)`` of the current turn's progress bubble, or ``None``."""
    with _LOCK:
        if session_key and str(session_key) in _BUBBLE_BY_SESSION:
            return _BUBBLE_BY_SESSION[str(session_key)]
        return None


def clear_bubble(*, session_key: Optional[str] = None) -> None:
    """Drop the bubble entry when the turn ends (idempotent, never raises)."""
    with _LOCK:
        if session_key:
            _BUBBLE_BY_SESSION.pop(str(session_key), None)


def register_progress_queue(*, queue: Any, session_key: Optional[str] = None,
                            session_id: Optional[str] = None) -> None:
    """Register the live progress queue of a running turn (called by the gateway)."""
    if queue is None:
        return
    with _LOCK:
        for key in (session_key, session_id):
            if key:
                _BY_SESSION[str(key)] = queue
        _LAST["queue"] = queue
        _LAST["at"] = time.time()


def unregister_progress_queue(*, queue: Any = None, session_key: Optional[str] = None,
                              session_id: Optional[str] = None) -> None:
    """Drop the registry entry when the turn ends (idempotent, never raises)."""
    with _LOCK:
        for key in (session_key, session_id):
            if key:
                _BY_SESSION.pop(str(key), None)
        if queue is not None:
            for key in [k for k, v in _BY_SESSION.items() if v is queue]:
                _BY_SESSION.pop(key, None)
            if _LAST["queue"] is queue:
                _LAST["queue"] = None


def resolve_queue(*, session_key: Optional[str] = None, session_id: Optional[str] = None) -> Any:
    with _LOCK:
        for key in (session_id, session_key):
            if key and str(key) in _BY_SESSION:
                return _BY_SESSION[str(key)]
        return _LAST["queue"] if _LAST["queue"] is not None else None


def has_queue(*, session_key: Optional[str] = None,
              session_id: Optional[str] = None) -> bool:
    """True when THIS key owns a live queue — no ``_LAST`` fallback.

    ``resolve_queue`` answers "where would a line go", which is right for a caller that only has a
    session id. A caller that knows the exact chat (a process watcher) must ask "is this chat
    running a turn at all"; otherwise it hands the line to whatever turn started last.
    """
    with _LOCK:
        return any(key and str(key) in _BY_SESSION for key in (session_id, session_key))


def push_progress(text: Any, *, session_key: Optional[str] = None,
                  session_id: Optional[str] = None) -> bool:
    """Append one item (plain line or native event dict) to the turn's progress bubble.

    Returns True when the item was queued. Never raises: a plugin must not be able to
    break a turn through this bridge.
    """
    try:
        queue = resolve_queue(session_key=session_key, session_id=session_id)
        if queue is None:
            return False
        queue.put_nowait(text)
        return True
    except Exception:  # pragma: no cover - defensive, bridge must fail open
        logger.debug("progress bridge push failed", exc_info=True)
        return False


def active_sessions() -> Dict[str, Any]:
    """Diagnostics only: which sessions currently have a live progress queue."""
    with _LOCK:
        return dict(_BY_SESSION)
