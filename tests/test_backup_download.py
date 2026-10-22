from __future__ import annotations

from http.cookies import SimpleCookie
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from hearing_compiler.backup_download import BackupDownloads, COOKIE, DOWNLOAD_PATH
from hearing_compiler.pipeline import InputError
from hearing_compiler.server import make_server
from hearing_compiler.store import Store
from test_pipeline import fixture


class BackupDownloadTests(unittest.TestCase):
    def populate(self, root):
        store = Store(Path(root))
        store.import_one(fixture("sample-hearing"))
        return store

    def test_expiration_one_use_and_close_remove_only_owned_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.populate(folder)
            time = [10]
            manager = BackupDownloads(store, clock=lambda: time[0], ttl=5)
            first = manager.prepare()
            with manager.take(first) as (stream, size):
                self.assertEqual(size, len(stream.read()))
            with self.assertRaises(InputError):
                with manager.take(first):
                    pass
            second = manager.prepare()
            time[0] = 16
            manager.prune()
            self.assertEqual(list(store.directory.glob(".backup-*.zip")), [])
            with self.assertRaises(InputError):
                with manager.take(second):
                    pass
            manager.prepare()
            manager.close()
            self.assertEqual(list(store.directory.glob(".backup-*.zip")), [])
            self.assertEqual(Store(store.directory).get("sample-hearing")["processing"], "Complete")

    def test_registry_limits_and_failed_verification_cleanup_are_truthful(self):
        with tempfile.TemporaryDirectory() as folder:
            store = self.populate(folder)
            manager = BackupDownloads(store, maximum=1)
            first = manager.prepare()
            with self.assertRaisesRegex(InputError, "pending backup"):
                manager.prepare()
            with manager.take(first):
                with self.assertRaisesRegex(InputError, "pending backup"):
                    manager.prepare()
            with patch("hearing_compiler.backup_download.verify_backup", side_effect=InputError("injected verification")):
                with self.assertRaisesRegex(InputError, "injected verification"):
                    manager.prepare()
            self.assertEqual(manager.reserved, 0)
            self.assertEqual(manager.pending, {})
            self.assertEqual(list(store.directory.glob(".backup-*.zip")), [])
            manager.close()

    def test_changed_snapshot_is_rejected_before_stream_and_failed_read_still_cleans(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = BackupDownloads(self.populate(folder))
            key = manager.prepare()
            path = manager.pending[key]["path"]
            path.write_bytes(b"changed archive")
            with self.assertRaisesRegex(InputError, "prepared backup changed"):
                with manager.take(key):
                    self.fail("Changed bytes must never enter the stream.")
            self.assertFalse(path.exists())
            key = manager.prepare()
            with self.assertRaisesRegex(RuntimeError, "interrupted consumer"):
                with manager.take(key):
                    raise RuntimeError("interrupted consumer")
            self.assertEqual(list(manager.store.directory.glob(".backup-*.zip")), [])
            self.assertEqual(manager.active, set())
            manager.close()

    def test_shutdown_during_prepare_withholds_capability_and_cleans_snapshot(self):
        from hearing_compiler.workspace_backup import create_backup
        with tempfile.TemporaryDirectory() as folder:
            manager = BackupDownloads(self.populate(folder))
            entered, resume = threading.Event(), threading.Event()
            errors = []
            def held_create(store, **kwargs):
                path = create_backup(store, **kwargs)
                entered.set()
                self.assertTrue(resume.wait(5))
                return path
            def prepare():
                try:
                    manager.prepare()
                except Exception as error:
                    errors.append(error)
            with patch("hearing_compiler.backup_download.create_backup", side_effect=held_create):
                thread = threading.Thread(target=prepare)
                thread.start()
                self.assertTrue(entered.wait(5))
                manager.stop_accepting()
                resume.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], InputError)
            self.assertEqual(manager.pending, {})
            self.assertEqual(manager.reserved, 0)
            self.assertEqual(list(manager.store.directory.glob(".backup-*.zip")), [])
            manager.close()

    def test_cleanup_reservation_blocks_concurrent_prepare_until_deletion_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = BackupDownloads(self.populate(folder), maximum=1)
            entered, resume = threading.Event(), threading.Event()
            original_unlink = Path.unlink
            errors = []
            def held_unlink(path, *args, **kwargs):
                if path.name.startswith(".backup-"):
                    entered.set()
                    self.assertTrue(resume.wait(5))
                return original_unlink(path, *args, **kwargs)
            def prepare():
                try:
                    manager.prepare()
                except Exception as error:
                    errors.append(error)
            with patch("hearing_compiler.backup_download.verify_backup", side_effect=InputError("verification failed")), \
                    patch.object(Path, "unlink", held_unlink):
                thread = threading.Thread(target=prepare)
                thread.start()
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(InputError, "pending backup"):
                    manager.prepare()
                resume.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(manager.reserved, 0)
            self.assertEqual(len(errors), 1)
            self.assertEqual(list(manager.store.directory.glob(".backup-*.zip")), [])
            manager.close()

    def test_partial_zip_creation_keeps_primary_error_and_owned_cleanup_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = BackupDownloads(self.populate(folder), maximum=1)
            original_unlink = Path.unlink
            def denied(path, *args, **kwargs):
                if path.name.startswith(".backup-"):
                    raise PermissionError("injected partial creation cleanup")
                return original_unlink(path, *args, **kwargs)
            with patch("hearing_compiler.workspace_backup.zipfile.ZipFile.writestr", side_effect=OSError("primary ZIP write failed")), \
                    patch.object(Path, "unlink", denied), self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(OSError, "primary ZIP write failed"):
                    manager.prepare()
            self.assertEqual(manager.reserved, 0)
            self.assertEqual(len(manager.cleanup_pending), 1)
            self.assertEqual(len(list(manager.store.directory.glob(".backup-*.zip"))), 1)
            manager.prune()
            manager.close()
            self.assertEqual(list(manager.store.directory.glob(".backup-*.zip")), [])

    def test_cleanup_failure_preserves_transfer_error_and_retry_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = BackupDownloads(self.populate(folder))
            key = manager.prepare()
            path = manager.pending[key]["path"]
            unlink = Path.unlink
            def failure(target, *args, **kwargs):
                if target == path:
                    raise PermissionError("injected snapshot cleanup failure")
                return unlink(target, *args, **kwargs)
            with patch.object(Path, "unlink", failure), self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, "primary transfer failure"):
                    with manager.take(key):
                        raise RuntimeError("primary transfer failure")
            self.assertEqual(manager.cleanup_pending, {path})
            self.assertTrue(path.exists())
            manager.prune()
            self.assertFalse(path.exists())
            self.assertEqual(manager.cleanup_pending, set())
            manager.close()

    def test_prepare_binds_verified_handle_bytes_and_retains_failed_cleanup(self):
        from hearing_compiler.workspace_backup import verify_backup
        with tempfile.TemporaryDirectory() as folder:
            manager = BackupDownloads(self.populate(folder), maximum=1)
            def replace_after_verification(source):
                result = verify_backup(source)
                Path(source.name).write_bytes(b"not the verified snapshot")
                return result
            with patch("hearing_compiler.backup_download.verify_backup", side_effect=replace_after_verification):
                with self.assertRaisesRegex(InputError, "changed during verification"):
                    manager.prepare()
            self.assertEqual(manager.pending, {})
            self.assertEqual(list(manager.store.directory.glob(".backup-*.zip")), [])
            unlink = Path.unlink
            def denied(target, *args, **kwargs):
                if target.name.startswith(".backup-"):
                    raise PermissionError("injected prepare cleanup failure")
                return unlink(target, *args, **kwargs)
            with patch("hearing_compiler.backup_download.verify_backup", side_effect=InputError("primary verification failure")), \
                    patch.object(Path, "unlink", denied), self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(InputError, "primary verification failure"):
                    manager.prepare()
            self.assertEqual(len(manager.cleanup_pending), 1)
            with patch.object(Path, "unlink", denied):
                with self.assertRaises(PermissionError):
                    manager.prepare()
                with self.assertRaises(PermissionError):
                    manager.close()
            self.assertEqual(len(list(manager.store.directory.glob(".backup-*.zip"))), 1)
            manager.prune()
            manager.close()
            self.assertEqual(list(manager.store.directory.glob(".backup-*.zip")), [])

    def test_native_http_cookie_access_source_integrity_and_shutdown_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            server = make_server(Path(folder), 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            token = json.loads(urlopen(base + "/api/session").read())["token"]
            server.store.import_one(fixture("sample-hearing"))
            before = server.store.get("sample-hearing")["draft"]
            def prepare(**extra):
                return urlopen(Request(base + "/api/workspace/backup-prepare", data=b"{}",
                                       headers={"X-Review-Token": token, **extra}))
            try:
                with self.assertRaises(HTTPError) as missing:
                    urlopen(base + DOWNLOAD_PATH)
                self.assertEqual(missing.exception.code, 403)
                with self.assertRaises(HTTPError) as forged:
                    prepare(Origin="https://unrelated.example")
                self.assertEqual(forged.exception.code, 403)
                with prepare() as response:
                    self.assertEqual(json.loads(response.read()), {"download_url": DOWNLOAD_PATH})
                    raw_cookie = response.headers["Set-Cookie"]
                    parsed = SimpleCookie(raw_cookie)[COOKIE]
                    self.assertTrue(parsed["httponly"])
                    self.assertEqual(parsed["samesite"], "Strict")
                    self.assertEqual(parsed["path"], DOWNLOAD_PATH)
                    auth = f"{COOKIE}={parsed.value}"
                with self.assertRaises(HTTPError) as cross_site:
                    urlopen(Request(base + DOWNLOAD_PATH, headers={"Cookie": auth, "Sec-Fetch-Site": "cross-site"}))
                self.assertEqual(cross_site.exception.code, 403)
                with self.assertRaises(HTTPError) as same_site:
                    urlopen(Request(base + DOWNLOAD_PATH, headers={"Cookie": auth, "Sec-Fetch-Site": "same-site",
                                                                  "Sec-Fetch-Dest": "image"}))
                self.assertEqual(same_site.exception.code, 403)
                with urlopen(Request(base + DOWNLOAD_PATH, headers={"Cookie": auth})) as response:
                    data = response.read()
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    self.assertEqual(len(data), int(response.headers["Content-Length"]))
                    self.assertIn("Max-Age=0", response.headers["Set-Cookie"])
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    state = json.loads(archive.read("state.json"))
                self.assertEqual(state["jobs"]["sample-hearing"]["draft"], before)
                with self.assertRaises(HTTPError):
                    urlopen(Request(base + DOWNLOAD_PATH, headers={"Cookie": auth}))
                with prepare() as response:
                    response.read()
                self.assertEqual(len(list(Path(folder).glob(".backup-*.zip"))), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(list(Path(folder).glob(".backup-*.zip")), [])
