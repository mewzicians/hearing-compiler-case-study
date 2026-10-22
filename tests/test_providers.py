import copy
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hearing_compiler.pipeline import InputError, extract
from hearing_compiler.providers import MacWhisperProvider, MockProvider, normalize_macwhisper
from test_pipeline import ROOT


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.export = json.loads((ROOT / "fixtures/macwhisper/sample-export.json").read_text())
        self.metadata = json.loads((ROOT / "fixtures/macwhisper/sample-metadata.json").read_text())

    def test_documented_millisecond_shape_normalizes_without_guessing(self):
        original = copy.deepcopy(self.export)
        document = normalize_macwhisper(self.export, self.metadata)
        self.assertEqual(document["segments"][0]["start"], 4994)
        self.assertEqual(document["segments"][2]["start"], 5052)
        self.assertEqual(document["segments"][0]["speaker"], "Speaker 2")
        self.assertEqual(document["segments"][0]["source_word_timings_ms"][0]["start"], 4995000)
        self.assertEqual(document["source_export"], original)
        self.assertEqual(self.export, original)
        draft = extract(document)
        self.assertIsNone(draft["questions"][0]["asker"]["value"])
        self.assertEqual(draft["questions"][0]["response"]["evidence"], ["mw04"])

    def test_missing_speakers_do_not_get_merged_into_one_person(self):
        for segment in self.export["segments"]:
            segment.pop("speaker")
        document = normalize_macwhisper(self.export, self.metadata)
        self.assertEqual(len({s["speaker"] for s in document["segments"]}), 4)
        draft = extract(document)
        self.assertTrue(any("Speaker labels were missing" in i["value"] for i in draft["issues"]))
        self.assertIsNone(draft["questions"][0]["response"]["value"])

    def test_mock_and_macwhisper_feed_same_extraction_boundary(self):
        mock = MockProvider().transcribe(ROOT / "fixtures/transcripts/sample-hearing.json")
        normalized = normalize_macwhisper(self.export, self.metadata)
        for document in (mock, normalized):
            self.assertEqual(extract(document)["testimonies"][0]["statements"][0]["position"]["value"], "Support")

    def test_local_model_required_and_command_is_a_list(self):
        with self.assertRaises(InputError):
            MacWhisperProvider("hosted-model")
        command = MacWhisperProvider("whisperkit:example").command(Path("public hearing.mp3"))
        self.assertEqual(command[0:2], ["mw", "transcribe"])
        self.assertEqual(command[3:], ["--speakers", "--format", "json", "--model", "whisperkit:example"])

    def test_mac_invocation_and_error_are_audited_with_subprocess_stub(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "synthetic-audio-placeholder.mp3"
            source.write_bytes(b"")
            provider = MacWhisperProvider("whisperkit:example")
            with patch("hearing_compiler.providers.sys.platform", "darwin"), patch(
                "hearing_compiler.providers.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(self.export), ""),
            ) as runner:
                document = provider.transcribe(source, self.metadata)
                self.assertFalse(document["synthetic"])
                self.assertEqual(document["source_scope"], "public_hearing")
                runner.assert_called_once()
            with patch("hearing_compiler.providers.sys.platform", "darwin"), patch(
                "hearing_compiler.providers.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "speaker detection unavailable"),
            ):
                with self.assertRaisesRegex(InputError, "speaker detection unavailable"):
                    provider.transcribe(source, self.metadata)
            with patch("hearing_compiler.providers.sys.platform", "win32"):
                with self.assertRaisesRegex(InputError, "macOS"):
                    provider.transcribe(source, self.metadata)

    def test_bad_exports_and_units_are_rejected(self):
        with self.assertRaises(InputError):
            normalize_macwhisper({"text": "flattened transcript"}, self.metadata)
        self.export["segments"][0]["start"] = "01:23:14"
        with self.assertRaisesRegex(InputError, "milliseconds"):
            normalize_macwhisper(self.export, self.metadata)

    def test_queue_stop_cancels_mw_instead_of_waiting_for_full_transcription(self):
        cancelled = threading.Event()
        cancelled.set()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "public.mp3"
            source.write_bytes(b"")
            provider = MacWhisperProvider("whisperkit:local", cancel_event=cancelled)
            with patch("hearing_compiler.providers.sys.platform", "darwin"), patch(
                "hearing_compiler.providers.subprocess.Popen"
            ) as create_process:
                process = create_process.return_value
                process.poll.return_value = None
                process.communicate.return_value = ("", "")
                with self.assertRaisesRegex(InputError, "interrupted"):
                    provider.transcribe(source, self.metadata)
                process.send_signal.assert_called_once()
                process.communicate.assert_called_once_with(timeout=2)


if __name__ == "__main__":
    unittest.main()
