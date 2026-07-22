"""Federation session aggregation — fan out to paired peers and
merge their session lists into the local one.

Registered as a session post-processor on the core extension hook.
Core calls ``merge_peer_sessions(out)`` at the end of
``_session_summary`` when ``merge_peers=True``; the function
mutates ``out`` in place by appending peer rows.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import federation
from federation import store as fed_store


# Per-peer fetch timeout. Loose enough for resource-constrained peers
# (e.g. a 6-core ARM SBC capturing ~10 pane snapshots) to finish writing
# their /api/sessions response. Slow / dead peers serve empty under
# their hostname rather than stalling the dashboard.
_PEER_FETCH_TIMEOUT_SEC = 5.0

# Total wall budget for the parallel-fetch step. Bounded by N peers x
# timeout in the worst case, but we ``join(timeout=...)`` each thread
# with this cap to keep the total request fast even when peers misbehave.
_PEER_AGGREGATE_BUDGET_SEC = 6.0


def _fetch_peer_sessions(base_url: str, timeout: float = _PEER_FETCH_TIMEOUT_SEC) -> list[dict]:
    """Best-effort GET <peer>/api/sessions; returns the rows or [].

    Sends ``?local=1`` so the peer skips its own federation merge.
    Without this the polling graph cascades: peer A -> peer B -> A -> ...
    """
    url = f"{base_url}/api/sessions?local=1"
    req = urllib.request.Request(url)
    token = federation.get_dashboard_auth_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rows = data.get("sessions") or []
            return rows if isinstance(rows, list) else []
    except (urllib.error.URLError, ValueError, UnicodeDecodeError, TimeoutError, OSError):
        return []


def merge_peer_sessions(out: list[dict]) -> None:
    """Walk *paired* LAN peers, fetch their session lists in parallel,
    prefix names with the peer's hostname, and append to ``out``.

    Pair status is consulted before any HTTP fetch — a discovered
    peer that the operator hasn't accepted contributes nothing.
    Peers that don't respond inside the budget contribute nothing
    for this tick.
    """
    peers = [p for p in federation.list_peers() if fed_store.is_paired(p.device_id)]
    if not peers:
        return
    results: dict[str, list[dict]] = {}
    threads: list[threading.Thread] = []
    for peer in peers:
        def _fetch(p=peer):
            results[p.device_id] = _fetch_peer_sessions(p.base_url)
        t = threading.Thread(target=_fetch, daemon=True,
                             name=f"federation-fetch-{peer.device_id[:8]}")
        t.start()
        threads.append(t)
    deadline = time.monotonic() + _PEER_AGGREGATE_BUDGET_SEC
    for t in threads:
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)
    for peer in peers:
        for row in results.get(peer.device_id, []):
            # Skip remote rows that are themselves remote (a peer
            # showing us another peer's sessions). Only direct-host
            # sessions get federated; otherwise the same row would
            # appear under multiple hostname prefixes as the graph
            # walks itself.
            if not isinstance(row, dict) or row.get("peer_url"):
                continue
            remote = dict(row)
            remote_name = str(remote.get("name", ""))
            # ``name`` is the dashboard's identity key, not merely a label.
            # Hostnames are neither unique nor authenticated, so include the
            # full device id and encode both components unambiguously. Keep the
            # friendly hostname form separately for rendering.
            remote["name"] = (
                f"peer:{quote(peer.device_id, safe='')}:"
                f"{quote(remote_name, safe='')}"
            )
            remote["display_name"] = f"{peer.hostname}:{remote_name}"
            remote["peer_session_name"] = remote_name
            remote["device_id"] = peer.device_id
            remote["peer_url"] = peer.base_url
            remote["peer_hostname"] = peer.hostname
            out.append(remote)
