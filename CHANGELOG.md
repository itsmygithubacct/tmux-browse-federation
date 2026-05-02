# Changelog

## v0.7.6-federation — 2026-05-02

Initial release. Federation extracted from `tmux-browse` core into
its own repo as part of the extension-split program.

What moved out of core:

- `lib/federation/` — peer registry + UDP beacon broadcaster /
  listener (now `federation/` here).
- `lib/federation/store.py` — paired-peers persistent store
  (now `federation/store.py` here).
- `lib/server_routes/peers.py` — `/api/peers` and `/api/peers/pair-*`
  HTTP handlers (now `server/routes.py` here).
- `lib/server.py::_fetch_peer_sessions` and `_merge_peer_sessions`
  — peer fan-out and session merge (now `server/session_merge.py`
  here).
- `static/panes/federation.js` — Federation Config card UI
  (now `static/federation.js` here).
- `tests/test_federation.py` — peer registry, beacon, paired
  store, and aggregation tests.

What stayed in core (`tmux-browse >= 0.7.6`):

- `lib/host_identity.py` — `device_id` (UUID at
  `~/.tmux-browse/device-id`) and short hostname. Used to tag
  every session row with originating-host metadata even when this
  extension is not installed.
- The `--no-federation` CLI flag — when set, core skips loading
  this extension at startup (broadcaster + listener never start).

Operational changes for users on `tmux-browse <= 0.7.5.x`:

- After upgrading core to 0.7.6, federation is no longer enabled
  by default. Run `make install-federation && make enable-federation`
  to keep the previous behaviour.
- The `~/.tmux-browse/paired-peers.json` file is preserved and
  read by this extension at the same path.
