"""LAN federation — auto-discover peer tmux-browse instances.

Each instance broadcasts a UDP beacon on a fixed port (8095) every
five seconds and listens for the same packets from other peers on
the same broadcast domain. Discovered peers are merged into the
local dashboard's session list, with each remote session's name
prefixed by the originating peer's hostname.

Stdlib-only: ``socket`` for UDP, ``threading`` for the broadcaster
and listener loops. No mDNS, no zeroconf, no pip dependency.

The trust model is "any host on the same LAN can claim to be a
peer." That's appropriate for a single-user / single-tenant LAN
but not for shared networks. Disable by uninstalling this extension
or by passing ``--no-federation`` to the dashboard.

This module exposes only the registry primitives + the start-up
hook. The HTTP route surface (``/api/peers``) lives in
``server.routes``; the session aggregation pass lives in
``server.session_merge``. Host identity (``device_id``,
``hostname``) lives in core's ``lib.host_identity`` so the dashboard
can tag local rows even when this extension is not installed.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass

from lib import __version__
from lib.host_identity import get_hostname, get_or_create_device_id  # noqa: F401

_log = logging.getLogger(__name__)

# Fixed port for both the broadcaster and the listener. UDP: a
# different process on the host can bind the same port at the same
# time, so a second tmux-browse on this machine can still beacon
# (it just won't *receive* — see the listener for how that's
# handled). 8095 is one below the dashboard default port (8096) on
# purpose: stays out of the way of HTTP traffic and is easy to
# remember.
BEACON_PORT = 8095

# Beacon cadence and TTL. Five seconds is fine for "did this peer
# just come up?" — phones flipping WiFi see it within ~10s; a
# fresh peer is visible to others within one beacon. The TTL is
# 3× the cadence so a single dropped packet doesn't drop the peer.
BEACON_INTERVAL_SEC = 5
PEER_TTL_SEC = 15


@dataclass
class PeerInfo:
    device_id: str
    hostname: str
    dashboard_port: int
    scheme: str  # "http" | "https"
    version: str
    last_seen: int
    addr: str  # IP address that last sent a beacon

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.addr}:{self.dashboard_port}"


# Peer registry. The listener thread populates ``_peers``; the
# session-aggregation path reads it. ``_peers_lock`` makes both
# safe under ``ThreadingHTTPServer``'s thread-per-request model.
_peers: dict[str, PeerInfo] = {}
_peers_lock = threading.Lock()


def list_peers(now: int | None = None) -> list[PeerInfo]:
    """Live peers — everything still inside the TTL window."""
    n = now if now is not None else int(time.time())
    with _peers_lock:
        return [p for p in _peers.values() if (n - p.last_seen) < PEER_TTL_SEC]


def gc_peers(now: int | None = None) -> int:
    """Drop peers that haven't beaconed inside the TTL.

    Returns the number of entries dropped — handy for tests."""
    n = now if now is not None else int(time.time())
    with _peers_lock:
        stale = [d for d, p in _peers.items() if (n - p.last_seen) >= PEER_TTL_SEC]
        for d in stale:
            _peers.pop(d, None)
        return len(stale)


def upsert_peer(info: PeerInfo) -> None:
    """Internal — used by the listener thread to record a beacon.

    Public-ish so tests can poke entries in directly."""
    with _peers_lock:
        _peers[info.device_id] = info


def clear_peers() -> None:
    """For tests."""
    with _peers_lock:
        _peers.clear()


# ---------------------------------------------------------------------------
# UDP beacon: broadcaster + listener
# ---------------------------------------------------------------------------
#
# Two daemon threads run for the lifetime of the dashboard server:
#
#   broadcaster: every BEACON_INTERVAL_SEC, send a UDP packet to the
#       LAN broadcast address (255.255.255.255). Payload is a JSON
#       blob naming this host. Other peers' listeners see it.
#
#   listener: bind a UDP socket on BEACON_PORT and recvfrom in a
#       loop. Each packet is parsed, our own packets ignored, and
#       valid peers upserted into the registry with the source IP
#       attached. Stale peers age out via gc_peers (called from the
#       request handler, not on a timer — keeps the listener thread
#       focused on a single concern).
#
# The listener tries to bind BEACON_PORT once. If another tmux-browse
# instance on this host already bound it, listen-bind fails and we
# log + continue without a listener. The broadcaster still works,
# so this peer is *visible* to others, just not the reverse. That's
# acceptable for the "two boxes on one LAN" case; the rare two-on-
# one-host case is degraded but not broken.


def _beacon_payload(my: PeerInfo, seq: int) -> bytes:
    """Build the JSON beacon body. Kept tiny so the packet fits in
    a single datagram comfortably (well under typical 1500 byte
    MTU)."""
    return json.dumps({
        "device_id": my.device_id,
        "hostname": my.hostname,
        "dashboard_port": my.dashboard_port,
        "scheme": my.scheme,
        "version": my.version,
        "beacon_seq": seq,
    }).encode("utf-8")


def _broadcaster(my: PeerInfo, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        seq = 0
        while not stop.is_set():
            seq += 1
            payload = _beacon_payload(my, seq)
            try:
                sock.sendto(payload, ("255.255.255.255", BEACON_PORT))
            except OSError as e:
                # Network down or no broadcast route. Log once and
                # keep trying — networks come back, and we don't
                # want to spam the log every 5 seconds.
                if seq == 1 or seq % 60 == 0:
                    _log.debug("federation: beacon send failed: %s", e)
            stop.wait(BEACON_INTERVAL_SEC)
    finally:
        sock.close()


def _listener(my_device_id: str, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT lets two tmux-browse instances on the same
        # host both receive beacons (Linux >=3.9). Best-effort: not
        # all platforms expose it.
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", BEACON_PORT))
        except OSError as e:
            # Port already bound and SO_REUSEPORT unavailable. Log
            # and exit the thread; we still beacon, just don't
            # receive. See module-level docstring.
            _log.warning("federation: listener could not bind UDP %d (%s); "
                         "incoming beacons disabled on this peer", BEACON_PORT, e)
            return
        sock.settimeout(1.0)
        while not stop.is_set():
            try:
                data, (addr, _port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
                did = str(msg["device_id"])
            except (ValueError, UnicodeDecodeError, KeyError):
                # Some other UDP service shouting on the same port,
                # or a corrupt frame. Drop quietly.
                continue
            if did == my_device_id:
                # Our own broadcast bouncing back via the loopback /
                # broadcast reflection — don't add ourselves to the
                # peer list.
                continue
            try:
                upsert_peer(PeerInfo(
                    device_id=did,
                    hostname=str(msg.get("hostname", "unknown"))[:64],
                    dashboard_port=int(msg.get("dashboard_port", 8096)),
                    scheme=str(msg.get("scheme", "http")),
                    version=str(msg.get("version", "?"))[:32],
                    last_seen=int(time.time()),
                    addr=addr,
                ))
            except (TypeError, ValueError):
                continue
    finally:
        sock.close()


def start_federation(dashboard_port: int, scheme: str = "http") -> threading.Event:
    """Start the broadcaster + listener daemon threads.

    Returns a ``threading.Event`` the caller can ``set()`` to stop
    both threads. The threads themselves are daemons so they don't
    block process exit.
    """
    my = PeerInfo(
        device_id=get_or_create_device_id(),
        hostname=get_hostname(),
        dashboard_port=dashboard_port,
        scheme=scheme,
        version=__version__,
        last_seen=0,
        addr="",
    )
    stop = threading.Event()
    threading.Thread(target=_broadcaster, args=(my, stop),
                     daemon=True, name="federation-beacon").start()
    threading.Thread(target=_listener, args=(my.device_id, stop),
                     daemon=True, name="federation-listen").start()
    _log.info("federation: started (port %d)", BEACON_PORT)
    return stop
