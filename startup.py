"""Federation extension lifecycle hooks.

The core loader calls ``register()`` at load time and receives a dict
of ``on_server_start`` / ``on_server_stop`` callables. The start hook
spins up the broadcaster + listener daemon threads; the stop hook
sets the shared event so they exit cleanly.

When the dashboard is launched with ``--no-federation``, core skips
loading this extension entirely — the start hook never fires, no
sockets are opened.
"""

from __future__ import annotations

import federation


# Holds the stop event between start and stop. The server process
# only ever has one pair of beacon threads, so a module-level handle
# is fine.
_stop_event = None


def register() -> dict:
    return {
        "on_server_start": [_start_federation],
        "on_server_stop": [_stop_federation],
    }


def _start_federation(httpd) -> None:
    global _stop_event
    bind = getattr(httpd, "server_address", ("", 8096))
    port = bind[1]
    scheme = "https" if getattr(httpd, "tls_paths", None) else "http"
    try:
        _stop_event = federation.start_federation(
            dashboard_port=port, scheme=scheme,
        )
        print(f"  federation: STARTED (UDP beacon on {federation.BEACON_PORT})")
    except Exception as e:
        # Federation is best-effort — never block startup on it.
        print(f"  federation: skipped ({e})")


def _stop_federation() -> None:
    global _stop_event
    if _stop_event is not None:
        _stop_event.set()
        _stop_event = None
