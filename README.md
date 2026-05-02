# tmux-browse-federation

Optional LAN federation extension for [tmux-browse](https://github.com/itsmygithubacct/tmux-browse).
Auto-discovers other tmux-browse hosts on the same broadcast domain
via a UDP beacon, and once both sides have explicitly accepted the
pairing, aggregates their session lists into one dashboard.

This lives in its own repo because most tmux-browse users run a
single host and don't need peer discovery — federation adds a UDP
listener, a network-callable pairing surface, and an outbound HTTP
fan-out, all of which are dead weight on a single-host install.
If you want a single dashboard tab to show panes from multiple
machines on your LAN, this is what you want.

## What's in here

- `federation/` — the importable package (`import federation`).
  Modules own the peer registry, the UDP beacon broadcaster +
  listener, and the persistent paired-peers store. Host identity
  (`device_id` + short hostname) lives in core's
  `lib.host_identity` so the dashboard can tag local rows with
  the same fields it tags remote rows even when this extension
  is not installed.
- `server/routes.py` — `/api/peers` + `/api/peers/pair-*` HTTP
  handlers, registered through the core extension loader.
- `server/session_merge.py` — fan-out to paired peers and merge
  their `/api/sessions` rows into the local response. Wired in
  via the `session_post_processors` hook.
- `static/federation.js` — the Federation Config card UI: peer
  list, status badges, Accept / Decline / Pair / Unpair buttons.
- `ui_blocks.html` — fills core's `<!--slot:config_post-->` with
  the Federation Config subsection wrapper.
- `startup.py` — extension lifecycle hooks. The
  `on_server_start` callback launches the broadcaster and listener
  daemon threads; the `on_server_stop` callback signals them to
  exit cleanly.
- `manifest.json` — what core reads to wire everything up
  (`min_tmux_browse: 0.7.6` because federation depends on the
  `session_post_processors` extension hook + the `host_identity`
  module that landed in 0.7.6).

## Install

```bash
# From a tmux-browse checkout:
make install-federation     # adds the submodule, fetches the pinned tag
make enable-federation      # writes federation=true to ~/.tmux-browse/extensions.json
python3 tmux_browse.py serve
```

The `--no-federation` CLI flag remains a runtime opt-out — even
when the extension is enabled, passing `--no-federation` skips
loading it for that run. Useful for one-off untrusted-network use.

## Trust model

LAN beacons are broadcast unencrypted. Anyone on the same network
segment can claim to be a peer; only your accept makes anything
happen. After accept:

- Both sides write the peer's `device_id` to
  `~/.tmux-browse/paired-peers.json` (mode 0600).
- Each side's dashboard fetches the other's `/api/sessions?local=1`
  on every refresh tick (5 s default). Failures are silent: a
  sluggish peer just doesn't contribute rows for that tick.
- Unpairing is one-sided: removing a peer here doesn't tell them.
  The next session-fetch from their side will simply succeed-and-
  show-nothing if they unpaired you, or continue showing your rows
  if they didn't.

For a hardened perimeter (multiple users, untrusted devices, public
network) put a reverse proxy in front of each dashboard. Federation
is designed for trusted LANs.

See [`docs/federation.md`](https://github.com/itsmygithubacct/tmux-browse/blob/main/docs/federation.md)
in the host repo for the full pairing flow, ports, and operational
notes.

## Running tests

```bash
# From the host tmux-browse checkout (with this submodule installed):
python3 -m unittest discover extensions/federation/tests
```

The tests use core's `lib.config.STATE_DIR` patch target, so the
core repo must be on `sys.path` (the bootstrap at the top of each
test file handles that automatically when run from the host repo).

## Versioning

This extension follows the host repo's release cadence. The
`pinned_ref` in core's `lib/extensions/catalog.py` is bumped
together with each tagged release here. The version-tag scheme is
`v<X.Y.Z>-federation` (e.g. `v0.7.6-federation`).
