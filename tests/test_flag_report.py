"""The shadow spelling rule, and the report that compares flags with what the reviewer did."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from hearing_compiler import roster

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import flag_report  # noqa: E402

LEGISLATORS = [{"name": "Morgan Ellery"}, {"name": "Dana Whitcomb"}, {"name": "Riley Navarro"},
               {"name": "Sam Ortíz"}]


def name(value, flag, review="NEEDS_REVIEW"):
    return {"value": value, "flag": flag, "review": review, "evidence": [], "original_value": value}


def draft_with(*people, asker=None):
    unit = {"testifiers": [{"name": person} for person in people], "questions": []}
    if asker:
        unit["questions"] = [{"asker": {"name": asker}, "responses": []}]
    return {"units": [unit], "unassigned_questions": []}


class ShadowRuleTests(unittest.TestCase):
    def test_only_spelling_doubts_on_exact_official_names_are_marked_and_nothing_changes(self):
        cases = [
            (name("Morgan Ellery", "Transcript says 'Representative Elery' at cue 68; verified against "
                                           "official sponsor list as Morgan Ellery"), True),
            (name("Dana R. Whitcomb", "Spoken as 'Dan Witcom'; official agenda confirms Dana R. Whitcomb"), True),
            # Who spoke can't be settled by a list (on a real hearing, six "who asked is inferred" on Riley Navarro).
            (name("Riley Navarro", "Who asked is inferred from context; confirm by listening."), False),
            (name("Sam Ortíz", "Speaker self-identifies as filing S.1131 and chair addresses him as 'Senator'; "
                                "agenda lists Sam Ortíz as sponsor"), False),
            (name("Morgan Elleray", "Spoken as 'Elleray'; spelling unverified"), False),  # Not an exact match.
            (name("Dana Avery", "Spoken as 'Dana Avary'"), False),  # A public witness: no official list.
            (name("Morgan Ellery", "Spoken as 'Elery'", review="APPROVED"), False),
        ]
        draft = draft_with(*[field for field, _ in cases])
        before = copy.deepcopy(draft)
        self.assertEqual(roster.shadow_confirm(draft, LEGISLATORS), 2)
        for (field, expected), person in zip(cases, draft["units"][0]["testifiers"]):
            self.assertEqual("would_clear" in person["name"], expected, field["flag"])
        for old, new in zip(before["units"][0]["testifiers"], draft["units"][0]["testifiers"]):
            new["name"].pop("would_clear", None)
            self.assertEqual(old, new)  # Flag, value and review untouched.

    def test_agenda_sponsors_count_as_official_names_and_questioners_are_checked_too(self):
        draft = draft_with(asker=name("Jordan Hale", "Transcript says 'Jordan Hail'"))
        self.assertEqual(roster.shadow_confirm(draft, [], sponsors=["Jordan Hale"]), 1)
        self.assertEqual(draft["units"][0]["questions"][0]["asker"]["name"]["would_clear"]["matched"], "Jordan Hale")


class FlagReportTests(unittest.TestCase):
    def test_flags_are_counted_by_kind_against_her_final_decision_and_wrong_clears_are_shown(self):
        drafted = name("Morgan Ellery", "Spoken as 'Elery'")
        drafted["would_clear"] = {"rule": roster.SHADOW_RULE, "matched": "Morgan Ellery"}
        wrong = name("Dana Whitcomb", "Spoken as 'Dan Witcom'")
        wrong["would_clear"] = {"rule": roster.SHADOW_RULE, "matched": "Dana Whitcomb"}
        asked = name("Riley Navarro", "Who asked is inferred from context; confirm by listening.")
        log = [{"op": "correct_field", "path": "/units/0/testifiers/0/name", "before": drafted,
                "after": {**drafted, "review": "APPROVED"}},
               {"op": "correct_field", "path": "/units/0/testifiers/1/name", "before": wrong,
                "after": {**wrong, "value": "Dan Whitcomb", "review": "APPROVED"}},
               {"op": "correct_field", "path": "/units/0/questions/0/asker/name", "before": asked,
                "after": {**asked, "value": "Morgan Hale", "review": "APPROVED"}}]
        job = {"id": "j", "processing": "Complete", "hearing": {"title": "Example hearing"},
               "draft": {"review_log": log, "roster": {"would_clear": 2},
                         "segments": [{"id": "s1", "start": 0, "end": 3600}]}}
        lines = flag_report.report(job)
        self.assertIn("reviewed ⚠: 3, fixed 2 (67% precision); 3 ⚠ per hearing hour", lines[1])
        self.assertTrue(any(line.split()[:2] == ["name", "spelling"] and "2 ⚠" in line and "1 fixed" in line
                            for line in lines), lines)
        self.assertTrue(any("questioner inferred" in line and "1 fixed" in line for line in lines), lines)
        self.assertIn("marked 2; reviewed 2: 1 kept, 1 EDITED (a wrong clear)", lines[-1])
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "state.json").write_text(json.dumps({"jobs": {"j": job}}), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                flag_report.main([folder])
            self.assertIn("Example hearing (Complete)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
