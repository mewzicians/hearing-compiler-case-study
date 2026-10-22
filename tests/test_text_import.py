from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hearing_compiler.audio import receive_audio
from hearing_compiler.pipeline import InputError, extract, fields, markdown, validate
from hearing_compiler.server import make_server
from hearing_compiler.store import Conflict, Store
from hearing_compiler.text_import import normalize_text
from hearing_compiler.word_export import docx_bytes
from audio_test_helpers import wav_bytes

TEXT = "\ufeffI am Nora Moss with Fictional Access Office. I support H.8123.\r\n\r\nThe notice must arrive fourteen days before renewal.\n00:01:20 [Speaker 2] Who sends it?"
HEARING = {"title": "Fictional text pilot", "date": "2026-10-06",
           "committee": "Example Committee", "source_file": "pilot.txt"}


class TextImportTests(unittest.TestCase):
    def document(self, text=TEXT):
        return normalize_text(text, HEARING, "pilot.txt", synthetic=True)

    def test_original_text_spans_and_unavailable_timing_are_lossless(self):
        doc = self.document()
        self.assertEqual(doc["source_text"], TEXT)
        self.assertEqual(doc["source_text_sha256"], hashlib.sha256(TEXT.encode()).hexdigest())
        self.assertEqual([s["source_line"] for s in doc["segments"]], [1, 3, 4])
        for s in doc["segments"]:
            self.assertIsNone(s["start"])
            self.assertIsNone(s["end"])
            self.assertTrue(s["speaker_unavailable"])
            self.assertTrue(s["timing_unavailable"])
            self.assertEqual(TEXT[s["source_char_start"]:s["source_char_end"]], s["text"])
        self.assertEqual(doc, self.document())

    def test_empty_binary_invalid_unicode_and_wrong_extension_fail(self):
        for text in ["", "\ufeff \r\n", "text\x00binary", "\ud800"]:
            with self.subTest(text=repr(text)), self.assertRaises(InputError):
                self.document(text)
        with self.assertRaises(InputError):
            normalize_text(TEXT, HEARING, "audio.mp3", synthetic=True)

    def test_long_line_chunks_are_source_spans_not_audio_times(self):
        text = "A" * 50001
        doc = self.document(text)
        self.assertEqual([len(s["text"]) for s in doc["segments"]], [50000, 1])
        self.assertEqual("".join(s["text"] for s in doc["segments"]), text)
        self.assertTrue(all(s["start"] is None for s in doc["segments"]))

    def test_fabricated_timing_bad_hash_missing_passage_or_bad_line_rejected(self):
        for corruption in ["timing", "hash", "span", "omission", "line"]:
            doc = self.document()
            if corruption == "timing":
                doc["segments"][0].update(start=0, end=1)
            elif corruption == "hash":
                doc["source_text_sha256"] = "wrong"
            elif corruption == "span":
                doc["segments"][0]["source_char_start"] += 1
            elif corruption == "line":
                doc["segments"][0]["source_line"] = True
            else:
                doc["segments"].pop(1)
            with self.subTest(corruption=corruption), self.assertRaises(InputError):
                validate(doc)

    def test_text_and_timed_json_are_separate_validation_paths(self):
        doc = self.document()
        doc.pop("source_format")
        with self.assertRaises(InputError):
            validate(doc)
        with self.assertRaises(InputError):
            normalize_text(TEXT, {**HEARING, "date": ""}, "pilot.txt", synthetic=True)

    def test_draft_fields_are_unapproved_and_export_uses_lines(self):
        doc = self.document()
        before = copy.deepcopy(doc)
        draft = extract(doc)
        self.assertEqual(doc, before)
        self.assertTrue(all(item["review"] != "READY" and item["review"] != "APPROVED"
                            for _, item in fields(draft)))
        note = markdown(draft)
        self.assertIn("Text line 1", note)
        self.assertIn("Time unavailable", note)
        self.assertNotIn("00:00:00", note)
        self.assertIn("transcript", note.lower())
        self.assertTrue(docx_bytes(draft).startswith(b"PK"))

    def test_review_duplicate_restart_and_archive_preserve_source(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            result = store.import_one(self.document())
            self.assertEqual(result["processing"], "Complete")
            job = store.get(result["id"])
            saved = store.review(job["id"], job["revision"],
                changes=[{"path": "/testimonies/0/name", "value": "Nora Moss", "review": "NEEDS_REVIEW"}])
            self.assertTrue(store.import_one(self.document())["reused"])
            restored = Store(Path(directory)).get(job["id"])
            self.assertEqual(restored["draft"], saved["draft"])
            self.assertEqual(restored["input"]["source_text"], TEXT)
            archive = store.export_snapshot(job["id"])
            self.assertEqual(archive["job"]["input"]["source_text"], TEXT)
            self.assertIsNone(archive["job"]["draft"]["segments"][0]["start"])


class TextHTTPTests(unittest.TestCase):
    def test_public_declaration_import_and_audio_attachment_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = make_server(root, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = f"http://127.0.0.1:{server.server_port}"
            token = json.loads(urlopen(address + "/api/session").read())["token"]
            def post(payload):
                request = Request(address + "/api/text", data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", "X-Review-Token": token})
                return json.loads(urlopen(request).read())
            try:
                body = {"text": TEXT, "filename": "pilot.txt", "hearing": HEARING}
                with self.assertRaises(HTTPError) as missing:
                    post(body)
                self.assertEqual(missing.exception.code, 400)
                missing.exception.close()
                self.assertEqual(server.store.list(), [])
                imported = post({**body, "source_scope": "public_hearing"})
                self.assertEqual(imported["processing"], "Complete")
                self.assertTrue(post({**body, "source_scope": "public_hearing"})["reused"])
                media = wav_bytes(2)
                asset = receive_audio(root, io.BytesIO(media), len(media), "test.wav")
                queued = server.store.enqueue_audio({**HEARING, "source_file": "test.wav",
                    "source_scope": "public_hearing"}, asset, False)
                attached = post({"attach_to": queued["id"], "text": TEXT, "filename": "pilot.txt"})
                self.assertEqual(attached["input"]["source_text"], TEXT)
                self.assertEqual(attached["hearing"]["source_file"], "test.wav")
                deadline = time.monotonic() + 5
                while server.store.get(queued["id"])["processing"] != "Complete" and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(server.store.get(queued["id"])["processing"], "Complete")
                before = server.store.get(queued["id"])
                with self.assertRaises(HTTPError) as existing:
                    post({"attach_to": queued["id"], "text": TEXT + "\nnew", "filename": "new.txt"})
                self.assertEqual(existing.exception.code, 409)
                existing.exception.close()
                self.assertEqual(server.store.get(queued["id"]), before)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
