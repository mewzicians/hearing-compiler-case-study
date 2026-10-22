import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from audio_test_helpers import export, metadata, wav_bytes
from hearing_compiler import audio as audio_module
from hearing_compiler.audio import AudioQueue, KeepAwake, audio_path, byte_range, receive_audio
from hearing_compiler.pipeline import InputError, extract
from hearing_compiler.providers import normalize_macwhisper
from hearing_compiler.server import make_server
from hearing_compiler.store import Conflict, Store
from test_pipeline import fixture


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Background work did not reach the expected state.")


class AudioQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.store = Store(self.folder)
        self.queues = []

    def tearDown(self):
        for queue in self.queues:
            queue.close()
        self.temporary.cleanup()

    def queue(self, provider=None):
        queue = AudioQueue(self.store, provider)
        self.queues.append(queue)
        return queue

    def upload(self, name="hearing.wav", audio=None, ready=False):
        raw = audio or wav_bytes(2)
        asset = receive_audio(self.folder, io.BytesIO(raw), len(raw), name)
        return self.store.enqueue_audio(metadata(name), asset, ready)

    def test_managed_copy_duplicate_reuse_and_source_path(self):
        raw = wav_bytes(2)
        first = self.upload(audio=raw)
        second = self.upload(name="renamed.wav", audio=raw)
        self.assertTrue(second["reused"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["processing"], "Awaiting transcript")
        target, asset = audio_path(self.store, first["id"])
        self.assertEqual(target.read_bytes(), raw)
        self.assertTrue(target.is_relative_to(self.folder / "audio"))
        self.assertEqual(asset["bytes"], len(raw))

    def test_reupload_restores_a_corrupted_managed_copy(self):
        raw = wav_bytes(2)
        item = self.upload(audio=raw)
        target, _ = audio_path(self.store, item["id"])
        target.write_bytes(b"x" * len(raw))
        receive_audio(self.folder, io.BytesIO(raw), len(raw), "hearing.wav")
        self.assertEqual(target.read_bytes(), raw)

    def test_invalid_and_interrupted_audio_leave_no_partial_files(self):
        with self.assertRaises(InputError):
            receive_audio(self.folder, io.BytesIO(b"not audio"), 9, "fake.mp3")
        with self.assertRaises(InputError):
            receive_audio(self.folder, io.BytesIO(b"RIFF"), 100, "short.wav")
        self.assertEqual(list((self.folder / "audio").glob("*")), [])

    def test_single_and_suffix_byte_ranges_and_bounds(self):
        self.assertEqual(byte_range(None, 100), (0, 99, False))
        self.assertEqual(byte_range("bytes=10-19", 100), (10, 19, True))
        self.assertEqual(byte_range("bytes=90-", 100), (90, 99, True))
        self.assertEqual(byte_range("bytes=-5", 100), (95, 99, True))
        for header in ("bytes=100-", "bytes=20-10", "bytes=0-1,5-8", "bytes=-0", "invalid"):
            with self.assertRaises(InputError):
                byte_range(header, 100)

    def test_imported_transcript_finishes_without_transcription_and_preserves_audio(self):
        item = self.upload()
        document = normalize_macwhisper(export(), self.store.get(item["id"])["hearing"], False)
        self.store.cache_transcript(item["id"], document)
        self.queue()
        eventually(lambda: self.store.get(item["id"])["processing"] == "Complete")
        before = self.store.get(item["id"])
        self.assertEqual(before["draft"]["questions"][0]["question"]["evidence"], ["mw03"])
        self.assertEqual(before["draft"]["segments"][2]["start"], 15)
        self.assertTrue(before["audio_url"])
        self.store.review(item["id"], before["revision"], [
            {"path": "/questions/0/asker", "value": "Rep. Avery", "review": "APPROVED"},
        ])
        restarted = Store(self.folder)
        self.assertEqual(restarted.get(item["id"])["draft"]["questions"][0]["asker"]["value"], "Rep. Avery")
        self.assertTrue(restarted.enqueue_audio(metadata(), before["audio"], False)["reused"])

    def test_transcription_failure_is_isolated_and_failed_stage_retry_uses_cache(self):
        calls = []

        class Provider:
            def transcribe(_, source, hearing):
                calls.append(hearing["id"])
                if len(calls) == 1:
                    raise InputError("deliberate transcription failure")
                return normalize_macwhisper(export(), hearing, False)

        first = self.upload(ready=True)
        second = self.upload("other.wav", wav_bytes(3), ready=True)
        queue = self.queue(Provider())
        eventually(lambda: self.store.get(second["id"])["processing"] == "Complete")
        self.assertEqual(self.store.get(first["id"])["failed_stage"], "transcription")
        queue.retry(first["id"])
        eventually(lambda: self.store.get(first["id"])["processing"] == "Complete")
        self.assertEqual(calls.count(second["id"]), 1)
        with self.assertRaises(Conflict):
            queue.retry(second["id"])

    def test_the_queue_hands_drafting_the_recording_but_never_saves_its_path(self):
        seen = []

        def extractor(document):
            seen.append(document.get("recording_path"))
            return extract({k: v for k, v in document.items() if k != "recording_path"})
        self.store.extractor = extractor
        item = self.upload()
        self.store.cache_transcript(item["id"], normalize_macwhisper(export(), self.store.get(item["id"])["hearing"], False))
        self.queue()
        eventually(lambda: self.store.get(item["id"])["processing"] == "Complete")
        target, _ = audio_path(self.store, item["id"])
        self.assertEqual([Path(p).resolve() for p in seen], [target])
        self.assertNotIn("recording_path", self.store.get(item["id"])["input"])
        self.assertNotIn("recording_path", self.store.path.read_text(encoding="utf-8"))

    def test_the_mac_is_kept_awake_only_while_hearings_are_processing(self):
        started = []

        class Process:
            def __init__(self, command, **_):
                started.append(command)
                self.stopped = False

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                return 0
        awake = KeepAwake(popen=Process)
        awake.tool = "/usr/bin/caffeinate"
        awake.hold(True)
        awake.hold(True)  # Already held: no second caffeinate.
        process = awake.process
        awake.hold(False)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][:3], ["/usr/bin/caffeinate", "-i", "-w"])  # Ends with the app's process.
        self.assertTrue(process.stopped)
        self.assertIsNone(awake.process)
        awake.tool = None  # Not a Mac: nothing happens.
        awake.hold(True)
        self.assertIsNone(awake.process)

        holds = []

        class Recorder:
            def hold(self, busy):
                holds.append(busy)
        item = self.upload()
        self.store.cache_transcript(item["id"], normalize_macwhisper(export(), self.store.get(item["id"])["hearing"], False))
        with patch.object(audio_module, "KeepAwake", Recorder):
            self.queue()
            eventually(lambda: self.store.get(item["id"])["processing"] == "Complete" and holds and not holds[-1])
        self.assertIn(True, holds)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("caffeinate"), "macOS only; CI runs it on a Mac")
    def test_caffeinate_really_starts_and_stops_on_a_mac(self):
        awake = KeepAwake()
        awake.hold(True)
        process = awake.process
        time.sleep(0.5)
        self.assertIsNone(process.poll())  # Running: the Mac won't idle-sleep.
        awake.hold(False)
        self.assertIsNotNone(process.poll())

    def test_extraction_retry_does_not_retranscribe(self):
        calls = []
        fail = [True]

        class Provider:
            def transcribe(_, source, hearing):
                calls.append(hearing["id"])
                return normalize_macwhisper(export(), hearing, False)

        def extractor(document):
            if fail[0]:
                raise InputError("deliberate extraction failure")
            return extract(document)

        self.store.extractor = extractor
        item = self.upload(ready=True)
        queue = self.queue(Provider())
        eventually(lambda: self.store.get(item["id"])["processing"] == "Failed")
        self.assertEqual(self.store.get(item["id"])["failed_stage"], "extraction")
        self.assertIsNotNone(self.store.get(item["id"])["input"])
        fail[0] = False
        queue.retry(item["id"])
        eventually(lambda: self.store.get(item["id"])["processing"] == "Complete")
        self.assertEqual(calls, [item["id"]])

    def test_blank_and_zero_length_whisper_segments_do_not_reject_the_transcript(self):
        raw = export()
        raw["segments"][1]["text"] = "  "
        raw["segments"][2]["end"] = raw["segments"][2]["start"]
        document = normalize_macwhisper(raw, metadata(), False)
        self.assertNotIn("mw02", [s["id"] for s in document["segments"]])
        self.assertEqual(document["segments"][1]["start"], document["segments"][1]["end"])
        self.assertEqual(document["source_export"], raw)
        raw["segments"][2]["end"] = raw["segments"][2]["start"] - 1
        with self.assertRaises(InputError):
            normalize_macwhisper(raw, metadata(), False)

    def test_unreadable_macwhisper_output_is_saved_and_retry_does_not_retranscribe(self):
        calls = []
        unreadable = export()
        unreadable["segments"][2]["end"] = unreadable["segments"][2]["start"] - 1

        class Provider:
            def run(_, source):
                calls.append(source.name)
                return json.dumps(unreadable if len(calls) == 1 else export())

        item = self.upload(ready=True)
        queue = self.queue(Provider())
        eventually(lambda: self.store.get(item["id"])["processing"] == "Failed")
        failed = self.store.get(item["id"])
        self.assertEqual(failed["failed_stage"], "transcription")
        self.assertIn("Retry rereads the saved output", failed["error"])
        saved = self.folder / failed["transcription_output"]["path"]
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), unreadable)
        queue.retry(item["id"])
        eventually(lambda: self.store.get(item["id"])["processing"] == "Failed")
        self.assertEqual(len(calls), 1)
        # Choosing another model is the explicit route to transcribe again.
        self.store.set_model("whisperkit:other", "json")
        queue.retry(item["id"])
        eventually(lambda: self.store.get(item["id"])["processing"] == "Complete")
        self.assertEqual(len(calls), 2)
        self.assertEqual(Store(self.folder).get(item["id"])["processing"], "Complete")

    def test_awaiting_and_waiting_jobs_survive_restart(self):
        awaiting = self.upload()
        waiting = self.upload("queued.wav", wav_bytes(3), ready=True)
        restarted = Store(self.folder)
        self.assertEqual(restarted.get(awaiting["id"])["processing"], "Awaiting transcript")
        self.assertEqual(restarted.get(waiting["id"])["processing"], "Waiting")

    def test_failed_claim_save_leaves_job_retryable_in_waiting_state(self):
        item = self.upload(ready=True)
        with patch("hearing_compiler.store.os.replace", side_effect=PermissionError("storage unavailable")):
            with self.assertRaises(PermissionError):
                self.store.claim_audio(True)
        self.assertEqual(self.store.get(item["id"])["processing"], "Waiting")

    def test_connection_checks_only_installed_local_models_and_does_not_need_credentials(self):
        queue = self.queue()
        with patch("hearing_compiler.audio.sys.platform", "darwin"), patch(
            "hearing_compiler.audio.shutil.which", return_value="/usr/local/bin/mw"
        ), patch("hearing_compiler.audio.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, "MacWhisper 14.4.1", ""),
            subprocess.CompletedProcess([], 0, "whisperkit:local-one Local\nhosted:remote Remote\napple:en-US English", ""),
        ]) as runner:
            result = queue.models()
            self.assertEqual(result["models"], ["whisperkit:local-one", "apple:en-US"])
            self.assertEqual(runner.call_args_list[0].args[0], ["/usr/local/bin/mw", "version"])
            self.assertEqual(runner.call_args_list[1].args[0], ["/usr/local/bin/mw", "models", "list"])
        with patch.object(queue, "models", return_value=result):
            queue.configure("whisperkit:local-one")
        self.assertEqual(self.store.settings()["model_id"], "whisperkit:local-one")
        with patch.object(queue, "models", return_value=result):
            with self.assertRaises(InputError):
                queue.configure("hosted:remote")

    def test_source_attachment_keeps_reviewed_fields_and_original_segments(self):
        self.store.import_one(fixture("sample-hearing"))
        reviewed = self.store.review("sample-hearing", 0, [
            {"path": "/questions/0/asker", "value": "Rep. Avery", "review": "APPROVED"},
        ])
        raw = wav_bytes(2)
        asset = receive_audio(self.folder, io.BytesIO(raw), len(raw), "source.wav")
        self.store.attach_audio("sample-hearing", asset)
        after = self.store.get("sample-hearing")
        self.assertEqual(after["draft"]["testimonies"], reviewed["draft"]["testimonies"])
        self.assertEqual(after["draft"]["questions"], reviewed["draft"]["questions"])
        self.assertEqual(after["draft"]["segments"], reviewed["draft"]["segments"])

    def test_legacy_demo_archive_removes_only_known_demo_and_preserves_real_input(self):
        self.store.import_one(fixture("sample-hearing"))
        real = fixture("no-questions")
        real["hearing"]["id"] = "real-import"
        real["synthetic"] = False
        real["source_scope"] = "public_hearing"
        self.store.import_one(real)
        before = copy.deepcopy(self.store.data)
        self.store.archive_legacy_samples()
        self.assertEqual([job["id"] for job in self.store.list()], ["real-import"])
        backups = list((self.folder / "backups").glob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), before)


class AudioHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.temporary.name), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = json.loads(urlopen(self.base + "/api/session").read())["token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def upload(self, raw, use_token=True):
        request = Request(self.base + "/api/audio/upload?metadata=" + quote(json.dumps(metadata())), data=raw,
                          headers={"Content-Type": "application/octet-stream",
                                   **({"X-Review-Token": self.token} if use_token else {})})
        return json.loads(urlopen(request).read())

    def test_empty_workspace_upload_media_ranges_and_head(self):
        self.assertEqual(json.loads(urlopen(self.base + "/api/hearings").read()), [])
        raw = wav_bytes(2)
        item = self.upload(raw)
        path = self.base + "/api/audio/" + item["id"]
        with urlopen(Request(path, headers={"Range": "bytes=100-199"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.read(), raw[100:200])
        with urlopen(Request(path, method="HEAD")) as response:
            self.assertEqual(int(response.headers["Content-Length"]), len(raw))
            self.assertEqual(response.read(), b"")
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(path, headers={"Range": f"bytes={len(raw)}-"}))
        self.assertEqual(error.exception.code, 416)

    def test_audio_upload_cannot_bypass_session_check(self):
        with self.assertRaises(HTTPError) as error:
            self.upload(wav_bytes(2), use_token=False)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.server.store.list(), [])

    def test_transcript_attachment_reaches_draft_without_a_mac(self):
        item = self.upload(wav_bytes(30))
        request = Request(self.base + "/api/audio/transcript",
                          data=json.dumps({"id": item["id"], "export": export()}).encode(),
                          headers={"Content-Type": "application/json", "X-Review-Token": self.token})
        urlopen(request).close()
        eventually(lambda: self.server.store.get(item["id"])["processing"] == "Complete")
        result = json.loads(urlopen(self.base + "/api/hearings/" + item["id"]).read())
        self.assertIn("Testifiers:", result["markdown"])
        self.assertEqual(result["draft"]["segments"][2]["start"], 15)


if __name__ == "__main__":
    unittest.main()
