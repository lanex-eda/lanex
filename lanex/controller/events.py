# Copyright 2026 LanEx Contributors
"""Process-wide event bus.

The runner, the installer, the simulator, and the tool manager all push events
here. Every SSE client drains from this single bus.

Design notes (why a ring buffer + a global sequence):

* **One monotonic sequence across the whole process** (:func:`next_seq`). The
  sequence never resets between runs, so a browser ``EventSource`` that
  reconnects (carrying ``Last-Event-ID``) correctly receives the *next* run's
  events instead of silently dropping them as "already seen".
* **Non-destructive reads** (:meth:`EventBus.events_since`). Multiple SSE clients
  (e.g. the main cockpit and the ``/ide`` pop-out, or two browser tabs) each keep
  their own cursor and all see every event — reading no longer consumes the
  queue, so clients can't starve each other.
* **Bounded** by count and by size, so a long run (or many parallel runs of a
  big design) can't grow memory without bound; very old events fall off the
  back once every client has long since read past them, and a client that
  resumes from before the oldest one gets a ``gap`` and resyncs.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

# --- process-global monotonic sequence -------------------------------------

_seq_lock = threading.Lock()
_seq = 0

# Which server process stamped an event id. The sequence restarts at 1 with
# every server start, so a browser that kept its window open across a restart
# reconnects with the OLD server's Last-Event-ID (say 5000): read as a plain
# number, the new server skipped every event until its own counter passed it,
# and the Flow page looked frozen (G14). Event ids on the wire are therefore
# "<boot>-<seq>"; an id from another boot, a bare number from an older server,
# or one ahead of this bus is a gap: the stream resyncs instead of skipping.
BOOT_ID = secrets.token_hex(4)
_WIRE_RX = re.compile(r"^([0-9a-f]{8})-(\d{1,18})$")


def wire_id(seq: int) -> str:
    """The SSE ``id:`` for bus sequence *seq*."""
    return f"{BOOT_ID}-{int(seq)}"


def resume_point(last_event_id: Optional[str], max_seq: int) -> Tuple[int, Optional[str]]:
    """Where a (re)connecting stream starts, from its ``Last-Event-ID``.

    Returns ``(cursor, gap_reason)``: ``cursor`` is the sequence to resume
    after (``-1`` = a fresh stream, start at the current end); ``gap_reason``
    is None when the cursor can be trusted, else why the client must resync
    (``"restart"``: the id came from another server process; ``"ahead"``: an
    id this bus never issued)."""
    raw = (last_event_id or "").strip()
    if not raw or raw == "-1":
        return -1, None
    m = _WIRE_RX.match(raw)
    if not m:
        return -1, "restart"
    if m.group(1) != BOOT_ID:
        return -1, "restart"
    seq = int(m.group(2))
    if seq > max_seq:
        return -1, "ahead"
    return seq, None


def next_seq() -> int:
    """Return the next process-global event sequence number (never resets)."""
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def _approx_size(value: Any, depth: int = 0) -> int:
    """A cheap estimate of an event's memory, in characters: strings count
    their length, containers their items (two levels deep), the rest a few
    bytes. Log batches carry lists of up to a few hundred lines, so this is
    what keeps one big design's logs from filling the ring by size."""
    if isinstance(value, str):
        return len(value)
    if depth >= 3:
        return 16
    if isinstance(value, dict):
        return 16 + sum(len(str(k)) + _approx_size(v, depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return 16 + sum(_approx_size(v, depth + 1) for v in value)
    return 16


class EventBus:
    """A bounded, broadcast, non-destructive event ring.

    Bounded twice: by count (``maxlen`` events) and by size (``max_chars``,
    an estimate of the text the events hold). The count alone let a big
    design's log batches hold hundreds of MB in a 20,000-event ring."""

    def __init__(self, maxlen: int = 20000, max_chars: int = 48_000_000) -> None:
        self._maxlen = max(1, int(maxlen))
        self._max_chars = max(1, int(max_chars))
        self._buf: Deque[Dict[str, Any]] = deque()
        self._sizes: Deque[int] = deque()
        self._chars = 0
        self._cond = threading.Condition(threading.RLock())

    def _trim(self) -> None:
        # Called with the lock held. Always keep the newest event.
        while len(self._buf) > 1 and (len(self._buf) > self._maxlen or self._chars > self._max_chars):
            self._buf.popleft()
            self._chars -= self._sizes.popleft()

    def emit(self, type_: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Append an event. If *payload* already carries an int ``seq`` (the
        runner mirrors its own events here with the seq it already stamped) we
        keep it so the two copies collapse; otherwise we assign a fresh one."""
        payload = payload or {}
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            seq = next_seq()
        evt = dict(payload)
        evt["type"] = type_
        evt["seq"] = seq
        evt.setdefault("ts", time.time())
        size = _approx_size(evt)
        with self._cond:
            self._buf.append(evt)
            self._sizes.append(size)
            self._chars += size
            self._trim()
            self._cond.notify_all()

    def events_since(self, cursor: int, *, block: bool = False, timeout: float = 0.4) -> List[Dict[str, Any]]:
        """Return every buffered event with ``seq > cursor`` (non-destructive).

        With ``block=True`` and nothing new, wait up to ``timeout`` for an emit
        before returning (so SSE can long-poll without a busy loop)."""
        with self._cond:
            new = [e for e in self._buf if e.get("seq", 0) > cursor]
            if new or not block:
                return new
            self._cond.wait(timeout)
            return [e for e in self._buf if e.get("seq", 0) > cursor]

    @property
    def max_seq(self) -> int:
        """The newest sequence in the ring. Emitters stamp a sequence before
        taking the ring's lock, so the ring is ordered only nearly: the newest
        is the largest of the last few entries, not always the last one."""
        with self._cond:
            if not self._buf:
                return 0
            n = len(self._buf)
            return max(self._buf[i].get("seq", 0) for i in range(max(0, n - 64), n))

    @property
    def min_seq(self) -> int:
        """Sequence of the OLDEST event still buffered (0 when empty).

        A reconnecting client that resumes from a cursor older than this has had
        the events in between evicted from the ring — its live view is now stale
        with no way to notice. The SSE layer uses this to emit an explicit
        ``gap`` so the client can re-hydrate from the authoritative status."""
        with self._cond:
            return self._buf[0].get("seq", 0) if self._buf else 0


# Singleton
bus = EventBus()


def publish(type_: str, payload: Optional[Dict[str, Any]] = None) -> None:
    bus.emit(type_, payload or {})
