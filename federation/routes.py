"""HTTP routes for the federation peer surface.

- ``GET /api/peers`` — every discovered peer with its pairing status
  (paired / request-sent / request-pending / discovered).
- ``POST /api/peers/pair-request`` — incoming: a peer is asking to
  pair with us. We record it as pending and surface it to the
  operator; the request body and source IP are saved for the
  accept/decline UI.
- ``POST /api/peers/pair-accept-callback`` — incoming: peer confirms
  they accepted our outgoing pair request. We add them to our
  paired set if we have an outgoing record; otherwise drop the
  message (a peer can't unilaterally pair us).
- ``POST /api/peers/pair-request-out`` — operator action: send a
  pair request to a discovered peer (we POST to their
  /pair-request).
- ``POST /api/peers/pair-accept`` — operator action: accept an
  incoming pair request and POST a confirmation to the peer.
- ``POST /api/peers/pair-decline`` — operator action: drop an
  incoming pending request.
- ``POST /api/peers/unpair`` — operator action: remove a peer
  from our paired set (the other side is informed only by the
  next session-fetch failing, which is documented behavior).

Pair-request/accept-callback handlers don't require the config-lock
token because the request is itself a network operation that the
operator hasn't yet authenticated. The operator-action handlers
(pair-request-out, pair-accept, pair-decline, unpair) DO require it
because they mutate local state.

The ``register()`` entry point returns a ``Registration`` populated
with the routes above plus ``merge_peer_sessions`` as a session
post-processor — core calls it at the end of ``_session_summary``
to fan out to paired peers and append their rows.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING
from urllib.parse import ParseResult

import federation
from federation import store as fed_store
from lib.extensions import Registration

from .session_merge import merge_peer_sessions

if TYPE_CHECKING:
    from lib.server import Handler


# Per-peer fetch timeout when sending a pair-request to a remote
# host. Kept tight so a stalled peer doesn't hang the operator's
# Accept/Reject UI.
_PAIR_REQ_TIMEOUT_SEC = 3.0


def _peer_status(device_id: str) -> str:
    """Compute the pairing status of one discovered peer.

    Order matters: paired wins over request-sent, which wins over
    request-pending, which wins over plain discovered. (A peer
    that's already paired shouldn't show as "request pending"
    after a re-discovery.)
    """
    if fed_store.is_paired(device_id):
        return "paired"
    if fed_store.has_outgoing(device_id):
        return "request-sent"
    if fed_store.has_pending(device_id):
        return "request-pending"
    return "discovered"


def h_peers(handler: "Handler", _parsed: ParseResult) -> None:
    """List every known peer with status. Includes ``paired`` peers
    that haven't beaconed recently (so the UI can render them as
    "paired but offline" instead of dropping them entirely)."""
    paired = fed_store.list_paired()
    discovered = {p.device_id: p for p in federation.list_peers()}

    rows = []
    seen: set[str] = set()
    for did, p in discovered.items():
        rows.append({
            "device_id": did,
            "hostname": p.hostname,
            "dashboard_port": p.dashboard_port,
            "scheme": p.scheme,
            "version": p.version,
            "last_seen": p.last_seen,
            "url": p.base_url,
            "status": _peer_status(did),
            "online": True,
        })
        seen.add(did)
    # Paired peers we haven't seen lately. They still belong in the
    # list as "paired (offline)" so the operator can unpair them
    # without having to wait for a beacon.
    now = int(time.time())  # noqa: F841 — kept for parity / potential future use
    for did, entry in paired.items():
        if did in seen:
            continue
        rows.append({
            "device_id": did,
            "hostname": entry.get("hostname", ""),
            "dashboard_port": None,
            "scheme": None,
            "version": None,
            "last_seen": entry.get("paired_at", 0),
            "url": None,
            "status": "paired",
            "online": False,
        })
    # Surface incoming pending requests too — they may have arrived
    # before the corresponding beacon, or from a peer that doesn't
    # broadcast (someone behind a different router segment using
    # manual IP entry, future work).
    for req in fed_store.list_pending():
        if req.device_id in seen or req.device_id in paired:
            continue
        rows.append({
            "device_id": req.device_id,
            "hostname": req.hostname,
            "dashboard_port": None,
            "scheme": None,
            "version": None,
            "last_seen": req.created_at,
            "url": None,
            "status": "request-pending",
            "online": False,
        })

    rows.sort(key=lambda r: (r["status"] != "request-pending",
                              r["status"] != "paired",
                              r["hostname"]))
    handler._send_json({"ok": True, "peers": rows})


# ---------------------------------------------------------------------------
# Incoming: pair-request from a peer (no auth — they haven't paired yet).
# ---------------------------------------------------------------------------


def h_pair_request(handler: "Handler", _parsed: ParseResult, body: dict) -> None:
    """Peer is asking us to pair. Record as pending; the operator
    sees it in the Federation Config card and clicks Accept/Decline.

    No auth required (the operator hasn't trusted this peer yet,
    so they can't have a token for us). Same threat model as the
    LAN-broadcast beacon: anyone on the segment can knock; only
    the operator's accept makes anything happen.
    """
    did = (body.get("device_id") or "").strip()
    hostname = (body.get("hostname") or "").strip()[:64]
    if not did:
        handler._send_json({"ok": False, "error": "missing 'device_id'"}, status=400)
        return
    if fed_store.is_paired(did):
        # Already paired — no-op, return ok so the requester knows
        # not to retry.
        handler._send_json({"ok": True, "already_paired": True})
        return
    src = handler.client_address[0] if handler.client_address else ""
    fed_store.add_pending(did, hostname, src)
    handler._send_json({"ok": True, "pending": True})


# ---------------------------------------------------------------------------
# Incoming: pair-accept callback. The peer is telling us they've
# accepted our outgoing pair request, so we write the pair into
# our local store too.
# ---------------------------------------------------------------------------


def h_pair_accept_callback(handler: "Handler", _parsed: ParseResult, body: dict) -> None:
    """Peer confirms they accepted our request. We add them to our
    paired set if we have an outgoing record for them — otherwise
    drop the message (a peer can't unilaterally pair us).
    """
    did = (body.get("device_id") or "").strip()
    hostname = (body.get("hostname") or "").strip()[:64]
    if not did:
        handler._send_json({"ok": False, "error": "missing 'device_id'"}, status=400)
        return
    if not fed_store.has_outgoing(did):
        # We never asked them. Refuse — keeps a hostile peer from
        # writing themselves into our paired set by sending an
        # unsolicited "we accepted" message.
        handler._send_json({"ok": False, "error": "no outgoing request for this peer"},
                           status=409)
        return
    fed_store.add_paired(did, hostname)
    fed_store.clear_outgoing(did)
    handler._send_json({"ok": True, "paired": True})


# ---------------------------------------------------------------------------
# Operator actions: send a pair request, accept an incoming one,
# decline an incoming one, unpair an existing peer.
# ---------------------------------------------------------------------------


def _post_to_peer(base_url: str, path: str, payload: dict,
                  timeout: float = _PAIR_REQ_TIMEOUT_SEC) -> tuple[bool, dict | None]:
    """Best-effort POST helper; returns (ok, parsed_body)."""
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            return True, parsed
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, None


def h_pair_request_out(handler: "Handler", _parsed: ParseResult, body: dict) -> None:
    """Operator: send a pair request to a discovered peer."""
    if not handler._check_unlock():
        return
    did = (body.get("device_id") or "").strip()
    if not did:
        handler._send_json({"ok": False, "error": "missing 'device_id'"}, status=400)
        return
    # Look up the peer in the live registry.
    peer = next((p for p in federation.list_peers() if p.device_id == did), None)
    if peer is None:
        handler._send_json({"ok": False, "error": "peer not currently visible"},
                           status=404)
        return
    if fed_store.is_paired(did):
        handler._send_json({"ok": True, "already_paired": True})
        return
    payload = {
        "device_id": federation.get_or_create_device_id(),
        "hostname": federation.get_hostname(),
    }
    ok, _ = _post_to_peer(peer.base_url, "/api/peers/pair-request", payload)
    if not ok:
        handler._send_json({"ok": False, "error": "peer did not respond"},
                           status=502)
        return
    fed_store.mark_outgoing(did)
    handler._send_json({"ok": True, "request_sent": True})


def h_pair_accept(handler: "Handler", _parsed: ParseResult, body: dict) -> None:
    """Operator: accept an incoming pair request. We write the pair
    locally AND POST a confirmation to the peer so they write us
    too."""
    if not handler._check_unlock():
        return
    did = (body.get("device_id") or "").strip()
    if not did:
        handler._send_json({"ok": False, "error": "missing 'device_id'"}, status=400)
        return
    if not fed_store.has_pending(did):
        handler._send_json({"ok": False, "error": "no pending request for this peer"},
                           status=404)
        return
    # Find the peer's base_url from the live registry. If they've
    # gone offline since the request, accept locally but skip the
    # callback — the peer's beacon will eventually return and the
    # session merge will work in both directions once they re-add
    # us via their own incoming-request path.
    peer = next((p for p in federation.list_peers() if p.device_id == did), None)
    pending = next((r for r in fed_store.list_pending() if r.device_id == did), None)
    hostname = peer.hostname if peer else (pending.hostname if pending else "")
    fed_store.add_paired(did, hostname)
    fed_store.remove_pending(did)
    if peer is not None:
        payload = {
            "device_id": federation.get_or_create_device_id(),
            "hostname": federation.get_hostname(),
        }
        _post_to_peer(peer.base_url, "/api/peers/pair-accept-callback", payload)
    handler._send_json({"ok": True, "paired": True})


def h_pair_decline(handler: "Handler", _parsed: ParseResult, body: dict) -> None:
    """Operator: decline an incoming pair request."""
    if not handler._check_unlock():
        return
    did = (body.get("device_id") or "").strip()
    if not did:
        handler._send_json({"ok": False, "error": "missing 'device_id'"}, status=400)
        return
    removed = fed_store.remove_pending(did)
    handler._send_json({"ok": True, "removed": bool(removed)})


def h_unpair(handler: "Handler", _parsed: ParseResult, body: dict) -> None:
    """Operator: remove a peer from the paired set."""
    if not handler._check_unlock():
        return
    did = (body.get("device_id") or "").strip()
    if not did:
        handler._send_json({"ok": False, "error": "missing 'device_id'"}, status=400)
        return
    removed = fed_store.remove_paired(did)
    fed_store.clear_outgoing(did)
    handler._send_json({"ok": True, "removed": bool(removed)})


def register() -> Registration:
    """Entry point the core loader calls at server start."""
    reg = Registration(name="federation")
    reg.get_routes.update({
        "/api/peers": h_peers,
    })
    reg.post_routes.update({
        "/api/peers/pair-request":          h_pair_request,
        "/api/peers/pair-accept-callback":  h_pair_accept_callback,
        "/api/peers/pair-request-out":      h_pair_request_out,
        "/api/peers/pair-accept":           h_pair_accept,
        "/api/peers/pair-decline":          h_pair_decline,
        "/api/peers/unpair":                h_unpair,
    })
    # Session post-processor: core calls this at the end of
    # ``_session_summary`` (when ``merge_peers=True``) to fan out
    # to paired peers and append their rows to the local list.
    reg.session_post_processors.append(merge_peer_sessions)
    return reg
