"""The hearing page link reaches Gemini on every intake route, and a failed transcription takes a transcript."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from audio_test_helpers import export, metadata, wav_bytes
from hearing_compiler import agenda
from hearing_compiler.audio import AudioQueue, receive_audio
from hearing_compiler.extraction import ExtractionService
from hearing_compiler.pipeline import InputError
from hearing_compiler.saved_state import validate_saved_state
from hearing_compiler.server import make_server
from hearing_compiler.store import Store
from test_audio import eventually
from test_gemini_extraction import KEY, Client, Keys

LINK = "https://malegislature.gov/Events/Hearings/Detail/1234"
BILLS = [{"bill": "H.1234", "title": "An Act about legal help", "sponsor": "A. Sponsor"}]
LINES = ("I'm Alex Morgan with Example Legal Services. I support H.1234.",
         "The proposal would expand access to legal assistance. It would make eligibility easier to understand.",
         "Who would qualify for assistance?", "Residents who meet the bill's income criteria.")
SRT = "".join(f"{n}\n00:00:{10 * n},000 --> 00:00:{10 * n + 9},000\n{line}\n\n" for n, line in enumerate(LINES, 1))
TEXT = "\n".join(LINES) + "\n"


class Agenda:
    """A fake agenda.fetch: no network, and a record of every page asked for."""

    def __init__(self):
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return {"url": url, "bills": BILLS}


class Gemini(Client):
    """The fake Gemini client; set hold to keep a hearing in Analyzing."""

    hold = None

    def call_gemini(self, *args, **kwargs):
        if self.hold is not None:
            self.hold.wait(10)
        return super().call_gemini(*args, **kwargs)


class Mw:
    """Stands in for MacWhisperProvider: run() returns raw mw output, so the queue takes the real mw branch."""

    def __init__(self):
        self.plan, self.calls = [], 0

    def run(self, source):
        self.calls += 1
        return self.plan.pop(0)() if self.plan else SRT


def refused():
    raise InputError("MacWhisper failed: Error: The command-line tool requires MacWhisper Pro.")


def drafted_with_agenda(test, job, client):
    test.assertEqual(job["processing"], "Complete", job.get("error"))
    test.assertEqual(job["draft"]["extraction_provenance"]["agenda_url"], LINK)
    test.assertEqual(job["draft"]["units"][0]["bill_title"]["value"], "An Act about legal help")
    test.assertIn("Official agenda:\nH.1234 | An Act about legal help", client.prompts[-1])


class RecordingLinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.agenda = Agenda()
        fetch = patch.object(agenda, "fetch", self.agenda)
        fetch.start()
        self.addCleanup(fetch.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def test_recording_link_reaches_the_first_mw_draft_after_restart_and_retry(self):
        store = Store(self.folder)
        store.set_model("whisper-cpp:ggml-base.en", "srt")  # As if "Use this model" was chosen with SRT.
        raw = wav_bytes(2)
        asset = receive_audio(store.directory, io.BytesIO(raw), len(raw), "hearing.wav")
        job_id = store.enqueue_audio({**metadata(), "hearing_url": LINK}, asset, True)["id"]
        store = Store(self.folder)  # The app restarts while the recording waits.
        client = Gemini()
        store.extractor = ExtractionService(store, threading.Event(), Keys(), client)
        store.extractor.configure("gemini", api_key=KEY)
        mw = Mw()
        mw.plan = [refused]
        queue = AudioQueue(store, mw)
        store.on_queued = queue.wake.set
        try:
            eventually(lambda: store.get(job_id)["processing"] == "Failed", timeout=10)
            self.assertEqual(store.get(job_id)["failed_stage"], "transcription")
            queue.retry(job_id)
            eventually(lambda: store.get(job_id)["processing"] == "Complete", timeout=10)
            self.assertEqual((mw.calls, len(client.prompts), self.agenda.urls), (2, 1, [LINK]))
            drafted_with_agenda(self, store.get(job_id), client)
            # Redrafting an untouched draft reads the saved input, link included.
            store.redraft(job_id)
            eventually(lambda: store.get(job_id)["processing"] == "Complete", timeout=10)
            self.assertEqual(self.agenda.urls, [LINK, LINK])
        finally:
            queue.close()
        drafted_with_agenda(self, Store(self.folder).get(job_id), client)


class HttpLinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.agenda, self.client, self.mw = Agenda(), Gemini(), Mw()
        fetch = patch.object(agenda, "fetch", self.agenda)
        fetch.start()
        self.addCleanup(fetch.stop)
        self.server = make_server(self.state, 0, transcription_provider=self.mw)
        store = self.server.store
        store.extractor = ExtractionService(store, self.server.audio_queue.stop_event, Keys(), self.client)
        store.extractor.configure("gemini", api_key=KEY)
        store.set_model("whisper-cpp:ggml-base.en", "srt")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = json.loads(urlopen(self.base + "/api/session").read())["token"]
        self.running = True

    def tearDown(self):
        self.stop()
        self.temporary.cleanup()

    def stop(self):
        if self.running:
            self.running = False
            if self.client.hold is not None:
                self.client.hold.set()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(10)

    def post(self, path, body):
        request = Request(self.base + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "X-Review-Token": self.token})
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def upload(self, raw, **extra):
        query = quote(json.dumps({**metadata(), **extra}))
        request = Request(f"{self.base}/api/audio/upload?metadata={query}", data=raw,
                          headers={"Content-Type": "application/octet-stream", "X-Review-Token": self.token})
        return json.loads(urlopen(request).read())

    def processing(self, job_id):
        return self.server.store.get(job_id)["processing"]

    def attach_attempts(self, job_id):
        return [self.post("/api/text", {"attach_to": job_id, "text": SRT, "filename": "again.srt"})[0],
                self.post("/api/audio/transcript", {"id": job_id, "export": export()})[0]]

    def test_imported_subtitles_and_text_draft_with_the_hearing_link(self):
        ids = []
        for filename, text in (("hearing.srt", SRT), ("hearing.txt", TEXT)):
            hearing = {**metadata(filename),
                       "hearing_url": " https://www.malegislature.gov/Events/Hearings/Detail/1234/ "}
            del hearing["source_scope"]  # Sent the way the import dialog sends it.
            status, job = self.post("/api/text", {"text": text, "filename": filename, "source_scope": "public_hearing",
                                                  "hearing": hearing})
            self.assertEqual(status, 200, job)
            eventually(lambda: self.processing(job["id"]) == "Complete", timeout=10)
            saved = self.server.store.get(job["id"])
            self.assertEqual(saved["input"].get("hearing_url"), LINK)
            drafted_with_agenda(self, saved, self.client)
            ids.append(job["id"])
        self.assertEqual(self.agenda.urls, [LINK, LINK])
        self.stop()
        restarted = Store(self.state)  # Saved work still reloads, with nothing set aside.
        validate_saved_state(json.loads(restarted.path.read_text(encoding="utf-8")))
        self.assertEqual(restarted.data.get("set_aside", []), [])
        for job_id in ids:
            self.assertEqual(restarted.get(job_id)["input"]["hearing_url"], LINK)
            self.assertEqual(restarted.get(job_id)["processing"], "Complete")

    def test_a_failed_transcription_takes_an_attached_transcript_then_refuses_once_busy_or_finished(self):
        self.mw.plan = [refused]
        job_id = self.upload(wav_bytes(2), hearing_url=LINK)["id"]
        eventually(lambda: self.processing(job_id) == "Failed", timeout=10)
        self.assertEqual(self.server.store.get(job_id)["failed_stage"], "transcription")
        self.client.hold = threading.Event()
        status, attached = self.post("/api/text", {"attach_to": job_id, "text": SRT, "filename": "hearing.srt"})
        self.assertEqual((status, attached["input"]["source_text"]), (200, SRT))
        eventually(lambda: self.processing(job_id) == "Analyzing", timeout=10)
        self.assertEqual(self.attach_attempts(job_id), [409, 409])
        self.client.hold.set()
        eventually(lambda: self.processing(job_id) == "Complete", timeout=10)
        done = self.server.store.get(job_id)
        self.assertEqual(self.mw.calls, 1)  # The attached transcript is drafted without running mw again.
        drafted_with_agenda(self, done, self.client)
        self.assertEqual(self.attach_attempts(job_id), [409, 409])
        self.assertEqual(self.server.store.get(job_id), done)

    def test_attaching_waits_while_mw_runs_and_works_after_unreadable_output(self):
        hold = threading.Event()

        def slow():
            hold.wait(10)
            return "MacWhisper printed something that is not subtitles"
        self.mw.plan = [slow]
        job_id = self.upload(wav_bytes(3))["id"]
        try:
            eventually(lambda: self.processing(job_id) == "Transcribing", timeout=10)
            self.assertEqual(self.attach_attempts(job_id), [409, 409])
        finally:
            hold.set()
        eventually(lambda: self.processing(job_id) == "Failed", timeout=10)
        self.assertIn("could not be read", self.server.store.get(job_id)["error"])
        status, attached = self.post("/api/audio/transcript", {"id": job_id, "export": export()})
        self.assertEqual((status, attached["input"]["source_export"]), (200, export()))
        eventually(lambda: self.processing(job_id) == "Complete", timeout=10)
        self.assertEqual(self.mw.calls, 1)


if __name__ == "__main__":
    unittest.main()
