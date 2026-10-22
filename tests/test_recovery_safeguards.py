"""Regressions from the independent recovery audit; disposable state only."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from hearing_compiler.audio import audio_path, receive_audio
from hearing_compiler.pipeline import InputError
from hearing_compiler.server import make_server
from hearing_compiler.store import Conflict, Store
from audio_test_helpers import wav_bytes
from test_pipeline import fixture, ROOT


class RecoverySafeguardTests(unittest.TestCase):
    def test_generated_audio_id_cannot_replace_completed_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            data = wav_bytes(2)
            asset = receive_audio(root, io.BytesIO(data), len(data), "fictional.wav")
            document = fixture("sample-hearing")
            job_id = "audio-" + asset["sha256"][:24]
            document["hearing"]["id"] = job_id
            store.import_one(document)
            store.review(job_id, 0, [{"path": "/testimonies/0/name", "value": "Reviewer correction"}])
            saved = store.get(job_id)
            with self.assertRaises(Conflict):
                store.enqueue_audio({**document["hearing"], "source_scope": "public_hearing"}, asset, False)
            self.assertEqual(store.get(job_id), saved)
            self.assertEqual(Store(root).get(job_id), saved)

    def test_same_size_changed_media_rejected_and_exact_reupload_repairs_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            document = fixture("sample-hearing")
            result = store.import_one(document)
            data = wav_bytes(2)
            asset = receive_audio(root, io.BytesIO(data), len(data), "fictional.wav")
            store.attach_audio(result["id"], asset)
            target, _ = audio_path(store, result["id"])  # Prime the verification cache.
            old_stat = target.stat()
            changed = bytearray(data)
            changed[-1] ^= 1
            target.write_bytes(changed)
            os.utime(target, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            with self.assertRaisesRegex(InputError, "SHA256"):
                audio_path(store, result["id"])
            receive_audio(root, io.BytesIO(data), len(data), "fictional.wav")
            self.assertEqual(audio_path(store, result["id"])[0].read_bytes(), data)

    def test_serialization_error_and_failure_status_cannot_poison_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            result = store.import_one(fixture("sample-hearing"))
            before = store.get(result["id"])
            with self.assertRaises(InputError):
                store.review(result["id"], 0, [{"path": "/testimonies/0/name", "value": "\ud800"}])
            self.assertEqual(store.get(result["id"]), before)
            with patch.object(store, "_save", side_effect=UnicodeEncodeError("utf-8", "\ud800", 0, 1, "invalid")):
                with self.assertRaises(UnicodeEncodeError):
                    store.set_model("whisperkit:valid")
            self.assertEqual(store.get(result["id"]), before)
            self.assertEqual(store.settings()["model_id"], "")
            with patch("hearing_compiler.store.os.replace", side_effect=PermissionError("injected storage failure")):
                with self.assertRaises(PermissionError):
                    store.fail_audio(result["id"], "extraction", "injected provider failure")
            self.assertEqual(store.get(result["id"]), before)
            self.assertEqual(Store(root).get(result["id"]), before)
            store.set_model("whisperkit:valid", "srt")
            self.assertEqual(Store(root).settings()["model_id"], "whisperkit:valid")

    def test_lost_save_response_retry_is_idempotent_and_payload_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            job_id = store.import_one(fixture("sample-hearing"))["id"]
            operation = {"op": "add_argument", "testimony": 0, "statement": 0,
                         "value": "Synthetic reviewer-added argument.", "evidence": ["s02"]}
            # Choose an actual original ID; fixtures retain their own names.
            operation["evidence"] = [store.get(job_id)["input"]["segments"][0]["id"]]
            saved = store.review(job_id, 0, [], [operation], request_id="lost-response-1")
            repeated = Store(root).review(job_id, 0, [], [operation], request_id="lost-response-1")
            self.assertEqual(repeated, saved)
            with self.assertRaises(Conflict):
                store.review(job_id, 0, [], [{**operation, "value": "Different"}], request_id="lost-response-1")
            self.assertEqual(store.get(job_id), saved)

    def test_unsaved_failure_is_visible_and_recovers_when_storage_returns(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            data = wav_bytes(2)
            asset = receive_audio(root, io.BytesIO(data), len(data), "fictional.wav")
            metadata = {**fixture("sample-hearing")["hearing"], "source_scope": "public_hearing"}
            job_id = store.enqueue_audio(metadata, asset, True)["id"]
            store.claim_audio(True)
            store.defer_failure(job_id, "transcription", "Provider stopped", PermissionError("disk unavailable"))
            visible = store.get(job_id)
            self.assertFalse(visible["persistence_error"]["persisted"])
            self.assertIn("storage recovery is pending", visible["error"])
            with patch("hearing_compiler.store.os.replace", side_effect=PermissionError("still unavailable")):
                with self.assertRaises(PermissionError):
                    store.flush_failures()
            self.assertEqual(store.data["jobs"][job_id]["processing"], "Transcribing")
            self.assertIn(job_id, store.pending_failures)
            store.flush_failures()
            self.assertNotIn(job_id, store.pending_failures)
            restored = Store(root).get(job_id)
            self.assertEqual(restored["processing"], "Failed")
            self.assertEqual(restored["error"], "Provider stopped")
            self.assertEqual(restored["failed_stage"], "transcription")

    def test_concurrent_failed_settings_save_cannot_detach_completed_import(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entered, release = threading.Event(), threading.Event()
            from hearing_compiler.pipeline import extract
            def delayed(document):
                entered.set()
                self.assertTrue(release.wait(5))
                return extract(document)
            store = Store(root, extractor=delayed)
            results, errors = [], []
            def import_one():
                try:
                    results.append(store.import_one(fixture("sample-hearing")))
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=import_one)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with patch("hearing_compiler.store.os.replace", side_effect=PermissionError("injected")):
                    with self.assertRaises(PermissionError):
                        store.set_model("whisperkit:valid")
            finally:
                release.set()
                worker.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(results[0]["processing"], "Complete")
            self.assertEqual(store.get(results[0]["id"])["processing"], "Complete")
            self.assertEqual(Store(root).get(results[0]["id"])["processing"], "Complete")

    def test_invalid_provider_error_is_diagnostic_text_not_a_poisoned_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            job_id = store.import_one(fixture("sample-hearing"))["id"]
            store.defer_failure(job_id, "extraction", "Bad provider \ud800", ValueError("Bad storage \ud801"))
            self.assertIn("\\ud800", json.dumps(store.get(job_id)))
            store.flush_failures()
            self.assertEqual(Store(root).get(job_id)["error"], "Bad provider \\ud800")
            self.assertEqual(store.import_one(fixture("no-questions"))["processing"], "Complete")

    def test_actual_exit_during_new_extraction_retains_source_and_previous_correction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            prior = fixture("sample-hearing")
            store.import_one(prior)
            store.review(prior["hearing"]["id"], 0,
                         [{"path": "/testimonies/0/name", "value": "Preserved correction"}])
            earlier = store.get(prior["hearing"]["id"])
            next_document = fixture("no-questions")
            source = root / "next-input.json"
            source.write_text(json.dumps(next_document), encoding="utf-8")
            script = (
                "import json,os,sys; from pathlib import Path; from hearing_compiler.store import Store; "
                "store=Store(Path(sys.argv[1]),extractor=lambda document:os._exit(73)); "
                "store.import_one(json.loads(Path(sys.argv[2]).read_text(encoding='utf-8')))"
            )
            completed = subprocess.run([sys.executable, "-c", script, str(root), str(source)], cwd=ROOT, timeout=15)
            self.assertEqual(completed.returncode, 73)
            restored = Store(root)
            self.assertEqual(restored.get(prior["hearing"]["id"]), earlier)
            interrupted = restored.get(next_document["hearing"]["id"])
            self.assertEqual(interrupted["input"], next_document)
            self.assertEqual(interrupted["processing"], "Failed")
            self.assertEqual(interrupted["failed_stage"], "extraction")
            self.assertEqual(restored.retry(interrupted["id"])["processing"], "Complete")

    def test_shutdown_holds_lease_until_incomplete_upload_writer_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = make_server(root, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            token = json.loads(urlopen(f"http://127.0.0.1:{server.server_port}/api/session").read())["token"]
            from urllib.parse import quote
            metadata = {**fixture("sample-hearing")["hearing"], "source_file": "held.wav",
                        "source_scope": "public_hearing"}
            data = wav_bytes(2)
            client = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
            closed = threading.Event()
            failures = []
            def stop():
                try:
                    server.shutdown()
                    server.server_close()
                except Exception as error:
                    failures.append(error)
                finally:
                    closed.set()
            stopper = threading.Thread(target=stop, daemon=True)
            try:
                headers = (f"POST /api/audio/upload?metadata={quote(json.dumps(metadata))} HTTP/1.1\r\n"
                           f"Host: 127.0.0.1:{server.server_port}\r\nX-Review-Token: {token}\r\n"
                           f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode()
                client.sendall(headers + data[:16])
                deadline = time.monotonic() + 5
                while not list((root / "audio").glob("upload-*.tmp")) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(list((root / "audio").glob("upload-*.tmp")))
                stopper.start()
                self.assertFalse(closed.wait(.7))
                with self.assertRaisesRegex(InputError, "already open"):
                    make_server(root, 0)
                client.sendall(data[16:])
                while client.recv(65536):
                    pass
                self.assertTrue(closed.wait(5))
                self.assertEqual(failures, [])
                reopened = make_server(root, 0)
                try:
                    self.assertEqual(len(reopened.store.list()), 1)
                finally:
                    reopened.server_close()
            finally:
                client.close()
                if not stopper.is_alive() and not closed.is_set():
                    server.shutdown()
                    server.server_close()
                stopper.join(5) if stopper.ident else None
                thread.join(5)
