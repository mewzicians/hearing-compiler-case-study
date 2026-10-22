"""The trial must report citation failures honestly and never persist the API key."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import gemini_hearing_trial as trial

SRT = ("\ufeff1\n00:00:01,000 --> 00:00:04,000\nWe now call House Bill 1885.\n\n"
       "2\n00:00:04,000 --> 00:00:04,000\n\n\n"
       "3\n00:00:04,000 --> 00:00:09,000\nI'm Dana Reyes. I support the bill because notice matters.\n\n"
       "4\n00:00:09,000 --> 00:01:30,000\nUnrelated discussion of H-1911 and Senate number 1236, plus S 999.\n")
AGENDA = {"hearing": {"title": "Fictional hearing", "date": "2025-01-01", "committee": "Example Committee"},
          "agenda": [{"bill": "H.1885", "title": "An Act about notice", "sponsor": "A. Sponsor"},
                     {"bill": "H.1911", "title": "An Act about trusts", "sponsor": "B. Sponsor"},
                     {"bill": "S.1236", "title": "An Act about alimony", "sponsor": "C. Sponsor"}]}


def entry(**changes):
    base = {"start_cue": 1, "end_cue": 2, "bill": "H.1885", "bill_title": None, "bill_cues": [1], "bill_check": None,
            "testifiers": [{"name": "Dana Reyes", "role": None, "organization": None, "cues": [2], "check": None}],
            "position": "Support", "position_cues": [2], "position_check": None,
            "arguments": [{"text": 'Said "notice matters" for families.', "cues": [2], "check": None}],
            "questions": [], "questions_note": None}
    base.update(changes)
    return base


class GeminiTrialTests(unittest.TestCase):
    def setUp(self):
        self.cues = trial.parse_srt(SRT)

    def test_blank_cues_are_skipped_and_spoken_bill_forms_map_to_the_agenda(self):
        self.assertEqual([c["n"] for c in self.cues], [1, 2, 3])
        found = trial.mentioned_bills(self.cues, {b["bill"] for b in AGENDA["agenda"]})
        self.assertEqual(found, {"H.1885": [1], "H.1911": [3], "S.1236": [3]})

    def test_clean_draft_has_no_problems_but_reports_missed_bills_and_silence(self):
        checks = trial.check({"entries": [entry()], "procedural": []}, self.cues, AGENDA)
        self.assertEqual(checks["problems"], [])
        self.assertEqual(sorted(checks["spoken_agenda_bills_missing_from_draft"]), ["H.1911", "S.1236"])
        self.assertEqual([gap["cues"] for gap in checks["uncovered_spans_20s_or_more"]], ["3-3"])

    def test_bad_citations_unflagged_guesses_and_misquotes_are_reported(self):
        bad = entry(bill="H.4040", end_cue=3,
                    arguments=[{"text": 'Said "families deserve better notice".', "cues": [2], "check": None},
                               {"text": "Cited a cue that does not exist.", "cues": [99], "check": None},
                               {"text": "No source at all.", "cues": [], "check": None}])
        outside = entry(start_cue=1, end_cue=1)
        problems = trial.check({"entries": [bad, outside], "procedural": []}, self.cues, AGENDA)["problems"]
        text = "\n".join(problems)
        self.assertIn("H.4040 is not on the agenda and is not flagged", text)
        self.assertIn("quote not found", text)
        self.assertIn("cites missing cues [99]", text)
        self.assertIn("no cited cues and no check flag", text)
        self.assertIn("outside its range 1-1", text)

    def test_quotes_correct_drifted_cue_numbers_and_unfound_quotes_are_flagged(self):
        drifted = entry(start_cue=2, end_cue=3, bill_quote="We now call House Bill 1885", bill_cues=[2],
                        testifiers=[{"name": "Dana Reyes", "role": None, "organization": None,
                                     "quote": "House Bill 1885. I'm Dana Reyes", "cues": [3], "check": None}],
                        position_quote="I support the bill because notice", position_cues=[3],
                        arguments=[{"text": "Notice matters.", "quote": "notice is the most important thing",
                                    "cues": [3], "check": None}])
        draft = {"entries": [drifted], "procedural": []}
        self.assertEqual(trial.resolve(draft, self.cues), {"quotes": 4, "found": 3, "moved": 3})
        resolved = draft["entries"][0]
        self.assertEqual((resolved["bill_cues"], resolved["testifiers"][0]["cues"], resolved["position_cues"]),
                         ([1], [1, 2], [2]))
        self.assertEqual(resolved["model_bill_cues"], [2])
        self.assertIn("quote not found", resolved["arguments"][0]["check"])
        self.assertEqual((resolved["start_cue"], resolved["end_cue"]), (1, 2))
        self.assertNotIn("quote not found", "\n".join(trial.check(draft, self.cues, AGENDA)["problems"]))

    def test_split_entries_are_rejoined_without_dropping_content(self):
        whole = entry()
        bill_half = {k: whole[k] for k in ("start_cue", "end_cue", "bill", "bill_title", "bill_cues", "bill_check")}
        testimony_half = {k: v for k, v in whole.items() if k not in bill_half}
        extra = {"arguments": [{"text": "Another point.", "cues": [2], "check": None}], "questions": []}
        draft = {"entries": [bill_half, testimony_half, extra], "procedural": []}
        self.assertEqual(trial.merge_fragments(draft), 2)
        self.assertEqual(len(draft["entries"]), 1)
        rejoined = draft["entries"][0]
        self.assertEqual({k: v for k, v in rejoined.items() if k != "arguments"},
                         {k: v for k, v in whole.items() if k != "arguments"})
        self.assertEqual([a["text"] for a in rejoined["arguments"]],
                         [whole["arguments"][0]["text"], "Another point."])

    def test_busy_responses_are_retried_and_other_errors_stop(self):
        def busy(code):
            return trial.urllib.error.HTTPError("url", code, "busy", {}, io.BytesIO(b'{"error": "busy"}'))

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        replies = [busy(503), busy(429), Response(b'{"candidates": []}')]

        def fake_urlopen(request, timeout):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(trial.urllib.request, "urlopen", fake_urlopen), \
                patch.object(trial.time, "sleep") as sleep, patch("builtins.print"):
            payload, _ = trial.call_gemini("prompt", "gemini-test", "key", 1000)
            self.assertEqual(payload, {"candidates": []})
            self.assertEqual([c.args[0] for c in sleep.call_args_list], list(trial.RETRY_WAITS[:2]))
            replies[:] = [busy(403)]
            with self.assertRaisesRegex(SystemExit, "HTTP 403"):
                trial.call_gemini("prompt", "gemini-test", "key", 1000)

    def test_run_saves_draft_notes_and_checks_without_the_key(self):
        draft = {"entries": [entry(bill_check="Bill number heard once; confirm")], "procedural": [
            {"start_cue": 3, "end_cue": 3, "summary": "Other business"}]}
        payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "thinking", "thought": True}, {"text": json.dumps(draft)}]}}],
            "usageMetadata": {"promptTokenCount": 100}, "modelVersion": "test"}
        sent = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            sent["key"] = request.get_header("X-goog-api-key")
            sent["body"] = json.loads(request.data)
            return Response(json.dumps(payload).encode())

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "h.srt").write_text(SRT, encoding="utf-8")
            (root / "agenda.json").write_text(json.dumps(AGENDA), encoding="utf-8")
            with patch.dict(os.environ, {"GEMINI_API_KEY": "secret-test-key"}), \
                    patch.object(trial.urllib.request, "urlopen", fake_urlopen), patch("builtins.print"):
                trial.main(["run", "--srt", str(root / "h.srt"), "--agenda", str(root / "agenda.json"),
                            "--model", "gemini-test", "--out", str(root / "out")])
            (run,) = (root / "out").iterdir()
            self.assertEqual(sent["key"], "secret-test-key")
            self.assertEqual(sent["body"]["generationConfig"]["responseMimeType"], "application/json")
            self.assertEqual(json.loads((run / "draft.json").read_text(encoding="utf-8")), draft)
            self.assertEqual(json.loads((run / "checks.json").read_text(encoding="utf-8"))["problems"], [])
            notes = (run / "notes.md").read_text(encoding="utf-8")
            self.assertIn("## Bill: H.1885 — An Act about notice [00:00:01] ⚠ check: Bill number heard once", notes)
            for path in run.iterdir():
                self.assertNotIn("secret-test-key", path.read_text(encoding="utf-8"))
