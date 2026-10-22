from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path

from hearing_compiler.pipeline import InputError, correct, extract, fields, markdown, review_summary

ROOT = Path(__file__).resolve().parent.parent


def fixture(name):
    return json.loads((ROOT / "fixtures" / "transcripts" / f"{name}.json").read_text(encoding="utf-8"))


class ExtractionTests(unittest.TestCase):
    def test_independent_fixture_expectations(self):
        expected = json.loads((ROOT / "fixtures" / "expected.json").read_text(encoding="utf-8"))
        for name, facts in expected.items():
            if name == "note":
                continue
            with self.subTest(case=name):
                document = fixture(name)
                if "error_contains" in facts:
                    with self.assertRaisesRegex(InputError, facts["error_contains"]):
                        extract(document)
                    continue
                draft = extract(document)
                self.assertEqual([t["name"]["value"] for t in draft["testimonies"]], facts["witnesses"])
                positions = [{s["bill"]["value"]: s["position"]["value"] for s in t["statements"]}
                             for t in draft["testimonies"]]
                if "positions" in facts:
                    for bill, position in facts["positions"].items():
                        self.assertEqual(positions[0][bill], position)
                if "witness_positions" in facts:
                    self.assertEqual(positions, facts["witness_positions"])
                self.assertEqual([q["question"]["evidence"] for q in draft["questions"]], facts["question_evidence"])
                if "response_evidence" in facts:
                    self.assertEqual([q["response"]["evidence"] for q in draft["questions"]], facts["response_evidence"])
                if facts.get("unknown_asker"):
                    self.assertIsNone(draft["questions"][0]["asker"]["value"])
                if "asker" in facts:
                    self.assertEqual(draft["questions"][0]["asker"]["value"], facts["asker"])
                if facts.get("unknown_organization"):
                    self.assertIsNone(draft["testimonies"][0]["organization"]["value"])
                if facts.get("unknown_bill"):
                    self.assertTrue(any(s["bill"]["value"] is None for t in draft["testimonies"] for s in t["statements"]))
                if facts.get("unknown_question_bill"):
                    self.assertIsNone(draft["questions"][0]["bill"]["value"])
                if facts.get("unknown_response"):
                    self.assertIsNone(draft["questions"][0]["response"]["value"])
                self.assertGreaterEqual(len(draft["issues"]), facts.get("minimum_issues", 0))
                ids = {s["id"] for s in document["segments"]}
                for path, item in fields(draft):
                    self.assertTrue(set(item["evidence"]) <= ids, path)
                    self.assertNotEqual(item["review"], "APPROVED", path)
                    if item["value"] is not None and not path.startswith("/issues"):
                        self.assertTrue(item["evidence"], path)
                    if path.endswith("/bill") and item["value"]:
                        source = " ".join(s["text"] for s in document["segments"] if s["id"] in item["evidence"])
                        self.assertIn(item["value"], source, f"{path} must cite its bill context")

    def test_changed_names_and_bill_are_extracted_from_text(self):
        document = fixture("sample-hearing")
        for segment in document["segments"]:
            segment["text"] = segment["text"].replace("Alex Morgan", "Jamie Vale").replace("H.1234", "S.9876")
        draft = extract(document)
        self.assertEqual(draft["testimonies"][0]["name"]["value"], "Jamie Vale")
        self.assertEqual(draft["testimonies"][0]["statements"][0]["bill"]["value"], "S.9876")
        self.assertNotIn("H.1234", markdown(draft))

    def test_question_about_support_is_not_witness_testimony(self):
        document = fixture("sample-hearing")
        document["segments"][2]["text"] = "Why do you support H.1234?"
        draft = extract(document)
        self.assertEqual(len(draft["testimonies"]), 1)
        self.assertEqual(len(draft["questions"]), 1)

    def test_legislator_can_be_a_testifier_when_the_role_is_explicit(self):
        document = fixture("sample-hearing")
        document["segments"][0]["text"] = "I'm Representative Vale. I am here to testify in support of H.1234."
        draft = extract(document)
        self.assertEqual(draft["testimonies"][0]["name"]["value"], "Representative Vale")
        self.assertEqual(draft["testimonies"][0]["statements"][0]["position"]["value"], "Support")
        self.assertEqual(draft["questions"][0]["response"]["evidence"], ["s04"])

    def test_reference_format_keeps_bill_first_and_unknown_questions_unassigned(self):
        draft = extract(fixture("sample-hearing"))
        note = markdown(draft)
        self.assertLess(note.index("## Bill:"), note.index("Testifiers:"))
        self.assertLess(note.index("Testifiers:"), note.index("Position:"))
        self.assertIn("Main Arguments:", note)
        self.assertIn("### Legislator Questions", note)
        mixed = markdown(extract(fixture("mixed-position")))
        self.assertIn("Questions needing testimony or bill attribution", mixed)
        self.assertNotIn("None noted during this testimony.", mixed)

    def test_new_witness_does_not_answer_previous_witness_question(self):
        document = fixture("sample-hearing")
        document["segments"][3]["text"] = "I'm Jamie Vale with Example Civic Forum. I support S.2222."
        document["segments"][3]["speaker"] = "Speaker 4"
        draft = extract(document)
        self.assertIsNone(draft["questions"][0]["response"]["value"])
        self.assertEqual(len(draft["testimonies"]), 2)

    def test_missing_intro_does_not_create_identity(self):
        document = fixture("sample-hearing")
        document["segments"][0]["text"] = "I support H.1234."
        draft = extract(document)
        self.assertIsNone(draft["testimonies"][0]["name"]["value"])
        self.assertIn("Unknown [unresolved]", markdown(draft))

    def test_validation_rejects_bad_times_ids_and_nonsynthetic_input(self):
        invalids = [
            ("synthetic", False),
            ("segments", []),
        ]
        for key, value in invalids:
            document = fixture("sample-hearing")
            document[key] = value
            with self.assertRaises(InputError):
                extract(document)
        for value in (-1, math.inf, math.nan, True, "01:00"):
            document = fixture("sample-hearing")
            document["segments"][0]["start"] = value
            with self.assertRaises(InputError):
                extract(document)
        document = fixture("sample-hearing")
        document["segments"][1]["id"] = document["segments"][0]["id"]
        with self.assertRaisesRegex(InputError, "Duplicate"):
            extract(document)

    def test_correction_retains_source_and_changes_export(self):
        draft = extract(fixture("sample-hearing"))
        original = copy.deepcopy(draft)
        changed = correct(draft, [{"path": "/questions/0/asker", "value": "Rep. Avery", "review": "APPROVED"}])
        self.assertEqual(draft, original)
        self.assertEqual(changed["questions"][0]["asker"]["original_value"], None)
        self.assertTrue(changed["questions"][0]["asker"]["corrected"])
        self.assertIn("Rep. Avery [human corrected]", markdown(changed))
        self.assertIn("01:24:12", markdown(changed))
        self.assertIn("[draft; review needed]", markdown(changed))
        self.assertEqual(changed["segments"], original["segments"])

    def test_approval_requires_resolution_and_all_fields(self):
        draft = extract(fixture("sample-hearing"))
        with self.assertRaises(InputError):
            correct(draft, [{"path": "/questions/0/asker", "review": "APPROVED"}])
        supported = [{"path": path, "review": "APPROVED"} for path, item in fields(draft) if item["value"]]
        changed = correct(draft, supported)
        self.assertNotEqual(review_summary(changed)["status"], "APPROVED")
        changed = correct(changed, [{"path": "/questions/0/asker", "value": "Rep. Avery", "review": "APPROVED"}])
        self.assertEqual(review_summary(changed)["status"], "APPROVED")
        self.assertNotIn("[draft; review needed]", markdown(changed))

    def test_invalid_corrections_are_atomic_and_do_not_escape_field_scope(self):
        draft = extract(fixture("sample-hearing"))
        original = copy.deepcopy(draft)
        with self.assertRaises(InputError):
            correct(draft, [
                {"path": "/testimonies/0/name", "value": "New Person"},
                {"path": "/segments/0/text", "value": "Altered evidence"},
            ])
        self.assertEqual(draft, original)
        with self.assertRaises(InputError):
            correct(draft, [{"path": "/testimonies/0/statements/0/position", "value": "Probably yes"}])

    def test_markdown_does_not_turn_user_text_into_html(self):
        draft = extract(fixture("sample-hearing"))
        draft = correct(draft, [{"path": "/testimonies/0/name", "value": "<script>alert(1)</script>"}])
        self.assertNotIn("<script>", markdown(draft))


if __name__ == "__main__":
    unittest.main()
