from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hearing_compiler.pipeline import InputError, extract
from hearing_compiler.store import Conflict, Store
from test_pipeline import fixture


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory)

    def tearDown(self):
        self.temporary.cleanup()

    def test_saved_correction_survives_restart_and_reimport_uses_completed_draft(self):
        document = fixture("sample-hearing")
        self.store.import_one(document)
        edited = self.store.review("sample-hearing", 0, [
            {"path": "/questions/0/asker", "value": "Rep. Avery", "review": "APPROVED"},
        ])
        restarted = Store(self.directory)
        self.assertEqual(restarted.get("sample-hearing")["draft"], edited["draft"])
        with patch("hearing_compiler.store.extract", side_effect=AssertionError("must not repeat extraction")):
            reused = restarted.import_one(document)
        self.assertTrue(reused["reused"])
        self.assertIn("Rep. Avery", restarted.get("sample-hearing")["markdown"])

    def test_failure_isolated_retry_repairs_only_failed_hearing(self):
        results = self.store.import_batch([fixture("sample-hearing"), fixture("broken-order"), fixture("no-questions")])
        self.assertEqual([r["processing"] for r in results], ["Complete", "Failed", "Complete"])
        before = self.store.get("sample-hearing")
        fixed = fixture("broken-order")
        fixed["segments"].sort(key=lambda s: s["start"])
        repaired = self.store.retry("broken-order", fixed)
        self.assertEqual(repaired["processing"], "Complete")
        self.assertEqual(self.store.get("sample-hearing"), before)
        with self.assertRaises(Conflict):
            self.store.retry("sample-hearing")

    def test_malformed_metadata_does_not_stop_batch(self):
        broken = {"schema_version": 1, "synthetic": True, "hearing": None, "segments": []}
        result = self.store.import_batch([broken, fixture("sample-hearing")])
        self.assertEqual([r["processing"] for r in result], ["Failed", "Complete"])

    def test_imports_with_extra_hearing_fields_or_empty_id_survive_restart(self):
        extra = fixture("sample-hearing")
        extra["hearing"]["room"] = "B-1"
        empty_id = fixture("no-questions")
        empty_id["hearing"]["id"] = ""
        result = self.store.import_batch([extra, empty_id])
        self.assertEqual([r["processing"] for r in result], ["Complete", "Failed"])
        restarted = Store(self.directory)
        self.assertEqual(len(restarted.list()), 2)
        self.assertNotIn("room", restarted.get("sample-hearing")["draft"]["hearing"])
        self.assertEqual(restarted.get("sample-hearing")["input"]["hearing"]["room"], "B-1")
        self.assertNotIn("set_aside", restarted.data)

    def test_one_unreadable_saved_hearing_is_set_aside_without_losing_the_rest(self):
        self.store.import_one(fixture("sample-hearing"))
        self.store.import_one(fixture("no-questions"))
        state = json.loads(self.store.path.read_text(encoding="utf-8"))
        state["jobs"]["no-questions"]["revision"] = "broken"
        original = json.dumps(state).encode("utf-8")
        self.store.path.write_bytes(original)
        restarted = Store(self.directory)
        self.assertEqual([item["id"] for item in restarted.list()], ["sample-hearing"])
        self.assertEqual(restarted.data["set_aside"][0]["job"], state["jobs"]["no-questions"])
        copies = list((self.directory / "backups").glob("before-set-aside-*.json"))
        self.assertEqual([path.read_bytes() for path in copies], [original])
        # The set-aside record survives later saves and restarts.
        restarted.review("sample-hearing", 0, [{"path": "/questions/0/asker", "value": "Rep. Avery"}])
        again = Store(self.directory)
        self.assertEqual(again.data["set_aside"], restarted.data["set_aside"])
        self.assertEqual(len(list((self.directory / "backups").glob("before-set-aside-*.json"))), 1)

    def test_stale_tab_cannot_overwrite_newer_corrections(self):
        self.store.import_one(fixture("sample-hearing"))
        self.store.review("sample-hearing", 0, [{"path": "/questions/0/asker", "value": "Rep. Avery"}])
        with self.assertRaises(Conflict):
            self.store.review("sample-hearing", 0, [{"path": "/questions/0/asker", "value": "Rep. Wrong"}])
        self.assertEqual(self.store.get("sample-hearing")["draft"]["questions"][0]["asker"]["value"], "Rep. Avery")

    def test_different_input_cannot_overwrite_same_hearing(self):
        original = fixture("sample-hearing")
        self.store.import_one(original)
        original["segments"][0]["text"] = "Different transcript"
        with self.assertRaises(Conflict):
            self.store.import_one(original)

    def test_interrupted_job_recovers_as_retryable_failure(self):
        self.store.import_one(fixture("sample-hearing"))
        state = json.loads(self.store.path.read_text())
        state["jobs"]["sample-hearing"]["processing"] = "Analyzing"
        self.store.path.write_text(json.dumps(state))
        restarted = Store(self.directory)
        self.assertEqual(restarted.get("sample-hearing")["processing"], "Failed")
        self.assertEqual(restarted.retry("sample-hearing")["processing"], "Complete")

    def test_corrupt_state_is_preserved(self):
        self.store.path.write_text("{broken")
        with self.assertRaisesRegex(InputError, "Preserve it"):
            Store(self.directory)
        self.assertEqual(self.store.path.read_text(), "{broken")

    def test_failed_save_does_not_claim_or_keep_an_unpersisted_correction(self):
        self.store.import_one(fixture("sample-hearing"))
        before = self.store.get("sample-hearing")
        with patch("hearing_compiler.store.os.replace", side_effect=PermissionError("disk unavailable")):
            with self.assertRaises(PermissionError):
                self.store.review("sample-hearing", 0, [{"path": "/questions/0/asker", "value": "Rep. Avery"}])
        self.assertEqual(self.store.get("sample-hearing"), before)
        self.assertEqual(Store(self.directory).get("sample-hearing"), before)

    def test_unexpected_provider_failure_is_isolated(self):
        def unreliable(document):
            if document["hearing"]["id"] == "no-questions":
                raise RuntimeError("deliberate provider failure")
            return extract(document)
        store = Store(self.directory, extractor=unreliable)
        with self.assertLogs(level="ERROR"):
            result = store.import_batch([fixture("sample-hearing"), fixture("no-questions"), fixture("multiple-bills")])
        self.assertEqual([r["processing"] for r in result], ["Complete", "Failed", "Complete"])

    def test_final_result_save_failure_becomes_retryable_without_restart_and_keeps_cached_attempt(self):
        original = fixture("sample-hearing")
        self.store.import_one(original)
        self.store.review("sample-hearing", 0, [{"path": "/questions/0/asker", "value": "Saved correction"}])
        preserved = self.store.get("sample-hearing")
        document = fixture("no-questions")
        def provider(source):
            self.store.record_model_attempt(source["hearing"]["id"], {"validation": {"output_status": "VALID"}, "output": {}})
            return extract(source)
        self.store.extractor = provider
        save = self.store._save
        def fail_final():
            if self.store.data["jobs"].get("no-questions", {}).get("processing") == "Complete":
                raise PermissionError("injected final-result save error")
            save()
        with patch.object(self.store, "_save", side_effect=fail_final):
            with self.assertRaises(PermissionError):
                self.store.import_one(document)
        failed = self.store.get("no-questions")
        self.assertEqual(failed["processing"], "Analyzing")
        self.assertFalse(failed["persistence_error"]["persisted"])
        self.assertIn("storage recovery", failed["error"])
        self.assertEqual(len(failed["model_attempts"]), 1)
        self.store.flush_failures()
        self.assertEqual(self.store.get("no-questions")["processing"], "Failed")
        self.assertEqual(self.store.get("no-questions")["failed_stage"], "extraction")
        self.assertEqual(self.store.retry("no-questions")["processing"], "Complete")
        self.assertEqual(len(self.store.get("no-questions")["model_attempts"]), 2)
        self.assertEqual(self.store.get("sample-hearing"), preserved)


if __name__ == "__main__":
    unittest.main()


class DurabilityFixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_set_aside_hearings_come_back_once_they_validate(self):
        store = Store(self.directory)
        store.import_one(fixture("sample-hearing"))
        good = store.data["jobs"].pop("sample-hearing")
        store.data["set_aside"] = [{"id": "sample-hearing", "reason": "old validator", "job": good},
                                   {"id": "broken", "reason": "still bad", "job": {"id": "broken"}}]
        store._save()
        reopened = Store(self.directory)
        self.assertEqual([j["id"] for j in reopened.list()], ["sample-hearing"])
        self.assertEqual([e["id"] for e in reopened.data["set_aside"]], ["broken"])

    def test_word_export_accepts_titles_longer_than_word_allows(self):
        from hearing_compiler.word_export import docx_bytes
        store = Store(self.directory)
        document = fixture("sample-hearing")
        document["hearing"]["title"] = "A very long hearing title " * 15
        store.import_one(document)
        self.assertTrue(docx_bytes(store.get("sample-hearing")["draft"]).startswith(b"PK"))

    def test_rejected_uploads_leave_no_recording_and_repairs_check_their_choices(self):
        import io
        from audio_test_helpers import wav_bytes
        from hearing_compiler.audio import receive_audio
        from hearing_compiler.pipeline import correct
        from hearing_compiler.rich_review import FORMAT
        store = Store(self.directory)
        raw = wav_bytes(2)
        asset = receive_audio(store.directory, io.BytesIO(raw), len(raw), "orphan.wav")
        store.discard_unused_audio(asset)
        self.assertEqual(list((store.directory / "audio").glob("*")), [])
        draft = {"draft_format": FORMAT, "segments": [{"id": "s1", "speaker": "A", "text": "x", "start": 0, "end": 1}],
                 "units": [], "unassigned_questions": [], "issues": [], "review_log": [], "coverage_ledger": [],
                 "hearing": {}, "synthetic": True}
        with self.assertRaisesRegex(InputError, "Position"):
            correct(draft, [], [{"op": "rich_add_unit", "path": "/units", "position": "Maybe later",
                                 "evidence": ["s1"], "name": "A", "bill": "H.1"}])
        with self.assertRaisesRegex(InputError, "role"):
            correct(draft, [], [{"op": "rich_add_question", "path": "/unassigned_questions", "role": "janitor",
                                 "evidence": ["s1"], "name": "A", "value": "Why?"}])
