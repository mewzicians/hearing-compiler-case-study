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
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import subprocess

from hearing_compiler.audio import receive_audio
from hearing_compiler.pipeline import InputError, extract, fields, validate
from hearing_compiler.providers import MacWhisperProvider
from hearing_compiler.server import make_server
from hearing_compiler.store import Store
from hearing_compiler.subtitle_import import normalize_subtitles
from audio_test_helpers import wav_bytes

HEARING = {"title": "Fictional subtitle pilot", "date": "2026-10-06",
           "committee": "Example Committee", "source_file": "pilot.mp3"}
SRT = ("\ufeff1\r\n00:00:02,125 --> 00:00:07,600\r\n"
       "I am Mira Vale with Fictional Notice Council.\r\nI support H.8137.\r\n\r\n"
       "2\r\n00:00:07,000 --> 00:00:12,000\r\n"
       "The notice must arrive twenty-one days before renewal.\r\n")
VTT = ("WEBVTT\n\nNOTE scripted fixture, not MacWhisper output\n\n"
       "intro\n00:02.125 --> 00:07.600 align:start\n"
       "<v Speaker 1>I support H.8137.</v>\n\n"
       "00:07.600 --> 00:12.000\nWho sends the notice?\n")


class SubtitleImportTests(unittest.TestCase):
    def document(self, text=SRT, name="pilot.srt"):
        return normalize_subtitles(text, HEARING, name, synthetic=True)

    def test_original_bom_crlf_multiline_identifier_and_clocks_survive(self):
        document = self.document()
        self.assertEqual(document["source_text"], SRT)
        self.assertEqual(document["source_text_sha256"], hashlib.sha256(SRT.encode()).hexdigest())
        self.assertEqual(document["segments"][0]["start"], 2.125)
        self.assertEqual(document["segments"][0]["end"], 7.6)
        self.assertEqual(document["segments"][0]["source_line"], 3)
        self.assertEqual(document["segments"][0]["source_cue_id"], "1")
        self.assertEqual(document, self.document())
        for segment in document["segments"]:
            self.assertEqual(SRT[segment["source_char_start"]:segment["source_char_end"]], segment["text"])
            self.assertTrue(segment["speaker_unavailable"])

    def test_vtt_cue_settings_metadata_and_voice_markup_stay_source_data(self):
        document = self.document(VTT, "pilot.vtt")
        self.assertEqual(len(document["segments"]), 2)
        self.assertEqual(document["segments"][0]["source_cue_id"], "intro")
        self.assertEqual(document["segments"][0]["start"], 2.125)
        self.assertIn("<v Speaker 1>", document["segments"][0]["text"])
        self.assertNotEqual(document["segments"][0]["speaker"], "Speaker 1")
        self.assertIsNone(document["segments"][1]["source_cue_id"])
        self.assertIn("NOTE scripted", document["source_text"])

    def test_source_corruptions_fail_their_original_cue_constraint(self):
        for change in ("clock", "text", "omission", "label", "hash", "line", "span"):
            document = self.document()
            if change == "clock":
                document["segments"][0]["start"] = 2.5
            elif change == "text":
                document["segments"][0]["text"] += " Invented."
            elif change == "omission":
                document["segments"].pop()
            elif change == "label":
                document["segments"][0]["speaker"] = "Speaker 1"
            elif change == "hash":
                document["source_text_sha256"] = "wrong"
            elif change == "line":
                document["segments"][0]["source_line"] += 1
            else:
                document["segments"][0]["source_char_start"] += 1
            with self.subTest(change=change), self.assertRaisesRegex(InputError, "original cue"):
                validate(document)

    def test_malformed_times_order_empty_cues_encoding_and_mapping_rejected(self):
        invalid = [
            ("", "srt"), ("1\n00:00:00,000 --> 00:00:01,000\n", "srt"),
            (SRT.replace("00:00:02,125", "00:61:02,125"), "srt"),
            (SRT.replace("00:00:07,600", "00:00:01,000"), "srt"),
            (SRT.replace("00:00:07,000", "00:00:01,000"), "srt"),
            (SRT.replace("00:00:02,125", "2.125"), "srt"),
            (SRT.replace("00:00:07,600", "00:00:07,600 align:start"), "srt"),
            ("1\n00:00:00,000 --> 00:00:01,000\n\x00", "srt"),
            ("1\n00:00:00,000 --> 00:00:01,000\n\ud800", "srt"),
            (VTT.replace("WEBVTT\n", "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00.000,MPEGTS:900000\n"), "vtt"),
            (VTT.replace("WEBVTT", "NOT VTT", 1), "vtt"),
        ]
        for text, kind in invalid:
            with self.subTest(text=repr(text[:60])), self.assertRaises(InputError):
                self.document(text, "pilot." + kind)

    def test_blank_and_zero_length_whisper_cues_keep_the_rest_of_the_transcript(self):
        text = ("1\n00:00:01,000 --> 00:00:02,000\n\n"
                "2\n00:00:02,000 --> 00:00:02,000\nI support H.8137.\n\n"
                "3\n00:00:03,000 --> 00:00:05,000\nWho sends the notice?\n")
        document = self.document(text)
        self.assertEqual([(s["source_cue_id"], s["start"], s["end"]) for s in document["segments"]],
                         [("2", 2, 2), ("3", 3, 5)])
        self.assertEqual(document["source_text"], text)
        self.assertEqual(validate(document), document)

    def test_missing_separator_cannot_hide_a_second_clock_in_the_first_cue(self):
        malformed = SRT.replace("\r\n\r\n2\r\n", "\r\n2\r\n")
        with self.assertRaisesRegex(InputError, "separator"):
            self.document(malformed)

    def test_overlaps_are_retained_for_review_without_merging_speakers(self):
        draft = extract(self.document())
        self.assertTrue(any("Overlapping" in issue["value"] for issue in draft["issues"]))
        self.assertEqual(draft["source_text"], SRT)
        self.assertFalse(any("Plain text has no" in issue["value"] for issue in draft["issues"]))
        self.assertTrue(all(item["review"] in ("NEEDS_REVIEW", "UNRESOLVED") for _, item in fields(draft)))

    def test_restart_and_exports_keep_original_and_unapproved_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            result = store.import_one(self.document())
            self.assertEqual(result["processing"], "Complete")
            snapshot = Store(Path(folder)).export_snapshot(result["id"])
            self.assertEqual(snapshot["job"]["input"]["source_text"], SRT)
            self.assertEqual(snapshot["job"]["draft"]["source_text"], SRT)
            self.assertNotEqual(snapshot["job"]["review"]["status"], "APPROVED")

    def test_free_provider_requests_srt_without_speaker_model_and_validates_actual_return(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "fictional.wav"
            source.write_bytes(wav_bytes(13))
            provider = MacWhisperProvider("whisper-cpp:ggml-tiny.en", output_format="srt")
            self.assertEqual(provider.command(source)[3:],
                ["--no-speakers", "--format", "srt", "--model", "whisper-cpp:ggml-tiny.en"])
            with patch("hearing_compiler.providers.sys.platform", "darwin"), patch(
                "hearing_compiler.providers.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, SRT, "")):
                document = provider.transcribe(source, HEARING)
            self.assertFalse(document["synthetic"])
            self.assertEqual(document["source_format"], "srt-utf8")
            with patch("hearing_compiler.providers.sys.platform", "darwin"), patch(
                "hearing_compiler.providers.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "unexpected flattened text", "")):
                with self.assertRaises(InputError):
                    provider.transcribe(source, HEARING)
            with self.assertRaises(InputError):
                MacWhisperProvider("whisper-cpp:ggml-tiny.en", output_format="unsupported")

    def test_settings_format_survives_restart_without_rewriting_old_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            store.set_model("whisper-cpp:ggml-tiny.en", "srt")
            self.assertEqual(Store(Path(folder)).settings()["output_format"], "srt")
            old = {"model_id": "whisperkit:old"}
            store.data["transcription"] = copy.deepcopy(old)
            store._save()
            self.assertEqual(Store(Path(folder)).settings(), old)


class SubtitleHTTPTests(unittest.TestCase):
    def test_invalid_source_isolated_valid_attachment_uses_original_audio_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = make_server(root, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            token = json.loads(urlopen(base + "/api/session").read())["token"]
            def post(body):
                request = Request(base + "/api/text", data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json", "X-Review-Token": token})
                return json.loads(urlopen(request).read())
            try:
                with self.assertRaises(HTTPError) as rejected:
                    post({"text": "malformed", "filename": "bad.srt", "hearing": HEARING,
                          "source_scope": "public_hearing"})
                self.assertEqual(rejected.exception.code, 400)
                rejected.exception.close()
                self.assertEqual(server.store.list(), [])
                audio = wav_bytes(13)
                asset = receive_audio(root, io.BytesIO(audio), len(audio), "fictional.wav")
                queued = server.store.enqueue_audio({**HEARING, "source_scope": "public_hearing"}, asset, False)
                post({"attach_to": queued["id"], "text": SRT, "filename": "pilot.srt"})
                deadline = time.monotonic() + 5
                while server.store.get(queued["id"])["processing"] != "Complete" and time.monotonic() < deadline:
                    time.sleep(.02)
                saved = server.store.get(queued["id"])
                self.assertEqual(saved["processing"], "Complete")
                self.assertEqual(saved["input"]["source_text"], SRT)
                self.assertEqual(saved["draft"]["segments"][0]["start"], 2.125)
                with self.assertRaises(HTTPError) as existing:
                    post({"attach_to": queued["id"], "text": SRT, "filename": "pilot.srt"})
                self.assertEqual(existing.exception.code, 409)
                existing.exception.close()
                self.assertEqual(server.store.get(queued["id"]), saved)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
