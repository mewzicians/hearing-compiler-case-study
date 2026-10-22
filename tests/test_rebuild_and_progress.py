"""Rebuilding an unreviewed Gemini draft from its saved reply, and the progress timer's data."""
from __future__ import annotations

from datetime import datetime, timedelta
import io
import itertools
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from audio_test_helpers import metadata, wav_bytes
from hearing_compiler import agenda, extraction, gemini, parts
from hearing_compiler.audio import AudioQueue, receive_audio
from hearing_compiler.pipeline import InputError, extract
from hearing_compiler.saved_state import validate_saved_state
from hearing_compiler.server import make_server
from hearing_compiler.store import REBUILD_MISS, Conflict, Store, set_stage
from hearing_compiler.workspace_backup import create_backup, restore_backup, verify_backup
from test_audio import eventually
from test_gemini_extraction import KEY, Client, Keys
from test_hearing_link_routes import LINK, Agenda, Gemini, Mw
from test_pipeline import fixture

MODEL = "gemini-3.8-flash"
JOB = "sample-hearing"


def older(store, job_id):
    """Makes a draft look as if an older build drafted it, like her Part 2, so Rebuild is offered."""
    with store.lock:
        store.data["jobs"][job_id]["draft"]["extraction_provenance"].pop("drafting", None)
        store._save()


class HeldAgenda(Agenda):
    """The fake agenda page; set bills to model a changed page, hold to pause a fetch, failure to fail it."""

    bills, hold, failure = None, None, None

    def __call__(self, url):
        if self.hold is not None:
            self.hold.wait(10)
        if self.failure:
            raise InputError(self.failure)
        loaded = super().__call__(url)
        return {**loaded, "bills": self.bills} if self.bills else loaded


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.agenda = HeldAgenda()
        fetch = patch.object(agenda, "fetch", self.agenda)
        fetch.start()
        self.addCleanup(fetch.stop)
        self.store = Store(self.folder)
        self.client = Client()
        self.store.extractor = lambda document: extraction.gemini_draft(
            document, KEY, MODEL, self.client, cache_dir=self.folder / "extraction-output")

    def tearDown(self):
        self.temporary.cleanup()

    def drafted(self):
        self.store.import_one({**fixture(JOB), "hearing_url": LINK})
        self.assertEqual(self.store.get(JOB)["processing"], "Complete")
        self.assertFalse(self.store.get(JOB)["can_rebuild"])  # This build's own drafts already have its fixes.
        older(self.store, JOB)
        return self.store.get(JOB)

    def unchanged(self, before, saved):
        self.assertEqual(self.store.get(JOB), before)
        self.assertEqual(self.store.path.read_bytes(), saved)
        self.assertEqual(len(self.client.prompts), 1)  # Never a new (paid) Gemini call.

    def test_rebuild_reuses_the_saved_reply_and_is_refused_once_reviewed(self):
        before = self.drafted()
        self.assertTrue(before["can_rebuild"])
        self.assertTrue(self.store.list()[0]["can_rebuild"])
        rebuilt = self.store.rebuild(JOB)
        self.assertEqual(len(self.client.prompts), 1)
        self.assertEqual(self.agenda.urls, [LINK])  # The draft kept its agenda, so the page isn't fetched again.
        provenance = rebuilt["draft"]["extraction_provenance"]
        self.assertEqual((provenance["reused_saved_reply"], provenance["agenda_url"]), (True, LINK))
        self.assertEqual(rebuilt["revision"], before["revision"] + 1)
        for key in ("id", "hearing", "input", "fingerprint"):
            self.assertEqual(rebuilt[key], before[key])
        self.assertFalse(rebuilt["can_rebuild"])  # Rebuilt with this build's fixes, so it isn't offered again.
        older(self.store, JOB)
        rebuilt = self.store.get(JOB)
        restarted = Store(self.folder)
        validate_saved_state(json.loads(restarted.path.read_text(encoding="utf-8")))
        self.assertEqual(restarted.get(JOB)["draft"]["extraction_provenance"]["reused_saved_reply"], True)
        reviewed = self.store.review(JOB, rebuilt["revision"], [{"path": "/units/0/bill", "review": "APPROVED"}])
        self.assertFalse(reviewed["can_rebuild"])
        saved = self.store.path.read_bytes()
        with self.assertRaisesRegex(Conflict, "already has your review work"):
            self.store.rebuild(JOB)
        self.assertEqual(self.store.path.read_bytes(), saved)

    def test_rebuild_runs_the_second_check_from_its_saved_replies(self):
        keys = Keys()
        keys.set(KEY)
        service = extraction.ExtractionService(self.store, threading.Event(), keys, self.client)

        def extractor(document):
            draft = extraction.gemini_draft(document, KEY, MODEL, self.client, cache_dir=self.folder / "extraction-output")
            return service.second_check(draft, KEY, MODEL)
        extractor.second_check = service.second_check
        self.store.extractor = extractor
        before = self.drafted()
        units = len(before["draft"]["units"])
        self.assertEqual((len(self.client.second), before["draft"]["second_check"]["units"]), (units, units))
        rebuilt = self.store.rebuild(JOB)
        self.assertEqual(rebuilt["draft"]["second_check"]["units"], units)
        self.assertEqual((len(self.client.prompts), len(self.client.second)), (1, units))  # Both reused: no new call.
        self.assertFalse(rebuilt["can_rebuild"])

    def test_a_check_that_failed_can_be_retried_by_rebuilding(self):
        keys = Keys()
        keys.set(KEY)
        service = extraction.ExtractionService(self.store, threading.Event(), keys, self.client)

        def busy(prompt):
            raise gemini.GeminiError("Gemini is busy.")
        self.client.check_reply = busy

        def extractor(document):
            draft = extraction.gemini_draft(document, KEY, MODEL, self.client, cache_dir=self.folder / "extraction-output")
            return service.second_check(draft, KEY, MODEL)
        extractor.second_check = service.second_check
        self.store.extractor = extractor
        self.store.import_one({**fixture(JOB), "hearing_url": LINK})
        first = self.store.get(JOB)
        self.assertTrue(first["draft"]["second_check"]["failed"])
        self.assertTrue(first["can_rebuild"])  # Built by this build, but its check didn't fully run.
        self.client.check_reply = {"items": [], "omissions": []}
        rebuilt = self.store.rebuild(JOB)
        self.assertEqual(rebuilt["draft"]["second_check"]["failed"], [])
        self.assertFalse(rebuilt["can_rebuild"])
        self.assertEqual(len(self.client.prompts), 1)  # The drafting reply was reused.

    def test_a_long_hearing_rebuilds_from_the_parts_it_was_drafted_in(self):
        from test_gemini_extraction import LongClient, long_hearing
        for name, value in (("MAX_WORDS", 300), ("PART_WORDS", 250), ("MIN_CUES", 4)):
            patcher = patch.object(gemini if name == "MAX_WORDS" else parts, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        document, spans = long_hearing()
        self.client = LongClient(spans)
        self.store.import_one({**document, "id": JOB})
        drafted = self.store.get(JOB)["draft"]
        self.assertEqual(len(drafted["extraction_provenance"]["parts"]), 2)
        older(self.store, JOB)
        with patch.object(parts, "split", lambda cues, count: [cues[:9], cues[9:]]):  # As if cutting changed.
            rebuilt = self.store.rebuild(JOB)
        self.assertEqual(len(self.client.prompts), 2)  # Each part's saved reply was found: no new call.
        self.assertEqual(rebuilt["draft"]["extraction_provenance"]["parts"][1]["first_cue"],
                         drafted["extraction_provenance"]["parts"][1]["first_cue"])

    def test_a_changed_page_cannot_block_a_draft_that_kept_its_agenda(self):
        self.drafted()
        self.agenda.bills = [{"bill": "H.1234", "title": "A retitled act", "sponsor": "A. Sponsor"}]
        self.agenda.failure = "Could not load the hearing page: timed out"
        rebuilt = self.store.rebuild(JOB)
        self.assertEqual((len(self.client.prompts), self.agenda.urls), (1, [LINK]))
        self.assertTrue(rebuilt["draft"]["extraction_provenance"]["reused_saved_reply"])

    def test_a_reply_that_cannot_be_matched_or_a_missing_agenda_changes_nothing(self):
        self.drafted()
        with self.store.lock:  # A draft from build 21 or earlier, which didn't keep its agenda.
            del self.store.data["jobs"][JOB]["draft"]["extraction_provenance"]["agenda"]
            self.store._save()
        before = self.store.get(JOB)
        saved = self.store.path.read_bytes()
        self.agenda.bills = [{"bill": "H.1234", "title": "A retitled act", "sponsor": "A. Sponsor"}]  # The page changed.
        with self.assertRaises(InputError) as raised:
            self.store.rebuild(JOB)
        self.assertEqual(str(raised.exception), REBUILD_MISS)
        self.unchanged(before, saved)
        self.agenda.bills, self.agenda.failure = None, "Could not load the hearing page: timed out"
        with self.assertRaisesRegex(InputError, r"needs the hearing page's agenda .*\(Could not load the hearing "
                                                r"page: timed out\)\. Nothing was changed"):
            self.store.rebuild(JOB)
        self.unchanged(before, saved)
        self.agenda.failure = None
        for reply in (self.folder / "extraction-output").glob("*.json"):
            reply.unlink()
        with self.assertRaisesRegex(InputError, "^" + REBUILD_MISS):
            self.store.rebuild(JOB)
        self.unchanged(before, saved)
        self.assertTrue(self.store.get(JOB)["can_rebuild"])

    def test_a_first_draft_made_without_the_agenda_is_rebuilt_without_it(self):
        self.agenda.failure = "Could not load the hearing page: offline"
        self.drafted()
        self.agenda.failure = None
        rebuilt = self.store.rebuild(JOB)
        self.assertEqual(len(self.client.prompts), 1)
        self.assertIsNone(rebuilt["draft"]["extraction_provenance"]["agenda_url"])
        self.assertEqual(len(self.agenda.urls), 0)

    def test_only_finished_unreviewed_gemini_drafts_can_be_rebuilt(self):
        self.store.extractor = None
        self.store.import_one(fixture(JOB))  # Rule baseline.
        raw = wav_bytes(1)
        waiting = self.store.enqueue_audio(metadata(), receive_audio(self.folder, io.BytesIO(raw), len(raw), "a.wav"),
                                           True)["id"]
        raw = wav_bytes(1, 330)
        awaiting = self.store.enqueue_audio(metadata("b.wav"),
                                            receive_audio(self.folder, io.BytesIO(raw), len(raw), "b.wav"), False)["id"]
        for job_id, reason in ((JOB, "Only a finished Gemini draft"), (waiting, "still processing"),
                               (awaiting, "Only a finished Gemini draft")):
            self.assertFalse(self.store.get(job_id)["can_rebuild"])
            with self.assertRaisesRegex(Conflict, reason):
                self.store.rebuild(job_id)
        self.assertEqual([job["can_rebuild"] for job in self.store.list()], [False, False, False])
        with self.assertRaises(KeyError):
            self.store.rebuild("missing")

    def test_a_correction_saved_during_a_rebuild_is_kept(self):
        before = self.drafted()
        self.agenda.hold = threading.Event()
        failures = []

        def rebuild():
            try:
                self.store.rebuild(JOB)
            except Exception as error:
                failures.append(error)

        worker = threading.Thread(target=rebuild)
        worker.start()
        try:
            eventually(lambda: JOB in self.store.rebuilding)
            self.assertFalse(self.store.get(JOB)["can_rebuild"])
            with self.assertRaisesRegex(Conflict, "already being rebuilt"):
                self.store.rebuild(JOB)
            corrected = self.store.review(JOB, before["revision"], [{"path": "/units/0/bill", "review": "APPROVED"}])
        finally:
            self.agenda.hold.set()
            worker.join(10)
        self.assertRegex(str(failures[0]), "changed while it was being rebuilt, so nothing was replaced")
        self.assertEqual(self.store.get(JOB), corrected)

    def test_redrafting_after_attaching_a_recording_still_loads(self):
        self.store.extractor = None
        self.store.import_one(fixture(JOB))
        raw = wav_bytes(1)
        self.store.attach_audio(JOB, receive_audio(self.folder, io.BytesIO(raw), len(raw), "renamed.wav"))
        service = extraction.ExtractionService(self.store, threading.Event(), Keys(), self.client)
        self.store.extractor = service
        service.configure("gemini", api_key=KEY)
        self.store.redraft(JOB)
        queue = AudioQueue(self.store)
        try:
            eventually(lambda: self.store.get(JOB)["processing"] == "Complete", timeout=10)
        finally:
            queue.close()
        restarted = Store(self.folder)
        self.assertEqual(restarted.data.get("set_aside", []), [])
        self.assertEqual(restarted.get(JOB)["draft"]["hearing"]["source_file"], "renamed.wav")


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        times = (f"2026-10-20T12:00:{n:02}+00:00" for n in itertools.count())
        clock = patch("hearing_compiler.store._now", lambda: next(times))
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def recording(self, store, can_transcribe=True):
        raw = wav_bytes(1)
        return store.enqueue_audio(metadata(), receive_audio(store.directory, io.BytesIO(raw), len(raw), "hearing.wav"),
                                   can_transcribe)["id"]

    def test_each_processing_stage_records_when_it_began(self):
        store = Store(self.folder)
        job_id = self.recording(store)

        def progress():
            listed = next(job for job in store.list() if job["id"] == job_id)
            detail = store.get(job_id)
            shown = {key: detail[key] for key in ("processing", "stage_started_at", "duration_seconds")}
            self.assertEqual({key: listed[key] for key in shown}, shown)
            return tuple(shown.values())

        self.assertEqual(progress(), ("Waiting", "2026-10-20T12:00:00+00:00", None))
        store.claim_audio(True)
        self.assertEqual(progress(), ("Transcribing", "2026-10-20T12:00:01+00:00", None))
        document = {**fixture(JOB), "synthetic": False, "source_scope": "public_hearing"}
        store.cache_transcript(job_id, document, from_worker=True)
        last_end = document["segments"][-1]["end"]
        self.assertEqual(progress(), ("Analyzing", "2026-10-20T12:00:02+00:00", last_end))
        set_stage(store.data["jobs"][job_id], "Analyzing")  # The worker's own step into drafting keeps the start.
        self.assertEqual(progress(), ("Analyzing", "2026-10-20T12:00:02+00:00", last_end))
        store.fail_audio(job_id, "extraction", "Gemini was busy.")
        self.assertEqual(progress(), ("Failed", None, last_end))
        store.retry_audio(job_id, True)
        self.assertEqual(progress(), ("Waiting", "2026-10-20T12:00:03+00:00", last_end))
        store.claim_audio(True)
        store.finish_audio(job_id, extract(document))
        self.assertEqual(progress(), ("Complete", None, last_end))
        self.assertNotIn("stage_started_at", store.export_snapshot(job_id)["job"])

    def test_saved_states_without_timer_fields_load_and_a_resumed_wait_starts_again(self):
        store = Store(self.folder)
        waiting = self.recording(store)  # Its wait starts at :00, and the rule draft's Analyzing at :01.
        store.import_one(fixture(JOB))
        state = json.loads(store.path.read_text(encoding="utf-8"))
        state["jobs"][JOB].update(processing="Analyzing", draft=None)  # Stopped mid-draft.
        for job in state["jobs"].values():
            job.pop("stage_started_at", None)  # As saved by build 21.
        store.path.write_text(json.dumps(state), encoding="utf-8")
        restarted = Store(self.folder)
        self.assertEqual(restarted.data.get("set_aside", []), [])
        resumed = restarted.get(waiting)
        self.assertEqual((resumed["processing"], resumed["stage_started_at"]), ("Waiting", "2026-10-20T12:00:02+00:00"))
        self.assertEqual((restarted.get(JOB)["processing"], restarted.get(JOB)["stage_started_at"]), ("Failed", None))
        validate_saved_state(json.loads(restarted.path.read_text(encoding="utf-8")))
        # A wait left over from an earlier session restarts too.
        self.assertEqual(Store(self.folder).get(waiting)["stage_started_at"], "2026-10-20T12:00:03+00:00")

    def test_backups_keep_the_timer_field_and_saved_replies(self):
        store = Store(self.folder / "source")
        store.extractor = lambda document: extraction.gemini_draft(
            document, KEY, MODEL, Client(), cache_dir=store.directory / "extraction-output")
        store.import_one(fixture(JOB))  # Analyzing from :00.
        older(store, JOB)
        waiting = self.recording(store)  # Waiting from :01.
        archive = create_backup(store)
        manifest, state = verify_backup(archive)
        self.assertTrue(any(name.startswith("extraction-output/") for name in manifest["files"]))
        self.assertIn("stage_started_at", state["jobs"][waiting])
        restored = Store(self.folder / "restored")
        restore_backup(restored, archive)
        restored.reset_stage_timers()
        self.assertEqual(restored.get(waiting)["stage_started_at"], "2026-10-20T12:00:02+00:00")
        self.assertTrue(restored.get(JOB)["can_rebuild"])


class RebuildRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.agenda, self.client, self.mw = HeldAgenda(), Gemini(), Mw()
        fetch = patch.object(agenda, "fetch", self.agenda)
        fetch.start()
        self.addCleanup(fetch.stop)
        self.server = make_server(Path(self.temporary.name) / "state", 0, transcription_provider=self.mw)
        store = self.server.store
        store.extractor = extraction.ExtractionService(store, self.server.audio_queue.stop_event, Keys(), self.client)
        store.extractor.configure("gemini", api_key=KEY)
        store.set_model("whisper-cpp:ggml-base.en", "srt")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.token = json.loads(urlopen(self.base + "/api/session").read())["token"]

    def tearDown(self):
        if self.client.hold is not None:
            self.client.hold.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)
        self.temporary.cleanup()

    def get(self, path):
        return json.loads(urlopen(self.base + path).read())

    def post(self, path, body):
        request = Request(self.base + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "X-Review-Token": self.token})
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_rebuild_route_keeps_the_recording_and_link_and_reports_its_refusals(self):
        self.client.hold = threading.Event()
        query = quote(json.dumps({**metadata(), "hearing_url": LINK}))
        job_id = json.loads(urlopen(Request(f"{self.base}/api/audio/upload?metadata={query}", data=wav_bytes(2), headers={
            "Content-Type": "application/octet-stream", "X-Review-Token": self.token})).read())["id"]
        eventually(lambda: self.get(f"/api/hearings/{job_id}")["processing"] == "Analyzing", timeout=10)
        (busy,) = self.get("/api/hearings")
        self.assertEqual(datetime.fromisoformat(busy["stage_started_at"]).utcoffset(), timedelta(0))
        self.assertEqual((busy["can_rebuild"], busy["duration_seconds"]), (False, 49.0))  # The last SRT cue's end.
        self.assertEqual(self.post("/api/rebuild", {"id": job_id}),
                         (409, {"error": "This hearing is still processing. Wait for its draft before rebuilding."}))
        self.client.hold.set()
        eventually(lambda: self.get(f"/api/hearings/{job_id}")["processing"] == "Complete", timeout=10)
        self.assertFalse(self.get(f"/api/hearings/{job_id}")["can_rebuild"])  # Drafted by this build.
        older(self.server.store, job_id)
        before = self.get(f"/api/hearings/{job_id}")
        self.assertEqual((before["can_rebuild"], before["stage_started_at"]), (True, None))
        self.assertTrue(self.get("/api/hearings")[0]["can_rebuild"])
        status, rebuilt = self.post("/api/rebuild", {"id": job_id})
        self.assertEqual(status, 200, rebuilt)
        self.assertEqual((rebuilt["revision"], len(self.client.prompts)), (before["revision"] + 1, 1))
        for key in ("id", "hearing", "hearing_url", "audio", "audio_url", "input"):
            self.assertEqual(rebuilt[key], before[key])
        self.assertTrue(rebuilt["draft"]["extraction_provenance"]["reused_saved_reply"])
        self.assertEqual(self.agenda.urls, [LINK])  # The draft kept its agenda; the page isn't fetched again.
        self.agenda.bills = [{"bill": "H.9", "title": "Another act", "sponsor": "B. Sponsor"}]
        self.assertEqual(self.post("/api/rebuild", {"id": job_id}),
                         (409, {"error": "This draft already has the app's latest fixes."}))
        older(self.server.store, job_id)
        status, again = self.post("/api/rebuild", {"id": job_id})  # A changed page doesn't block it.
        self.assertEqual((status, again["revision"], len(self.client.prompts)), (200, rebuilt["revision"] + 1, 1))
        older(self.server.store, job_id)
        again = self.get(f"/api/hearings/{job_id}")
        for reply in (self.server.store.directory / "extraction-output").glob("*.json"):
            reply.unlink()
        self.assertEqual(self.post("/api/rebuild", {"id": job_id}), (400, {"error": REBUILD_MISS}))
        self.assertEqual(self.get(f"/api/hearings/{job_id}"), again)
        self.post("/api/review", {"id": job_id, "revision": again["revision"],
                                  "changes": [{"path": "/units/0/bill", "review": "APPROVED"}]})
        self.assertEqual(self.post("/api/rebuild", {"id": job_id}),
                         (409, {"error": "This draft already has your review work, so it won't be replaced."}))
        self.assertEqual(self.post("/api/rebuild", {"id": "missing"}), (404, {"error": "Hearing not found."}))
        self.assertEqual(len(self.client.prompts), 1)


if __name__ == "__main__":
    unittest.main()
