from __future__ import annotations

import copy
import unittest

from hearing_compiler.pipeline import InputError, correct, extract, fields, markdown, prepare_review, review_summary
from test_pipeline import fixture


class StructuralReviewTests(unittest.TestCase):
    def setUp(self):
        self.draft = extract(fixture("sample-hearing"))

    def apply(self, *operations, draft=None):
        return correct(self.draft if draft is None else draft, operations=list(operations))

    def test_missing_testimony_statement_argument_and_question_can_be_repaired(self):
        original = copy.deepcopy(self.draft)
        draft = self.apply(
            {"op": "add_testimony", "speaker": "Speaker 2", "name": "Example Witness",
             "organization": None, "evidence": ["s02"]},
            {"op": "add_statement", "testimony": 1, "bill": "H.1911", "position": "Support", "evidence": ["s02"]},
            {"op": "add_argument", "testimony": 1, "statement": 1,
             "value": "Express authorization is required to create the trust.", "evidence": ["s02"]},
            {"op": "add_question", "asker_speaker": "Speaker 3", "respondent_speaker": "Speaker 2",
             "asker": "Representative Example", "respondent": "Example Witness", "bill": "H.1911",
             "question": "Please explain the authorization requirement.",
             "response": "The document must expressly authorize it.", "evidence": ["s03"], "response_evidence": ["s04"]})
        self.assertEqual(self.draft, original)
        self.assertEqual(draft["segments"], original["segments"])
        self.assertEqual(draft["hearing"], original["hearing"])
        statement = draft["testimonies"][1]["statements"][1]
        self.assertEqual(statement["arguments"][0]["evidence"], ["s02"])
        self.assertEqual(statement["arguments"][0]["original_value"], None)
        self.assertEqual(draft["questions"][1]["response"]["evidence"], ["s04"])
        self.assertEqual(len(draft["review_log"]), 4)
        self.assertTrue(all(item["review"] != "APPROVED" for _, item in fields(draft)))
        note = markdown(draft)
        self.assertIn("authorization requirement", note)
        self.assertIn("[draft; review needed]", note)

    def test_malformed_operations_reject_atomically(self):
        invalid = [
            {"op": "set", "path": "/segments/0/text", "value": "Altered"},
            {"op": "add_testimony", "speaker": "Invented speaker", "evidence": ["s01"]},
            {"op": "add_testimony", "speaker": "Speaker 2", "evidence": ["missing"]},
            {"op": "add_argument", "testimony": True, "statement": 0, "value": "x", "evidence": ["s01"]},
            {"op": "remove_testimony", "testimony": -1},
            {"op": "remove_testimony", "testimony": 0, "hearing": {"title": "Changed"}},
            {"op": "set_evidence", "path": "/hearing/title", "evidence": ["s01"]},
            {"op": "set_evidence", "path": "/testimonies/0/name", "evidence": "s01"},
            {"op": "group_testimonies", "testimonies": [0, 0]},
            {"op": "confirm_no_questions", "testimony": 0, "statement": 0, "confirmed": "true"},
        ]
        for operation in invalid:
            with self.subTest(operation=operation):
                original = copy.deepcopy(self.draft)
                with self.assertRaises(InputError):
                    self.apply({"op": "add_argument", "testimony": 0, "statement": 0,
                                "value": "A repair that must be rolled back.", "evidence": ["s02"]}, operation)
                self.assertEqual(self.draft, original)

    def test_removed_material_remains_in_audit_but_not_export(self):
        before = self.draft["testimonies"][0]["statements"][0]["arguments"][0]
        draft = self.apply({"op": "remove_argument", "testimony": 0, "statement": 0, "argument": 0})
        self.assertEqual(draft["review_log"][0]["before"], before)
        self.assertFalse(any(path.startswith("/review_log") for path, _ in fields(draft)))
        # Full original source is retained even after a false extracted entry is removed.
        draft = correct(draft, operations=[{"op": "remove_testimony", "testimony": 0}])
        self.assertEqual(draft["segments"], self.draft["segments"])
        self.assertIn("No testimony entries remain", markdown(draft))
        self.assertIn("Questions needing testimony or bill attribution", markdown(draft))
        self.assertEqual(review_summary(draft)["status"], "UNRESOLVED")

    def test_evidence_and_association_changes_reset_approval(self):
        draft = correct(self.draft, [{"path": "/testimonies/0/name", "review": "APPROVED"}])
        changed = correct(draft, [], [{"op": "set_evidence", "path": "/testimonies/0/name", "evidence": ["s02"]}])
        self.assertEqual(changed["testimonies"][0]["name"]["review"], "NEEDS_REVIEW")
        self.assertEqual(changed["testimonies"][0]["name"]["original_value"], "Alex Morgan")
        # Approving in the same save as the evidence change means the reviewer checked the new evidence.
        approved = correct(draft, [{"path": "/testimonies/0/name", "review": "APPROVED"}],
                           [{"op": "set_evidence", "path": "/testimonies/0/name", "evidence": ["s02"]}])
        self.assertEqual(approved["testimonies"][0]["name"]["review"], "APPROVED")
        self.assertEqual(approved["testimonies"][0]["name"]["evidence"], ["s02"])
        changed = correct(changed, [{"path": "/questions/0/asker", "value": "Rep. Example", "review": "APPROVED"}],
                          [{"op": "set_question_speakers", "question": 0,
                            "asker_speaker": "Speaker 2", "respondent_speaker": "Speaker 3"}])
        question = changed["questions"][0]
        self.assertIsNone(question["asker"]["value"])
        self.assertIsNone(question["respondent"]["value"])
        self.assertEqual(question["question"]["review"], "NEEDS_REVIEW")
        self.assertEqual(changed["segments"], self.draft["segments"])

    def test_optional_organization_not_applicable_is_reviewable_and_clearable(self):
        draft = correct(self.draft, [{"path": "/testimonies/0/organization", "value": None,
                                     "not_applicable": True, "review": "APPROVED"}])
        self.assertIn("Organization: Not applicable", markdown(draft))
        self.assertIsNone(draft["testimonies"][0]["organization"]["value"])
        self.assertFalse(any(not f["value"] and f.get("not_applicable") and f["review"] != "APPROVED"
                             for _, f in fields(draft)))
        draft = correct(draft, [{"path": "/testimonies/0/organization", "value": None,
                                "not_applicable": False, "review": "UNRESOLVED"}])
        self.assertIn("Organization: Unknown [unresolved]", markdown(draft))
        with self.assertRaises(InputError):
            correct(draft, [{"path": "/testimonies/0/name", "value": None,
                             "not_applicable": True, "review": "APPROVED"}])

    def test_no_questions_requires_confirmation_and_valid_context(self):
        with self.assertRaisesRegex(InputError, "matching/ambiguous"):
            self.apply({"op": "confirm_no_questions", "testimony": 0, "statement": 0,
                        "confirmed": True, "evidence": ["s02"]})
        draft = self.apply({"op": "remove_question", "question": 0})
        self.assertIn("/testimonies/0/statements/0/questions_none", dict(fields(draft)))
        self.assertNotIn("None noted during this testimony.", markdown(draft))
        with self.assertRaisesRegex(InputError, "evidence"):
            correct(draft, operations=[{"op": "confirm_no_questions", "testimony": 0,
                                       "statement": 0, "confirmed": True, "evidence": []}])
        draft = correct(draft, operations=[{"op": "confirm_no_questions", "testimony": 0,
                                           "statement": 0, "confirmed": True, "evidence": ["s02"]}])
        self.assertIn("None noted during this testimony. [human confirmed]", markdown(draft))
        changed = correct(draft, [{"path": "/testimonies/0/statements/0/bill", "value": "S.2222"}])
        self.assertIsNone(changed["testimonies"][0]["statements"][0]["questions_none"]["value"])
        self.assertNotIn("None noted during this testimony.", markdown(changed))

    def test_ambiguous_question_blocks_none_confirmed(self):
        draft = correct(self.draft, [{"path": "/questions/0/bill", "value": None}])
        with self.assertRaisesRegex(InputError, "ambiguous"):
            correct(draft, operations=[{"op": "confirm_no_questions", "testimony": 0,
                                       "statement": 0, "confirmed": True, "evidence": ["s02"]}])
        self.assertIn("Questions needing testimony or bill attribution", markdown(draft))

    def test_no_questions_approval_cannot_bypass_explicit_evidence_confirmation(self):
        draft = self.apply({"op": "remove_question", "question": 0})
        draft = correct(draft, operations=[{"op": "confirm_no_questions", "testimony": 0,
                                           "statement": 0, "confirmed": True, "evidence": ["s02"]}])
        path = "/testimonies/0/statements/0/questions_none"
        for evidence in ([], ["s03"]):
            with self.subTest(evidence=evidence):
                changed = correct(draft, operations=[{"op": "set_evidence", "path": path,
                                                     "evidence": evidence}])
                self.assertEqual(changed["testimonies"][0]["statements"][0]["questions_none"]["review"], "NEEDS_REVIEW")
                with self.assertRaisesRegex(InputError, "explicit no-questions"):
                    correct(changed, [{"path": path, "review": "APPROVED"}])
                with self.assertRaises(InputError):
                    correct(changed, operations=[{"op": "confirm_no_questions", "testimony": 0,
                                                 "statement": 0, "confirmed": True, "evidence": evidence}])
        with self.assertRaisesRegex(InputError, "explicit no-questions"):
            correct(draft, [{"path": path, "review": "APPROVED"}])

    def test_removing_all_arguments_from_approved_note_makes_structure_unresolved(self):
        changes = [{"path": path, "review": "APPROVED"} for path, item in fields(self.draft) if item["value"]]
        changes.append({"path": "/questions/0/asker", "value": "Rep. Example", "review": "APPROVED"})
        draft = correct(self.draft, changes)
        self.assertEqual(review_summary(draft)["status"], "APPROVED")
        operations = [{"op": "remove_argument", "testimony": 0, "statement": 0, "argument": 0}
                      for _ in draft["testimonies"][0]["statements"][0]["arguments"]]
        changed = correct(draft, operations=operations)
        self.assertEqual(review_summary(changed)["status"], "UNRESOLVED")
        self.assertGreaterEqual(review_summary(changed)["unresolved"], 1)
        self.assertIn("No main argument detected; inspect the transcript.", markdown(changed))
        self.assertIn("Review: Unresolved", markdown(changed))

    def test_shared_testimony_retains_individual_records_and_conflicting_positions(self):
        draft = self.apply({"op": "add_testimony", "speaker": "Speaker 3", "name": "Second Testifier",
                            "organization": None, "evidence": ["s03"]})
        draft = correct(draft, [{"path": "/testimonies/1/statements/0/bill", "value": "H.1234"},
                                {"path": "/testimonies/1/statements/0/position", "value": "Support"}])
        original = copy.deepcopy(draft["testimonies"])
        draft = correct(draft, operations=[{"op": "group_testimonies", "testimonies": [0, 1]}])
        self.assertEqual(draft["testimonies"], original)
        self.assertIn("Testifiers: Alex Morgan [draft; review needed]; Second Testifier [draft; review needed]", markdown(draft))
        self.assertIn("Shared testimony: Shared testimony [draft; review needed]", markdown(draft))
        self.assertIn("Transcript speaker: Speaker 2", markdown(draft))
        self.assertIn("Transcript speaker: Speaker 3", markdown(draft))
        self.assertIn("Speaker 2", markdown(draft))
        conflicting = correct(draft, [{"path": "/testimonies/1/statements/0/position", "value": "Oppose"}])
        note = markdown(conflicting)
        self.assertEqual(note.count("## Bill: H.1234"), 2)
        self.assertIn("Position: Support", note)
        self.assertIn("Position: Oppose", note)
        self.assertIn("entries are shown separately", note)
        ungrouped = correct(conflicting, operations=[{"op": "ungroup_testimonies",
                                                    "group_id": conflicting["testimony_groups"][0]["id"]}])
        self.assertEqual(ungrouped["testimonies"], conflicting["testimonies"])
        self.assertFalse(ungrouped["testimony_groups"])

    def test_legacy_normalization_is_pure_and_deterministic(self):
        old = copy.deepcopy(self.draft)
        old.pop("testimony_groups")
        old["testimonies"][0].pop("id")
        original = copy.deepcopy(old)
        self.assertEqual(prepare_review(old), prepare_review(old))
        self.assertEqual(old, original)
        self.assertNotIn("audio not attached", markdown(old))

    def test_shared_bill_heading_keeps_every_member_review_and_correction_state(self):
        draft = self.apply({"op": "add_testimony", "speaker": "Speaker 3", "name": "Second Testifier",
                            "organization": None, "evidence": ["s03"]})
        draft = correct(draft, [{"path": "/testimonies/0/statements/0/bill", "review": "APPROVED"},
                                {"path": "/testimonies/1/statements/0/bill", "value": "H.1234"},
                                {"path": "/testimonies/1/statements/0/position", "value": "Support"}],
                        [{"op": "group_testimonies", "testimonies": [0, 1]}])
        note = markdown(draft)
        self.assertEqual(note.count("## Bill: H.1234"), 1)
        self.assertIn("## Bill: H.1234 [draft; review needed]", note)
        approved = correct(draft, [{"path": "/testimonies/1/statements/0/bill", "review": "APPROVED"}])
        self.assertIn("## Bill: H.1234 [human corrected]", markdown(approved))


if __name__ == "__main__":
    unittest.main()
