from __future__ import annotations

import copy
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from docx import Document

from hearing_compiler.pipeline import InputError
from hearing_compiler.audio import receive_audio
from hearing_compiler.server import make_server
from hearing_compiler.store import Conflict, Store
from test_pipeline import fixture
from audio_test_helpers import wav_bytes


ARGUMENT = {
    "op": "add_argument", "testimony": 0, "statement": 0,
    "value": "A short omitted argument repaired by the reviewer.",
    "evidence": ["s02"],
}


class SavedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)
        self.document = fixture("sample-hearing")
        self.store.import_one(self.document)

    def tearDown(self):
        self.temporary.cleanup()

    def test_structural_repair_survives_restart_archive_and_duplicate_intake(self):
        original_segments = copy.deepcopy(self.document["segments"])
        edited = self.store.review("sample-hearing", 0, operations=[ARGUMENT])
        restarted = Store(self.directory)
        saved = restarted.get("sample-hearing")
        self.assertEqual(saved["draft"], edited["draft"])
        self.assertEqual(saved["draft"]["segments"], original_segments)
        self.assertIn(ARGUMENT["value"], saved["markdown"])
        self.assertIn("[draft; review needed]", saved["markdown"])
        snapshot = restarted.export_snapshot("sample-hearing", saved["revision"])
        self.assertEqual(snapshot["job"]["draft"], saved["draft"])
        self.assertEqual(snapshot["job"]["input"], self.document)
        self.assertEqual(snapshot["kind"], "hearing-review-archive")
        self.assertFalse(snapshot["recording_included"])
        self.assertTrue(restarted.import_one(self.document)["reused"])
        self.assertEqual(restarted.get("sample-hearing")["draft"], edited["draft"])

    def test_failed_structural_save_rolls_back_memory_and_disk(self):
        before = self.store.get("sample-hearing")
        with patch("hearing_compiler.store.os.replace", side_effect=PermissionError("disk unavailable")):
            with self.assertRaises(PermissionError):
                self.store.review("sample-hearing", 0, operations=[ARGUMENT])
        self.assertEqual(self.store.get("sample-hearing"), before)
        self.assertEqual(Store(self.directory).get("sample-hearing"), before)

    def test_invalid_structural_batch_cannot_save_its_first_operation(self):
        before = self.store.get("sample-hearing")
        disk = self.store.path.read_bytes()
        with self.assertRaises(InputError):
            self.store.review("sample-hearing", 0, operations=[ARGUMENT, {
                "op": "set_evidence", "path": "/segments/0/text", "evidence": ["s01"],
            }])
        self.assertEqual(self.store.get("sample-hearing"), before)
        self.assertEqual(self.store.path.read_bytes(), disk)

    def test_stale_export_or_review_cannot_misrepresent_the_saved_revision(self):
        self.store.review("sample-hearing", 0, operations=[ARGUMENT])
        with self.assertRaises(Conflict):
            self.store.export_snapshot("sample-hearing", 0)
        with self.assertRaises(Conflict):
            self.store.review("sample-hearing", 0, operations=[ARGUMENT])
        self.assertEqual(self.store.get("sample-hearing")["revision"], 1)

    def test_archive_excludes_recording_bytes_and_local_paths_but_keeps_identity(self):
        raw = wav_bytes(1)
        asset = receive_audio(self.directory, io.BytesIO(raw), len(raw), "original.wav")
        self.store.attach_audio("sample-hearing", asset)
        snapshot = self.store.export_snapshot("sample-hearing")
        self.assertEqual(snapshot["job"]["audio"]["sha256"], asset["sha256"])
        self.assertEqual(snapshot["job"]["audio"]["filename"], "original.wav")
        self.assertEqual(snapshot["job"]["audio"]["bytes"], asset["bytes"])
        self.assertNotIn("path", snapshot["job"]["audio"])
        self.assertNotIn("audio_url", snapshot["job"])
        self.assertNotIn(str(self.directory), json.dumps(snapshot))
        self.assertFalse(snapshot["recording_included"])
        self.assertEqual(snapshot["job"]["draft"]["segments"], self.document["segments"])

    def test_failed_import_save_does_not_leave_a_phantom_job(self):
        document = fixture("no-questions")
        with patch("hearing_compiler.store.os.replace", side_effect=PermissionError("disk unavailable")):
            with self.assertRaises(PermissionError):
                self.store.import_one(document)
        self.assertEqual([job["id"] for job in self.store.list()], ["sample-hearing"])
        self.assertEqual(Store(self.directory).list(), self.store.list())
        self.assertEqual(self.store.import_one(document)["processing"], "Complete")

    def test_failed_retry_save_preserves_retryable_job(self):
        failed = fixture("broken-order")
        self.store.import_one(failed)
        before = self.store.get("broken-order")
        repaired = copy.deepcopy(failed)
        repaired["segments"].sort(key=lambda item: item["start"])
        with patch("hearing_compiler.store.os.replace", side_effect=PermissionError("disk unavailable")):
            with self.assertRaises(PermissionError):
                self.store.retry("broken-order", repaired)
        self.assertEqual(self.store.get("broken-order"), before)
        self.assertEqual(Store(self.directory).get("broken-order"), before)
        self.assertEqual(self.store.retry("broken-order", repaired)["processing"], "Complete")


class ExportHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temporary.name), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = json.loads(urlopen(self.base + "/api/session").read())["token"]
        self.server.store.import_one(fixture("sample-hearing"))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def post(self, path, body):
        return urlopen(Request(self.base + path, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "X-Review-Token": self.token,
        }))

    def test_repaired_saved_content_reaches_word_json_and_markdown_downloads(self):
        saved = json.loads(self.post("/api/review", {
            "id": "sample-hearing", "revision": 0, "operations": [ARGUMENT],
        }).read())
        draft_before = copy.deepcopy(saved["draft"])
        for format_name in ("docx", "json", "md"):
            with self.subTest(format=format_name):
                response = urlopen(self.base + f"/api/export/sample-hearing?format={format_name}&revision=1")
                self.assertIn(f'.{format_name}"', response.headers["Content-Disposition"])
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                raw = response.read()
                if format_name == "docx":
                    self.assertEqual(response.headers["Content-Type"],
                                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                    text = "\n".join(p.text for p in Document(io.BytesIO(raw)).paragraphs)
                    self.assertIn(ARGUMENT["value"], text)
                    self.assertIn("[draft; review needed]", text)
                    self.assertIn("Speaker 2", text)
                    self.assertIn("s02", text)
                elif format_name == "json":
                    snapshot = json.loads(raw)
                    self.assertEqual(snapshot["job"]["draft"], draft_before)
                    self.assertFalse(snapshot["recording_included"])
                else:
                    self.assertEqual(raw.decode(), saved["markdown"])
        self.assertEqual(self.server.store.get("sample-hearing")["draft"], draft_before)

    def test_export_rejects_stale_revision_invalid_format_and_missing_hearing(self):
        self.server.store.review("sample-hearing", 0, operations=[ARGUMENT])
        for suffix, code in (("sample-hearing?format=docx&revision=0", 409),
                             ("sample-hearing?format=exe", 400),
                             ("sample-hearing?format=json&revision=-1", 400),
                             ("missing?format=docx", 404)):
            with self.subTest(path=suffix):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self.base + "/api/export/" + suffix)
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
