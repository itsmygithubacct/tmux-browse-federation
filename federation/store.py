"""Persistent paired-peers store.

Pairing replaces the original "trust everyone on the LAN" model
that shipped with 0.7.3.0. Discovery still happens via UDP
beacons, but a peer's sessions are only aggregated into the
local dashboard after both sides have explicitly accepted the
pairing.

Two records:

- **paired** (persistent, ``~/.tmux-browse/paired-peers.json``):
  peers we've previously accepted. Survives restart.
- **pending requests** (in-memory only): incoming pair
  requests awaiting the operator's accept/decline. Cleared on
  restart by design — pairing is consensual; if you missed the
  request, the other side can re-send.

The file format is small + flat:

```json
{
  "<peer device_id>": {
    "hostname": "alpha",
    "paired_at": 1777400000
  },
  ...
}
```
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from lib import config


def _paired_path() -> Path:
    return config.STATE_DIR / "paired-peers.json"


# ---------------------------------------------------------------------------
# Persistent paired set
# ---------------------------------------------------------------------------

_paired_lock = threading.Lock()


def _read_paired() -> dict[str, dict]:
    p = _paired_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for did, entry in raw.items():
        if isinstance(did, str) and isinstance(entry, dict):
            out[did] = {
                "hostname": str(entry.get("hostname", ""))[:64],
                "paired_at": int(entry.get("paired_at", 0) or 0),
            }
    return out


def _write_paired(data: dict[str, dict]) -> None:
    p = _paired_path()
    config.ensure_dirs()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(p)


def list_paired() -> dict[str, dict]:
    with _paired_lock:
        return _read_paired()


def is_paired(device_id: str) -> bool:
    if not device_id:
        return False
    with _paired_lock:
        return device_id in _read_paired()


def add_paired(device_id: str, hostname: str,
               now: int | None = None) -> None:
    """Record an accepted pairing. Idempotent: re-adding refreshes
    the hostname but keeps the original paired_at."""
    if not device_id:
        return
    n = now if now is not None else int(time.time())
    with _paired_lock:
        data = _read_paired()
        existing = data.get(device_id, {})
        data[device_id] = {
            "hostname": hostname or existing.get("hostname", ""),
            "paired_at": existing.get("paired_at") or n,
        }
        _write_paired(data)


def remove_paired(device_id: str) -> bool:
    """Drop a pairing. Returns True if anything was removed."""
    if not device_id:
        return False
    with _paired_lock:
        data = _read_paired()
        if device_id not in data:
            return False
        data.pop(device_id, None)
        _write_paired(data)
        return True


# ---------------------------------------------------------------------------
# In-memory pending requests
# ---------------------------------------------------------------------------
#
# When peer A sends a pair request to peer B, B records it here.
# B's dashboard polls the federation status and surfaces "alpha
# wants to pair" with Accept / Decline buttons. On accept B writes
# the pairing both into the persistent store AND POSTs back to A
# so A also writes it.
#
# Pending requests time out after 1 hour to keep memory bounded
# under spam; the operator can always re-request.

_PENDING_TTL_SEC = 3600


@dataclass
class PendingRequest:
    device_id: str
    hostname: str
    addr: str  # source IP from the HTTP request
    created_at: int


_pending: dict[str, PendingRequest] = {}
_pending_lock = threading.Lock()


def add_pending(device_id: str, hostname: str, addr: str,
                now: int | None = None) -> None:
    n = now if now is not None else int(time.time())
    with _pending_lock:
        _pending[device_id] = PendingRequest(
            device_id=device_id,
            hostname=hostname or "unknown",
            addr=addr or "",
            created_at=n,
        )


def list_pending(now: int | None = None) -> list[PendingRequest]:
    """Return live pending requests (younger than the TTL)."""
    n = now if now is not None else int(time.time())
    with _pending_lock:
        # GC inline so the list is always fresh.
        for did in [d for d, r in _pending.items() if (n - r.created_at) >= _PENDING_TTL_SEC]:
            _pending.pop(did, None)
        return list(_pending.values())


def remove_pending(device_id: str) -> bool:
    if not device_id:
        return False
    with _pending_lock:
        return _pending.pop(device_id, None) is not None


def has_pending(device_id: str) -> bool:
    if not device_id:
        return False
    with _pending_lock:
        return device_id in _pending


def clear_all() -> None:
    """For tests."""
    with _paired_lock:
        if _paired_path().exists():
            _paired_path().unlink()
    with _pending_lock:
        _pending.clear()


# ---------------------------------------------------------------------------
# Outgoing-request tracking (so the UI can show "request sent, waiting")
# ---------------------------------------------------------------------------
#
# When we POST a pair-request to a peer, we remember that we did so
# until either (a) the peer's beacon arrives with us in their paired
# list (the pair-accept callback writes our local store), or (b) the
# request ages out. Lighter than the incoming-request store —
# device_ids only.

_outgoing_lock = threading.Lock()
_outgoing: dict[str, int] = {}  # device_id -> ts when we sent the request


def mark_outgoing(device_id: str, now: int | None = None) -> None:
    if not device_id:
        return
    n = now if now is not None else int(time.time())
    with _outgoing_lock:
        _outgoing[device_id] = n


def has_outgoing(device_id: str, now: int | None = None) -> bool:
    if not device_id:
        return False
    n = now if now is not None else int(time.time())
    with _outgoing_lock:
        ts = _outgoing.get(device_id)
        if ts is None:
            return False
        if (n - ts) >= _PENDING_TTL_SEC:
            _outgoing.pop(device_id, None)
            return False
        return True


def clear_outgoing(device_id: str) -> None:
    if not device_id:
        return
    with _outgoing_lock:
        _outgoing.pop(device_id, None)
