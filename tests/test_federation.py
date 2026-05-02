"""Federation peer registry + beacon serialization tests.

The UDP listener/broadcaster threads aren't unit-tested here —
network state isn't reproducible in CI. Instead we cover the
pure-Python pieces: device-id persistence, peer registry GC,
beacon JSON shape, and the urllib-based peer fetcher's error
paths."""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

# Layout: <core_repo>/extensions/federation/tests/test_federation.py
# parents[3] is the core repo (so ``lib.*`` resolves);
# parents[1] is the extension root (so ``federation`` and its submodules resolve).
_REPO = Path(__file__).resolve().parents[3]
_EXT = Path(__file__).resolve().parents[1]
for _p in (_REPO, _EXT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import federation  # noqa: E402
from lib import config as lib_config  # noqa: E402


class DeviceIdTests(unittest.TestCase):
    """Per-host UUID persistence to ~/.tmux-browse/device-id.

    Lives in core's ``lib.host_identity``; the federation extension
    re-exports ``get_or_create_device_id`` from there for backward
    compat. We test through the federation surface to confirm the
    re-export still works.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / ".tmux-browse"
        self._patch = mock.patch.object(lib_config,
                                         "STATE_DIR", self.state_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_creates_uuid_on_first_call(self):
        did = federation.get_or_create_device_id()
        self.assertRegex(did, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        path = self.state_dir / "device-id"
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text().strip(), did)

    def test_persistent_across_calls(self):
        a = federation.get_or_create_device_id()
        b = federation.get_or_create_device_id()
        self.assertEqual(a, b)


class PeerRegistryTests(unittest.TestCase):
    """Thread-safe peer dict + TTL-based GC."""

    def setUp(self):
        federation.clear_peers()

    def tearDown(self):
        federation.clear_peers()

    def _peer(self, did="alpha", hostname="alpha", last_seen=None):
        return federation.PeerInfo(
            device_id=did, hostname=hostname,
            dashboard_port=8096, scheme="http",
            version="test",
            last_seen=last_seen if last_seen is not None else int(time.time()),
            addr="10.0.0.1",
        )

    def test_upsert_and_list(self):
        p = self._peer()
        federation.upsert_peer(p)
        rows = federation.list_peers()
        self.assertEqual([r.device_id for r in rows], ["alpha"])

    def test_upsert_replaces_existing(self):
        federation.upsert_peer(self._peer(hostname="alpha"))
        federation.upsert_peer(self._peer(hostname="alpha-renamed"))
        rows = federation.list_peers()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].hostname, "alpha-renamed")

    def test_list_peers_filters_stale(self):
        federation.upsert_peer(self._peer(last_seen=int(time.time()) - 1000))
        rows = federation.list_peers()
        self.assertEqual(rows, [])

    def test_gc_drops_stale(self):
        federation.upsert_peer(self._peer(did="fresh"))
        federation.upsert_peer(self._peer(did="stale", last_seen=int(time.time()) - 1000))
        dropped = federation.gc_peers()
        self.assertEqual(dropped, 1)
        live = [p.device_id for p in federation.list_peers()]
        self.assertEqual(live, ["fresh"])

    def test_peer_info_base_url(self):
        p = federation.PeerInfo(
            device_id="x", hostname="alpha",
            dashboard_port=9090, scheme="https",
            version="t", last_seen=0, addr="10.0.0.5",
        )
        self.assertEqual(p.base_url, "https://10.0.0.5:9090")


class BeaconPayloadTests(unittest.TestCase):
    """Wire format the broadcaster sends and the listener expects."""

    def test_payload_round_trip(self):
        my = federation.PeerInfo(
            device_id="abc-123", hostname="alpha",
            dashboard_port=8096, scheme="http",
            version="0.7.6", last_seen=0, addr="",
        )
        wire = federation._beacon_payload(my, 42)
        msg = json.loads(wire.decode())
        self.assertEqual(msg["device_id"], "abc-123")
        self.assertEqual(msg["hostname"], "alpha")
        self.assertEqual(msg["dashboard_port"], 8096)
        self.assertEqual(msg["scheme"], "http")
        self.assertEqual(msg["version"], "0.7.6")
        self.assertEqual(msg["beacon_seq"], 42)

    def test_payload_under_typical_mtu(self):
        my = federation.PeerInfo(
            device_id="x" * 36, hostname="h" * 64,
            dashboard_port=65535, scheme="https",
            version="x" * 32, last_seen=0, addr="",
        )
        wire = federation._beacon_payload(my, 999_999_999)
        self.assertLess(len(wire), 1400)


class FetchPeerSessionsTests(unittest.TestCase):
    """The urllib-based GET <peer>/api/sessions wrapper."""

    def test_url_error_returns_empty(self):
        from federation import session_merge
        with mock.patch("federation.session_merge.urllib.request.urlopen",
                         side_effect=URLError("boom")):
            rows = session_merge._fetch_peer_sessions("http://10.0.0.1:8096")
        self.assertEqual(rows, [])

    def test_well_formed_response_returns_rows(self):
        from federation import session_merge
        body = json.dumps({"ok": True, "sessions": [
            {"name": "foo"}, {"name": "bar"},
        ]}).encode()
        fake = mock.MagicMock()
        fake.read.return_value = body
        fake.__enter__ = lambda self: self
        fake.__exit__ = lambda self, *a: False
        with mock.patch("federation.session_merge.urllib.request.urlopen", return_value=fake):
            rows = session_merge._fetch_peer_sessions("http://10.0.0.1:8096")
        self.assertEqual([r["name"] for r in rows], ["foo", "bar"])

    def test_malformed_json_returns_empty(self):
        from federation import session_merge
        fake = mock.MagicMock()
        fake.read.return_value = b"not json{"
        fake.__enter__ = lambda self: self
        fake.__exit__ = lambda self, *a: False
        with mock.patch("federation.session_merge.urllib.request.urlopen", return_value=fake):
            rows = session_merge._fetch_peer_sessions("http://10.0.0.1:8096")
        self.assertEqual(rows, [])


class PairedStoreTests(unittest.TestCase):
    """Persistent paired-peers store (~/.tmux-browse/paired-peers.json)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        state_dir = Path(self.tmp.name) / ".tmux-browse"
        self._patch = mock.patch.object(lib_config,
                                         "STATE_DIR", state_dir)
        self._patch.start()
        from federation import store as fed_store
        fed_store.clear_all()
        self.fed_store = fed_store

    def tearDown(self):
        self.fed_store.clear_all()
        self._patch.stop()
        self.tmp.cleanup()

    def test_add_then_is_paired(self):
        self.fed_store.add_paired("peer-x", "alpha")
        self.assertTrue(self.fed_store.is_paired("peer-x"))
        self.assertFalse(self.fed_store.is_paired("peer-y"))

    def test_add_persists_to_disk(self):
        self.fed_store.add_paired("peer-x", "alpha")
        raw = self.fed_store._read_paired()
        self.assertIn("peer-x", raw)
        self.assertEqual(raw["peer-x"]["hostname"], "alpha")

    def test_remove_drops_entry(self):
        self.fed_store.add_paired("peer-x", "alpha")
        self.assertTrue(self.fed_store.remove_paired("peer-x"))
        self.assertFalse(self.fed_store.is_paired("peer-x"))
        self.assertFalse(self.fed_store.remove_paired("peer-x"))

    def test_add_idempotent_keeps_paired_at(self):
        self.fed_store.add_paired("peer-x", "alpha", now=1000)
        self.fed_store.add_paired("peer-x", "alpha-renamed", now=2000)
        raw = self.fed_store._read_paired()
        self.assertEqual(raw["peer-x"]["paired_at"], 1000)
        self.assertEqual(raw["peer-x"]["hostname"], "alpha-renamed")


class PendingRequestTests(unittest.TestCase):
    """In-memory pending pair requests."""

    def setUp(self):
        from federation import store as fed_store
        fed_store.clear_all()
        self.fed_store = fed_store

    def tearDown(self):
        self.fed_store.clear_all()

    def test_add_and_has_pending(self):
        self.fed_store.add_pending("peer-x", "alpha", "10.0.0.1")
        self.assertTrue(self.fed_store.has_pending("peer-x"))

    def test_remove_pending(self):
        self.fed_store.add_pending("peer-x", "alpha", "10.0.0.1")
        self.assertTrue(self.fed_store.remove_pending("peer-x"))
        self.assertFalse(self.fed_store.has_pending("peer-x"))

    def test_list_pending_filters_stale(self):
        self.fed_store.add_pending("stale", "old", "10.0.0.5",
                                    now=int(time.time()) - 7200)
        self.fed_store.add_pending("fresh", "new", "10.0.0.6")
        live = self.fed_store.list_pending()
        names = [r.device_id for r in live]
        self.assertEqual(names, ["fresh"])

    def test_outgoing_round_trip(self):
        self.fed_store.mark_outgoing("peer-x")
        self.assertTrue(self.fed_store.has_outgoing("peer-x"))
        self.fed_store.clear_outgoing("peer-x")
        self.assertFalse(self.fed_store.has_outgoing("peer-x"))


class PairAcceptCallbackGuardTests(unittest.TestCase):
    """The pair-accept-callback handler must refuse callbacks from
    peers we never sent a request to."""

    def setUp(self):
        from federation import store as fed_store
        fed_store.clear_all()
        self.fed_store = fed_store

    def tearDown(self):
        self.fed_store.clear_all()

    def test_callback_without_outgoing_is_refused(self):
        from federation import routes as routes_peers

        class FakeHandler:
            def __init__(self):
                self.payload = None
                self.status = None
            def _send_json(self, obj, status=200):
                self.payload = obj
                self.status = status

        h = FakeHandler()
        routes_peers.h_pair_accept_callback(h, mock.MagicMock(),
                                             {"device_id": "stranger"})
        self.assertEqual(h.status, 409)
        self.assertFalse(h.payload["ok"])
        self.assertFalse(self.fed_store.is_paired("stranger"))

    def test_callback_with_outgoing_pairs(self):
        from federation import routes as routes_peers

        class FakeHandler:
            def __init__(self):
                self.payload = None
                self.status = None
            def _send_json(self, obj, status=200):
                self.payload = obj
                self.status = status

        self.fed_store.mark_outgoing("known")
        h = FakeHandler()
        routes_peers.h_pair_accept_callback(h, mock.MagicMock(),
                                             {"device_id": "known",
                                              "hostname": "alpha"})
        self.assertTrue(h.payload["ok"])
        self.assertTrue(self.fed_store.is_paired("known"))
        self.assertFalse(self.fed_store.has_outgoing("known"))


class AggregationGatedOnPairing(unittest.TestCase):
    """``merge_peer_sessions`` must only fetch from paired peers."""

    def setUp(self):
        from federation import store as fed_store
        federation.clear_peers()
        fed_store.clear_all()
        self.fed_store = fed_store

    def tearDown(self):
        federation.clear_peers()
        self.fed_store.clear_all()

    def test_unpaired_peer_skipped(self):
        from federation import session_merge
        federation.upsert_peer(federation.PeerInfo(
            device_id="not-paired", hostname="x",
            dashboard_port=9999, scheme="http",
            version="t", last_seen=int(time.time()),
            addr="10.0.0.99",
        ))
        out: list[dict] = []
        with mock.patch("federation.session_merge._fetch_peer_sessions") as fake_fetch:
            session_merge.merge_peer_sessions(out)
            fake_fetch.assert_not_called()
        self.assertEqual(out, [])

    def test_paired_peer_attempted(self):
        from federation import session_merge
        federation.upsert_peer(federation.PeerInfo(
            device_id="paired-x", hostname="x",
            dashboard_port=9999, scheme="http",
            version="t", last_seen=int(time.time()),
            addr="10.0.0.99",
        ))
        self.fed_store.add_paired("paired-x", "x")
        out: list[dict] = []
        with mock.patch("federation.session_merge._fetch_peer_sessions",
                         return_value=[{"name": "remote-foo"}]) as fake_fetch:
            session_merge.merge_peer_sessions(out)
            fake_fetch.assert_called_once()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "x:remote-foo")
        self.assertEqual(out[0]["device_id"], "paired-x")


if __name__ == "__main__":
    unittest.main()
