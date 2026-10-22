from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from hearing_compiler.pipeline import fingerprint


def source_packet(document, current, context=()):
    """Validator context for one excerpt (moved here from the retired local-model client)."""
    packet = {
        "hearing": copy.deepcopy(document["hearing"]),
        "coverage": {"span_id": "span-" + fingerprint([s["id"] for s in current])[:20],
                     "scope": "excerpt", "gaps": [], "complete_testimony_ranges": []},
        "context_segments": copy.deepcopy(list(context)), "trusted_metadata": [],
        "earlier_extractions": [],
    }
    if document.get("source_format") == "plain-text-utf8":
        packet["source_profile"] = "plain-text-utf8"
        packet["source_document"] = copy.deepcopy(document)
    return packet
from hearing_compiler.pipeline import InputError, correct, field, fields, markdown, review_summary
from hearing_compiler.rich_review import AGENDA_TITLE, NOT_ON_AGENDA, agenda_titles, from_packets, notion, prepare
from hearing_compiler.store import Store
from hearing_compiler.word_export import docx_bytes
from hearing_compiler.model_contract.validator import validate_model_draft
from review_helpers import add_missing_issues


def joint_packet():
    texts = [
        ("Voice A", "I am Nia Frost with Fictional Renewal Council."),
        ("Voice B", "I am Toma Reed with Fictional Local Offices."),
        ("Voice A", "We are presenting jointly. We support H.8181 only if the written-authorization condition remains."),
        ("Voice A", "The filing is allowed only with written authorization."),
        ("Voice B", "The proposal must keep local costs within the stated budget."),
        ("Voice C", "I am Representative Stone. Who covers the costs?"),
        ("Voice A", "The agency covers half."),
        ("Voice B", "The local office covers the remaining half."),
    ]
    document = {"schema_version": 1, "synthetic": True,
                "hearing": {"id": "joint-rich-test", "title": "Fictional joint test", "date": "2026-10-07",
                            "committee": "Example Committee", "source_file": "fictional.mp3"},
                "segments": [{"id": f"joint{i + 1}", "speaker": speaker, "start": i * 5,
                              "end": i * 5 + 4, "text": text} for i, (speaker, text) in enumerate(texts)]}
    context = source_packet(document, document["segments"])
    def claim(value, index=None):
        return {"value": value, "basis": "direct" if value is not None else "missing",
                "missing_reason": None if value is not None else "not_available_in_excerpt",
                "evidence": [] if index is None else [{"segment_id": f"joint{index}", "quote": texts[index - 1][1]}],
                "metadata_refs": []}
    names = {"Voice A": claim("Nia Frost", 1), "Voice B": claim("Toma Reed", 2), "Voice C": claim("Representative Stone", 6)}
    output = {"contract": "hearing-extraction-draft/v1", "hearing_id": "joint-rich-test",
              "input_span_id": context["coverage"]["span_id"], "review_issues": [],
              "unassigned_questions": [], "coverage_ledger": [],
              "testimony_units": [{
                  "unit_id": "unit-joint1-1", "segment_ids": [s["id"] for s in document["segments"]],
                  "testifiers": [{"speaker_labels": [speaker], "name": names[speaker],
                                 "organization": claim(org, index)} for speaker, org, index in
                                [("Voice A", "Fictional Renewal Council", 1), ("Voice B", "Fictional Local Offices", 2)]],
                  "joint_basis": claim("", 3)["evidence"], "bill_id": claim("H.8181", 3),
                  "bill_title": claim(None), "position": claim("Support", 3),
                  "arguments": [{"contributors": ["Voice A"], "text": claim(texts[3][1], 4)},
                                {"contributors": ["Voice B"], "text": claim(texts[4][1], 5)}],
                  "questions": [{"asker": {"speaker_labels": ["Voice C"], "name": names["Voice C"]},
                                 "asker_role": claim("legislator", 6), "question": claim("Who covers the costs?", 6),
                                 "bill_id": claim("H.8181", 3), "response_status": "answered",
                                 "responses": [{"respondent": {"speaker_labels": [speaker], "name": names[speaker]},
                                                "text": claim(texts[index - 1][1], index)}
                                               for speaker, index in [("Voice A", 7), ("Voice B", 8)]]}],
                  "question_status": "present"}]}
    add_missing_issues(output)
    for segment in document["segments"]:
        output["coverage_ledger"].append({"segment_id": segment["id"], "categories": ["testimony"],
                                           "record_refs": ["/testimony_units/0"], "reason": None})
    packet = {"output": output, "context": context, "current": document["segments"]}
    return document, packet


class RichReviewTests(unittest.TestCase):
    def draft(self):
        doc, packet = joint_packet()
        report = validate_model_draft(json.dumps(packet["output"]), packet["context"], packet["current"])
        self.assertEqual(report["output_status"], "VALID", report)
        return from_packets(doc, [packet], {"model": "synthetic test"})

    def test_joint_members_individual_arguments_and_multiple_responses_are_not_flattened(self):
        draft = self.draft()
        unit = draft["units"][0]
        self.assertEqual(len(unit["testifiers"]), 2)
        self.assertEqual([a["contributors"] for a in unit["arguments"]], [["Voice A"], ["Voice B"]])
        self.assertEqual(len(unit["questions"][0]["responses"]), 2)
        self.assertNotEqual(review_summary(draft)["status"], "APPROVED")
        self.assertTrue(all(item["review"] != "APPROVED" for _, item in fields(draft)))
        note = markdown(draft)
        for text in ("Nia Frost", "Toma Reed", "written authorization", "stated budget",
                     "The agency covers half.", "The local office covers the remaining half."):
            self.assertIn(text, note)
        self.assertEqual(note.count("The filing is allowed"), 1)
        self.assertTrue(docx_bytes(draft).startswith(b"PK"))

    def test_invalid_model_approval_cannot_enter_review_adapter(self):
        doc, packet = joint_packet()
        packet["output"]["APPROVED"] = True
        with self.assertRaisesRegex(InputError, "invalid model"):
            from_packets(doc, [packet], {})

    def test_corrections_clear_current_model_quotes_but_preserve_original_provenance(self):
        draft = self.draft()
        original = copy.deepcopy(draft["units"][0]["arguments"][0]["text"]["source_quotes"])
        edited = correct(draft, [{"path": "/units/0/arguments/0/text", "value": "Human corrected condition.", "review": "NEEDS_REVIEW"}])
        item = edited["units"][0]["arguments"][0]["text"]
        self.assertEqual(item["source_quotes"], [])
        self.assertEqual(item["original_source_quotes"], original)
        self.assertEqual(draft["units"][0]["arguments"][0]["text"]["source_quotes"], original)
        self.assertEqual(edited["segments"], draft["segments"])

    def test_structural_repairs_reach_exports_history_and_restart(self):
        doc, packet = joint_packet()
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder), extractor=lambda document: from_packets(document, [packet], {}))
            job_id = store.import_one(doc)["id"]
            job = store.get(job_id)
            repaired = store.review(job_id, job["revision"], [], [
                {"op": "rich_remove", "path": "/units/0/questions/0/responses/1"},
                {"op": "rich_add_response", "path": "/units/0/questions/0/responses",
                 "name": "Toma Reed", "labels": ["Voice B"], "value": "Reviewer retained the remaining-half response.", "evidence": ["joint8"]},
                {"op": "rich_add_argument", "path": "/units/0/arguments", "labels": ["Voice A"],
                 "value": "A missed source-backed reviewer argument.", "evidence": ["joint4"]},
            ])
            restored = Store(Path(folder)).get(job_id)
            self.assertEqual(restored["draft"], repaired["draft"])
            self.assertIn("Reviewer retained", restored["markdown"])
            self.assertEqual(len(restored["draft"]["review_log"]), 3)
            archive = store.export_snapshot(job_id)
            self.assertEqual(archive["job"]["draft"]["coverage_ledger"], repaired["draft"]["coverage_ledger"])

    def test_attribution_repairs_cannot_modify_source_or_invent_voice_labels(self):
        draft = self.draft()
        for op in ({"op": "rich_set_mapping", "path": "/segments/0", "labels": []},
                   {"op": "rich_set_mapping", "path": "/units/0/testifiers/0", "labels": ["Invented voice"]}):
            with self.assertRaises(InputError):
                correct(draft, [], [op])
        repaired = correct(draft, [], [{"op": "rich_set_contributors", "path": "/units/0/arguments/0", "labels": ["Voice B"]}])
        self.assertEqual(repaired["units"][0]["arguments"][0]["contributors"], ["Voice B"])
        self.assertEqual(repaired["segments"], draft["segments"])

    def test_unknown_roles_remain_separate_from_legislator_heading_and_questions_can_move(self):
        draft = self.draft()
        changed = correct(draft, [{"path": "/units/0/questions/0/asker_role", "value": "other", "review": "NEEDS_REVIEW"}])
        self.assertIn("Other or unresolved question roles", markdown(changed))
        moved = correct(changed, [], [{"op": "rich_move_question", "path": "/units/0/questions/0", "destination": "/unassigned_questions"}])
        self.assertEqual(len(moved["units"][0]["questions"]), 0)
        self.assertEqual(len(moved["unassigned_questions"][0]["responses"]), 2)
        self.assertIn("Unassigned exchanges", markdown(moved))

    def test_no_questions_confirmation_cannot_hide_retained_exchange(self):
        draft = self.draft()
        with self.assertRaises(InputError):
            correct(draft, [], [{"op": "rich_confirm_no_questions", "path": "/units/0",
                                "confirmed": True, "evidence": ["joint1"]}])

    def approved(self):
        draft = self.draft()
        for _, item in fields(draft):
            if not item["value"]:
                item["value"] = "Fictional reviewer resolution"
            item["review"] = "APPROVED"
        self.assertEqual(review_summary(draft)["status"], "APPROVED")
        return draft

    def test_mapping_changes_invalidate_dependent_roles_and_response_text_only(self):
        draft = self.approved()
        original = copy.deepcopy(draft)
        changed = correct(draft, [], [
            {"op": "rich_set_mapping", "path": "/units/0/questions/0/asker", "labels": ["Voice A"]},
            {"op": "rich_set_mapping", "path": "/units/0/questions/0/responses/0/respondent", "labels": ["Voice B"]},
        ])
        exchange = changed["units"][0]["questions"][0]
        for item in (exchange["asker_role"], exchange["question"], exchange["bill"], exchange["responses"][0]["text"]):
            self.assertEqual(item["review"], "NEEDS_REVIEW")
            self.assertEqual(item["source_quotes"], [])
            self.assertTrue(item["original_source_quotes"])
        self.assertEqual(exchange["responses"][1], original["units"][0]["questions"][0]["responses"][1])
        self.assertEqual(changed["segments"], original["segments"])
        self.assertNotEqual(review_summary(changed)["status"], "APPROVED")

    def test_unassigned_matching_or_ambiguous_exchange_blocks_absence_but_other_bill_does_not(self):
        draft = self.draft()
        moved = correct(draft, [], [{"op": "rich_move_question", "path": "/units/0/questions/0",
                                   "destination": "/unassigned_questions"}])
        confirm = {"op": "rich_confirm_no_questions", "path": "/units/0", "confirmed": True, "evidence": ["joint1"]}
        for bill in ("H.8181", None):
            moved["unassigned_questions"][0]["bill"]["value"] = bill
            with self.subTest(bill=bill), self.assertRaisesRegex(InputError, "unassigned exchanges"):
                correct(moved, [], [confirm])
        moved["unassigned_questions"][0]["bill"]["value"] = "H.9292"
        confirmed = correct(moved, [], [confirm])
        self.assertEqual(confirmed["units"][0]["questions_none"]["review"], "APPROVED")
        # A legacy false absence is normalized without rewriting stored history.
        confirmed["unassigned_questions"][0]["bill"]["value"] = "H.8181"
        before = copy.deepcopy(confirmed)
        self.assertNotIn("None noted during this testimony.", markdown(confirmed))
        self.assertNotEqual(review_summary(confirmed)["status"], "APPROVED")
        self.assertEqual(confirmed, before)

    def test_no_questions_decisions_return_when_a_matching_exchange_is_moved_back(self):
        draft = self.draft()
        other = copy.deepcopy(draft["units"][0])  # A second testimony on the same bill, with no exchanges.
        other.update(id="span-1-unit-joint1-2", questions=[], question_status="none_detected_in_supplied_material",
                     questions_none=field(None, [], True))
        draft["units"].append(other)
        confirmed = correct(draft, [], [{"op": "rich_confirm_no_questions", "path": "/units/1", "confirmed": True,
                                         "evidence": ["joint1"]}])
        moved = correct(confirmed, [], [{"op": "rich_move_question", "path": "/units/0/questions/0",
                                         "destination": "/unassigned_questions"}])
        # While the unassigned exchange might belong to that testimony, its decision is shown as open...
        self.assertNotIn("None noted during this testimony.", markdown(moved))
        self.assertEqual(moved["units"][1]["questions_none"]["review"], "APPROVED")  # ...but stays saved.
        back = correct(moved, [], [{"op": "rich_move_question", "path": "/unassigned_questions/0",
                                    "destination": "/units/0/questions"}])
        self.assertIn("None noted during this testimony.", markdown(back))

    def test_moving_an_exchange_to_where_it_already_is_changes_nothing(self):
        draft = self.approved()
        unchanged = correct(draft, [], [{"op": "rich_move_question", "path": "/units/0/questions/0",
                                         "destination": "/units/0/questions"}])
        self.assertEqual(unchanged, prepare(draft))  # No approvals reset, no reordering, no history entry.

    def test_removing_last_response_creates_reviewable_missing_state_and_resolution_survives_restart(self):
        draft = self.approved()
        changed = correct(draft, [], [
            {"op": "rich_remove", "path": "/units/0/questions/0/responses/1"},
            {"op": "rich_remove", "path": "/units/0/questions/0/responses/0"},
        ])
        exchange = changed["units"][0]["questions"][0]
        self.assertEqual(exchange["response_status"], "unclear")
        self.assertEqual(exchange["response_resolution"]["review"], "UNRESOLVED")
        self.assertNotEqual(review_summary(changed)["status"], "APPROVED")
        self.assertNotIn("Response unavailable/unclear: answered", markdown(changed))
        resolved = correct(changed, [{"path": "/units/0/questions/0/response_resolution",
                                    "value": "Reviewer checked supplied source; the answer is outside this excerpt.",
                                    "review": "APPROVED"}])
        self.assertIn("answer is outside this excerpt", markdown(resolved))
        restored = correct(resolved, [], [{"op": "rich_add_response", "path": "/units/0/questions/0/responses",
                                         "name": "Nia Frost", "labels": ["Voice A"],
                                         "value": "The agency covers half.", "evidence": ["joint7"]}])
        self.assertIsNone(restored["units"][0]["questions"][0]["response_resolution"])
        self.assertNotEqual(review_summary(restored)["status"], "APPROVED")
        self.assertTrue(docx_bytes(restored).startswith(b"PK"))

    def test_queue_detail_and_export_normalize_existing_inapplicable_field_without_saved_rewrite(self):
        doc, packet = joint_packet()
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder), extractor=lambda source: from_packets(source, [packet], {}))
            job_id = store.import_one(doc)["id"]
            store.data["jobs"][job_id]["draft"] = self.approved()
            from hearing_compiler.pipeline import field
            store.data["jobs"][job_id]["draft"]["units"][0]["questions_none"] = field(None, [], True)
            store._save()
            before = store.path.read_bytes()
            loaded = Store(Path(folder))
            self.assertEqual(loaded.list()[0]["review"], loaded.get(job_id)["review"])
            self.assertEqual(loaded.get(job_id)["review"], loaded.export_snapshot(job_id)["job"]["review"])
            self.assertEqual(loaded.get(job_id)["review"]["status"], "APPROVED")
            self.assertEqual(loaded.path.read_bytes(), before)

    def test_blank_substantive_repairs_are_rejected_and_exports_keep_revision_identity(self):
        draft = self.draft()
        for operation, path in (("rich_add_argument", "/units/0/arguments"),
                                ("rich_add_question", "/units/0/questions"),
                                ("rich_add_response", "/units/0/questions/0/responses")):
            with self.subTest(op=operation), self.assertRaises(InputError):
                correct(draft, [], [{"op": operation, "path": path, "value": None, "evidence": ["joint4"]}])
        draft["export_revision"], draft["export_saved_at"] = 12, "2026-10-07T13:00:00Z"
        text = markdown(draft)
        self.assertIn("Saved review revision: 12", text)
        self.assertIn("Bill evidence:", text)
        self.assertIn("Testifier evidence:", text)


class TypedBillTests(unittest.TestCase):
    """A bill typed in review takes the agenda's official title, flagged until confirmed; one that isn't on
    the agenda is flagged. Speech recognition once turned "bodily autonomy" into "bottle the autonomy"."""
    AGENDA = [{"bill": "H.2011", "title": "An Act relative to bodily autonomy and family integrity", "sponsor": "A. Sponsor"},
              {"bill": "S.1227", "title": "An Act relative to bodily autonomy and family integrity", "sponsor": "B. Sponsor"},
              {"bill": "H.1975", "title": "An Act relative to adoptions", "sponsor": "C. Sponsor"}]
    HEARD = "An Act relative to bottle the autonomy and family integrity"
    OFFICIAL = "An Act relative to bodily autonomy and family integrity"

    def draft(self, bill=None):
        from hearing_compiler import extraction
        from test_gemini_extraction import KEY, Client
        from test_pipeline import fixture
        document = fixture("sample-hearing")
        document["hearing_url"] = "https://malegislature.gov/Events/Hearings/Detail/1234"
        reply = {"entries": [{"start_cue": 1, "end_cue": 4, "bill": bill, "bill_title": self.HEARD,
                              "bill_quote": "I support H.1234",
                              "testifiers": [{"name": "Alex Morgan", "quote": "I'm Alex Morgan with Example Legal Services"}],
                              "position": "Support", "position_quote": "I support H.1234",
                              "arguments": [{"text": "Expands access.", "quote": "The proposal would expand access"}]}]}
        return extraction.gemini_draft(document, KEY, "gemini-3.8-flash", Client(reply),
                                       fetch_agenda=lambda url: {"url": url, "bills": self.AGENDA})

    def test_typed_bills_are_normalized_and_take_the_official_title_until_it_is_confirmed(self):
        draft = self.draft()
        self.assertEqual(agenda_titles(draft), {b["bill"]: b["title"] for b in self.AGENDA})
        changed = correct(draft, [{"path": "/units/0/bill", "value": "h2011 / S. 1227", "review": "APPROVED"}])
        bill, title = changed["units"][0]["bill"], changed["units"][0]["bill_title"]
        self.assertEqual((bill["value"], bill["review"], bill.get("flag")), ("H.2011 / S.1227", "APPROVED", None))
        # The reviewer approved the bill; the title she didn't type waits for her.
        self.assertEqual((title["value"], title["review"], title["flag"], title["original_value"]),
                         (self.OFFICIAL, "NEEDS_REVIEW", AGENDA_TITLE, self.HEARD))
        self.assertEqual(changed["review_log"][-1]["op"], "agenda_title")
        self.assertIn(f"Bill: H.2011 / S.1227 — {self.OFFICIAL} ⚠\n", notion(changed))
        confirmed = correct(changed, [{"path": "/units/0/bill_title", "review": "APPROVED"}])
        self.assertIn(f"Bill: H.2011 / S.1227 — {self.OFFICIAL}\n", notion(confirmed))
        for typed, shown in (("H. 1975", "H.1975"), ("house bill 1975", "H.1975"), ("H.1975, S.1227", "H.1975 / S.1227")):
            with self.subTest(typed):
                self.assertEqual(correct(draft, [{"path": "/units/0/bill", "value": typed}])["units"][0]["bill"]["value"],
                                 shown)

    def test_bills_not_on_the_agenda_are_flagged_and_a_title_typed_in_the_same_save_is_kept(self):
        draft = self.draft()
        for typed in ("H.4444", "H.1975 / S.9999", "the adoption bill"):
            with self.subTest(typed):
                unit = correct(draft, [{"path": "/units/0/bill", "value": typed}])["units"][0]
                self.assertEqual((unit["bill"]["flag"], unit["bill_title"]["value"]), (NOT_ON_AGENDA, self.HEARD))
        misheard = self.draft("H.4321")
        self.assertEqual(misheard["units"][0]["bill"]["flag"], NOT_ON_AGENDA)
        again = correct(misheard, [{"path": "/units/0/bill", "value": "H.4444"}])["units"][0]["bill"]
        self.assertEqual(again["flag"], NOT_ON_AGENDA)  # Not repeated.
        fixed = correct(misheard, [{"path": "/units/0/bill", "value": "H.1975"}])["units"][0]
        self.assertEqual((fixed["bill"].get("flag"), fixed["bill_title"]["value"]), (None, "An Act relative to adoptions"))
        kept = correct(draft, [{"path": "/units/0/bill_title", "value": "An Act relative to adoption"},
                               {"path": "/units/0/bill", "value": "H.1975"}])["units"][0]["bill_title"]
        self.assertEqual((kept["value"], kept.get("flag")), ("An Act relative to adoption", None))

    def test_drafts_without_a_saved_agenda_normalize_typed_bills_and_recheck_the_old_title(self):
        from hearing_compiler.rich_review import TITLE_CHANGED
        old = self.draft()
        del old["extraction_provenance"]["agenda"]  # Drafts from before the agenda was saved.
        doc, packet = joint_packet()
        for draft in (old, from_packets(doc, [packet], {"model": "synthetic test"})):
            self.assertEqual(agenda_titles(draft), {})
            before = copy.deepcopy(draft["units"][0]["bill_title"])
            unit = correct(draft, [{"path": "/units/0/bill", "value": "s 8181"}])["units"][0]
            self.assertEqual((unit["bill"]["value"], unit["bill"].get("flag")), ("S.8181", None))
            title = unit["bill_title"]
            if not before["value"]:
                self.assertEqual(title, before)
                continue
            # The old title may belong to the old number, so it goes back to review.
            self.assertEqual((title["value"], title["review"], title["flag"]),
                             (before["value"], "NEEDS_REVIEW", TITLE_CHANGED))
            # Tidying the same number ("S. 8181" for S.8181) leaves an approved title alone.
            settled = correct(correct(draft, [{"path": "/units/0/bill", "value": "S.8181"}]),
                              [{"path": "/units/0/bill_title", "review": "APPROVED"}])
            again = correct(settled, [{"path": "/units/0/bill", "value": "S. 8181"}])["units"][0]["bill_title"]
            self.assertEqual(again["review"], "APPROVED")

    def test_an_off_agenda_bill_approved_in_the_same_save_still_shows_its_warning(self):
        unit = correct(self.draft(), [{"path": "/units/0/bill", "value": "H.4444", "review": "APPROVED"}])["units"][0]
        self.assertEqual((unit["bill"]["review"], unit["bill"]["flag"]), ("NEEDS_REVIEW", NOT_ON_AGENDA))
