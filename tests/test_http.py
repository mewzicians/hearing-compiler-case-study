from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from hearing_compiler.server import make_server
from hearing_compiler.pipeline import InputError
from test_pipeline import fixture


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temporary.name), 0, seed=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = json.loads(urlopen(self.base + "/api/session").read())["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, path, body=None, token=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Review-Token"] = self.token
        if origin:
            headers["Origin"] = origin
        return urlopen(Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers))

    def test_end_to_end_http_import_review_and_output(self):
        result = json.loads(self.request("/api/import", {"documents": [fixture("sample-hearing")]}).read())
        self.assertEqual(result[0]["processing"], "Complete")
        saved = json.loads(self.request("/api/review", {
            "id": "sample-hearing", "revision": 0,
            "changes": [{"path": "/questions/0/asker", "value": "Rep. Avery", "review": "APPROVED"}],
        }).read())
        self.assertIn("Rep. Avery", saved["markdown"])
        self.assertEqual(json.loads(self.request("/api/hearings").read())[0]["revision"], 1)
        self.assertIn(b"Hearing Compiler", self.request("/").read())

    def test_window_version_comes_from_a_packaged_stamp_or_the_source_checkout(self):
        from hearing_compiler import version
        served = json.loads(urlopen(self.base + "/api/version").read())
        self.assertEqual(served["version"], version.VERSION)
        self.assertTrue(served["label"].startswith(f"v{version.VERSION}"))
        with tempfile.TemporaryDirectory() as folder:
            stamp = version.write_stamp(Path(folder), "d728eddeedd9162790dd2f0303011dbdc9411893", "14")
            version.info.cache_clear()
            try:
                with patch.object(version, "STAMP", stamp):
                    packaged = version.info()
            finally:
                version.info.cache_clear()
        self.assertEqual(packaged["label"], f"v{version.VERSION} · build 14 · d728edd")
        self.assertTrue(packaged["packaged"])
        update = json.loads(urlopen(self.base + "/api/update").read())
        self.assertEqual((update["available"], update["can_install"]), (False, False))
        with self.assertRaises(HTTPError) as refused:
            self.request("/api/update/install", {})
        self.assertEqual(refused.exception.code, 400)
        urlopen(self.base + "/api/session")  # The server keeps running after a refused install.

    def test_mutation_requires_local_session_and_origin(self):
        for options in ({"token": False}, {"origin": "https://unrelated.example"}):
            with self.assertRaises(HTTPError) as caught:
                self.request("/api/import", {"documents": [fixture("sample-hearing")]}, **options)
            self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.server.store.list(), [])

    def test_unknown_resource_and_bad_payload_return_clear_errors(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/hearings/missing")
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/import", {"documents": []})
        self.assertEqual(caught.exception.code, 400)

    def test_second_app_cannot_overwrite_the_same_workspace(self):
        with self.assertRaisesRegex(InputError, "already open"):
            make_server(Path(self.temporary.name), 0, seed=False)

    def test_literal_loopback_startup_does_not_depend_on_reverse_dns(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("socket.getfqdn", side_effect=OSError("Reverse DNS unavailable")) as lookup:
                server = make_server(Path(folder), 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertEqual(server.server_address[0], "127.0.0.1")
                self.assertEqual(json.loads(urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/hearings").read()), [])
                lookup.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def test_retired_seeding_cannot_create_or_modify_a_workspace(self):
        absent = Path(self.temporary.name) / "uncreated"
        with self.assertRaisesRegex(InputError, "sample seeding is retired"):
            make_server(absent, 0, seed=True)
        self.assertFalse(absent.exists())
        state = Path(self.temporary.name) / "state.json"
        before = state.read_bytes()
        with self.assertRaisesRegex(InputError, "sample seeding is retired"):
            make_server(Path(self.temporary.name), 0, seed=True)
        self.assertEqual(state.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
