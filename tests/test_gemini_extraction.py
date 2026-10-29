"""Gemini drafts must stay reviewable, cite real segments, flag doubt and never persist the key."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from hearing_compiler import audio_check, extraction, gemini, parts, second_check
from hearing_compiler.audio import AudioQueue
from hearing_compiler.pipeline import InputError, fields
from hearing_compiler.server import make_server
from hearing_compiler import store as store_module
from hearing_compiler.store import Store
from test_audio import eventually
from test_pipeline import fixture

KEY = "AIzaTESTKEY-0123456789abcdefghij"
# Split the way Gemini sometimes does: bill half, then testimony half, then an empty entry.
REPLY = {"entries": [
    {"start_cue": 1, "end_cue": 4, "bill": "H.1234", "bill_title": None,
     "bill_quote": "Example Legal Services. I support H.1234", "bill_cues": [3], "bill_check": None},
    {"testifiers": [{"name": "Alex Morgan", "role": None, "organization": "Example Legal Services",
                     "quote": "I'm Alex Morgan with Example Legal Services", "cues": [1],
                     "check": "Name heard once; confirm spelling"}],
     "position": "Support", "position_quote": "I support H.1234", "position_cues": [1], "position_check": None,
     "arguments": [{"text": "Expands access to legal assistance.",
                    "quote": "The proposal would expand access to legal assistance", "cues": [1], "check": None},
                   {"text": "Simplifies eligibility.", "quote": "eligibility would be simpler for everyone",
                    "cues": [2], "check": None}],
     "questions": [{"asker": "Unidentified member", "question": "Who would qualify for assistance?",
                    "respondent": "Alex Morgan", "response": "Residents who meet the bill's income criteria.",
                    "quote": "Who would qualify for assistance", "cues": [3], "check": None}],
     "questions_note": None},
    {"start_cue": 4, "end_cue": 4, "bill": "H.9999", "bill_quote": "income criteria", "bill_cues": [4]},
], "procedural": []}


class Keys:
    def __init__(self):
        self.value = None

    def available(self):
        return True

    def get(self):
        return self.value

    def set(self, key):
        self.value = key

    def delete(self):
        self.value = None


class Client:
    def __init__(self, reply=REPLY, failure=None, check_reply=None):
        self.reply, self.failure, self.prompts, self.checked = reply, failure, [], []
        self.check_reply, self.second = check_reply or {"items": [], "omissions": []}, []
        self.audio_reply, self.audio = {"items": []}, []

    def check_key(self, key, model, **_):
        self.checked.append(key)
        if key.startswith("AIzaREJECTED"):
            raise gemini.GeminiError("Gemini rejected the key or model (HTTP 400).")

    def call_gemini(self, prompt, model, key, log=None, **options):
        reply = self.reply
        if second_check.is_check(prompt):  # Counted apart from drafting calls.
            self.second.append(prompt)
            reply = self.check_reply(prompt) if callable(self.check_reply) else self.check_reply
        elif audio_check.is_check(prompt):  # So are audio checks, which carry their clips in parts.
            self.audio.append(options.get("parts"))
            reply = self.audio_reply(options["parts"]) if callable(self.audio_reply) else self.audio_reply
        else:
            self.prompts.append(prompt)
            if self.failure:
                raise gemini.GeminiError(self.failure)
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(reply)}]}}],
                "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 300}, "modelVersion": model}, 12.5


class GeminiExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.store = Store(self.folder)
        self.keys, self.client = Keys(), Client()
        self.service = extraction.ExtractionService(self.store, threading.Event(), self.keys, self.client)
        self.store.extractor = self.service
        self.queues = []

    def tearDown(self):
        for queue in self.queues:
            queue.close()
        self.temporary.cleanup()

    def queue(self):
        queue = AudioQueue(self.store)
        self.store.on_queued = queue.wake.set
        self.queues.append(queue)
        return queue

    def test_key_is_checked_kept_out_of_saved_state_and_removable(self):
        with self.assertRaisesRegex(InputError, "Paste the Gemini API key"):
            self.service.configure("gemini")
        with self.assertRaisesRegex(InputError, "rejected"):
            self.service.configure("gemini", api_key="AIzaREJECTED-0123456789abcdefgh")
        self.assertIsNone(self.keys.value)
        info = self.service.configure("gemini", api_key=f"  {KEY}  ")
        self.assertEqual((info["provider"], info["key_saved"], self.keys.value), ("gemini", True, KEY))
        self.assertNotIn(KEY, json.dumps(info))
        self.assertNotIn(KEY, self.store.path.read_text(encoding="utf-8"))
        self.assertTrue(self.service.background)
        self.assertEqual(self.service.configure("gemini")["key_saved"], True)  # The saved key is reused.
        info = self.service.forget_key()
        self.assertEqual((info["provider"], info["key_saved"]), ("rules", False))

    def test_gemini_draft_runs_in_the_queue_and_becomes_a_reviewable_draft(self):
        self.service.configure("gemini", api_key=KEY)
        queued = self.store.import_one(fixture("sample-hearing"))
        self.assertEqual(queued["processing"], "Waiting")
        self.queue()
        eventually(lambda: self.store.get("sample-hearing")["processing"] == "Complete", timeout=10)
        self.assertIn("1 [01:23:14] I'm Alex Morgan with Example Legal Services.", self.client.prompts[0])
        draft = Store(self.folder).get("sample-hearing")["draft"]  # Survives restart validation.
        self.assertEqual(draft["draft_format"], "hearing-review-units/v1")
        (unit,) = draft["units"]
        self.assertEqual(unit["segment_ids"], ["s01", "s02", "s03", "s04"])
        self.assertEqual((unit["bill"]["value"], unit["bill"]["evidence"]), ("H.1234", ["s01"]))
        self.assertEqual(unit["testifiers"][0]["name"]["value"], "Alex Morgan")
        self.assertEqual(unit["position"]["evidence"], ["s01"])
        self.assertEqual([a["text"]["evidence"] for a in unit["arguments"]], [["s02"], ["s02"]])
        self.assertEqual(unit["questions"][0]["responses"][0]["text"]["value"],
                         "Residents who meet the bill's income criteria.")
        reviews = {item["review"] for item in (unit["bill"], unit["position"], unit["arguments"][0]["text"])}
        self.assertEqual(reviews, {"NEEDS_REVIEW"})  # Nothing from the AI arrives approved.
        # Flags sit on the field they are about; whole-hearing problems stay in issues.
        self.assertEqual(unit["testifiers"][0]["name"]["flag"], "Name heard once; confirm spelling")
        self.assertIn("quote not found", unit["arguments"][1]["text"]["flag"])
        self.assertNotIn("flag", unit["arguments"][0]["text"])
        issues = " | ".join(issue["value"] for issue in draft["issues"])
        self.assertIn("H.9999", issues)  # The empty entry is reported, not silently dropped.
        self.assertNotIn("Name heard once", issues)
        self.assertEqual(draft["extraction_provenance"]["split_entries_rejoined"], 1)
        markdown = Store(self.folder).get("sample-hearing")["markdown"]
        self.assertIn("H.1234", markdown)
        self.assertIn("Alex Morgan", markdown)

    def test_gemini_failure_is_a_retryable_failed_hearing(self):
        self.service.configure("gemini", api_key=KEY)
        self.client.failure = "Gemini returned HTTP 503: busy"
        self.store.import_one(fixture("sample-hearing"))
        self.queue()
        eventually(lambda: self.store.get("sample-hearing")["processing"] == "Failed", timeout=10)
        self.assertIn("Gemini drafting failed", self.store.get("sample-hearing")["error"])
        self.client.failure = None
        self.store.retry("sample-hearing")
        eventually(lambda: self.store.get("sample-hearing")["processing"] == "Complete", timeout=10)

    def test_retired_local_model_settings_fall_back_to_the_rule_baseline(self):
        self.store.set_extraction({"provider": "ollama", "endpoint": "http://127.0.0.1:11434", "model": "old:4b"})
        self.assertEqual((self.service.info()["provider"], self.service.info()["retired"]), ("rules", "ollama"))
        self.assertFalse(self.service.background)
        result = self.store.import_one(fixture("sample-hearing"))
        self.assertEqual(result["processing"], "Complete")
        self.assertNotIn("draft_format", self.store.get("sample-hearing")["draft"])
        self.assertEqual(self.client.prompts, [])

    def test_http_configuration_never_echoes_the_key(self):
        server = make_server(self.folder / "http", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            token = json.loads(urlopen(base + "/api/session").read())["token"]
            keys = Keys()
            with patch.object(extraction, "KeychainKeys", return_value=keys), \
                    patch.object(server.store.extractor, "keys", keys), \
                    patch.object(server.store.extractor, "client", Client()):
                body = json.dumps({"provider": "gemini", "model": "gemini-3.8-flash", "api_key": KEY}).encode()
                reply = urlopen(Request(base + "/api/extraction/configure", data=body, headers={
                    "Content-Type": "application/json", "X-Review-Token": token})).read().decode()
                info = urlopen(base + "/api/extraction").read().decode()
            self.assertNotIn(KEY, reply + info)
            self.assertEqual(json.loads(reply)["provider"], "gemini")
            self.assertEqual(keys.value, KEY)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bills_missing_from_the_loaded_agenda_are_flagged(self):
        agenda = [{"bill": b, "title": "An Act about legal help", "sponsor": "A. Sponsor"} for b in ("H.1234", "S.1234")]
        document = fixture("sample-hearing")
        document["hearing_url"] = "https://malegislature.gov/Events/Hearings/Detail/1234"

        def units(*bills):
            reply = {"entries": [{"start_cue": 1, "end_cue": 4, "bill": bill, "bill_quote": "I support H.1234",
                                  "testifiers": [{"name": "Alex Morgan", "quote": "I'm Alex Morgan with Example Legal Services"}],
                                  "position": "Support", "position_quote": "I support H.1234",
                                  "arguments": [{"text": "Expands access.", "quote": "The proposal would expand access"}]}
                                 for bill in bills]}
            return extraction.gemini_draft(document, KEY, "gemini-3.8-flash", Client(reply),
                                           fetch_agenda=lambda url: {"url": url, "bills": agenda})["units"]
        (misheard,) = units("H.4321")  # The model wrote a number that isn't on the agenda and didn't flag it.
        self.assertEqual(misheard["bill"]["flag"], "This testimony also mentions H.1234; check which points belong to "
                                                   "which bill. Not on this hearing's agenda; check the number against "
                                                   "the audio.")
        (companions,) = units("H.1234", "S.1234")
        self.assertEqual(companions["bill"]["value"], "H.1234 / S.1234")
        self.assertNotIn("flag", companions["bill"])
        # Speech recognition can run companion numbers together; both still count as mentioned.
        self.assertEqual(gemini.mentioned_bills([{"n": 7, "text": "Samantha Fine on H1852S1164."}]),
                         {"H.1852": [7], "S.1164": [7]})

    def test_questioners_are_flagged_unless_their_name_is_said_nearby(self):
        def asker(name, check=None, heard=None):
            document = fixture("sample-hearing")
            if heard:
                document["segments"][1]["text"] += " " + heard
            reply = {"entries": [{**REPLY["entries"][1], **REPLY["entries"][0], "questions": [
                {"asker": name, "question": "Who would qualify for assistance?", "respondent": "Alex Morgan",
                 "response": "Residents who meet the bill's income criteria.",
                 "quote": "Who would qualify for assistance", "cues": [3], "check": check}]}]}
            return extraction.gemini_draft(document, KEY, "gemini-3.8-flash", Client(reply))["units"][0]["questions"][0]
        guessed = asker("Dana Avery", check="Question partly garbled")
        self.assertEqual(guessed["asker"]["name"]["flag"], "Who asked is inferred from context; confirm by listening.")
        self.assertEqual(guessed["question"]["flag"], "Question partly garbled")  # Not copied onto the questioner.
        self.assertNotIn("flag", asker("Representative Avery", heard="Thank you. Representative Avery?")["asker"]["name"])
        self.assertNotIn("flag", asker("Unidentified member")["asker"]["name"])


class GeminiDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_dropped_and_cut_off_connections_retry_but_certificate_and_quota_errors_stop(self):
        import io
        import ssl
        import urllib.error

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def run(replies, waits=None):
            def fake(request, timeout, **_):
                reply = replies.pop(0)
                if isinstance(reply, BaseException):
                    raise reply
                return reply
            waits = [] if waits is None else waits
            with patch.object(gemini.urllib.request, "urlopen", fake):
                return gemini.call_gemini("p", "m", "k", wait=waits.append, log=lambda _: None), waits

        def refusal(code, message):
            body = json.dumps({"error": {"code": code, "message": message}}).encode()
            return urllib.error.HTTPError("u", code, "", {}, io.BytesIO(body))

        (payload, _), waits = run([ConnectionResetError("reset"), Response(b'{"cand'), Response(b'{"ok": 1}')])
        self.assertEqual((payload, waits), ({"ok": 1}, [15, 30]))
        (payload, _), waits = run([refusal(503, "The model is overloaded."), Response(b'{"ok": 1}')])
        self.assertEqual((payload, waits), ({"ok": 1}, [15]))
        quota = urllib.error.HTTPError("u", 429, "", {}, io.BytesIO(b'{"message": "Quota exceeded per day"}'))
        certificate = urllib.error.URLError(ssl.SSLCertVerificationError("bad certificate"))
        for failure, message in ((quota, "quota"), (certificate, "securely")):
            with self.subTest(message), self.assertRaisesRegex(gemini.GeminiError, message):
                run([failure, Response(b"{}")])
        # Google's spend-cap and prepaid-credit wording: waiting can't clear these, so one request and no waits.
        for code, message in (
                (429, "Your project has exceeded its monthly spending cap. Please go to AI Studio at "
                      "https://ai.studio/spend to manage your project spend cap."),
                (429, "Your prepayment credits are depleted. Please go to AI Studio at https://ai.studio/projects "
                      "to manage your project and billing."),
                (402, "Your prepayment credits are depleted.")):
            replies, waits = [refusal(code, message), Response(b"{}")], []
            with self.subTest(code=code, message=message[:30]):
                with self.assertRaisesRegex(gemini.GeminiError, "^Gemini refused this key because its project "
                                                                "reached its monthly spending cap or ran out of"):
                    run(replies, waits)
                self.assertEqual((len(replies), waits), (1, []))

    def test_odd_model_output_never_crashes_drafting(self):
        odd = [
            {"entries": [{"bill": ["H.1234"], "testifiers": "Alex Morgan", "arguments": ["Expands access."],
                          "questions": "Who qualifies?", "position": None, "bill_cues": 1, "start_cue": "1",
                          "end_cue": 4}]},
            [{"bill": "H.1234", "testifiers": [{"name": {"first": "Alex"}, "cues": "1"}], "arguments": [7]}],
            {"entries": [{"bill": "H.1234"}, {"testifiers": [None, 3], "arguments": "Expands access.",
                                               "questions": [{"question": ["x"], "cues": [True, "2"]}]}],
             "procedural": "opening"},
        ]
        document = fixture("sample-hearing")
        for reply in odd:
            with self.subTest(reply=str(reply)[:60]):
                draft = extraction.gemini_draft(document, KEY, "gemini-3.8-flash", Client(reply))
                self.assertTrue(draft["units"])

    def test_saved_reply_is_reused_and_cut_off_replies_are_not(self):
        document, cache = fixture("sample-hearing"), self.folder / "extraction-output"
        client = Client()
        first = extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client, cache_dir=cache)
        again = extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client, cache_dir=cache)
        self.assertEqual((len(client.prompts), len(list(cache.glob("*.json")))), (1, 1))
        self.assertEqual((first["extraction_provenance"]["reused_saved_reply"],
                          again["extraction_provenance"]["reused_saved_reply"]), (False, True))
        (cache / next(cache.glob("*.json")).name).write_text(json.dumps(
            {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{"}]}}]}), encoding="utf-8")
        extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client, cache_dir=cache)
        self.assertEqual(len(client.prompts), 2)

    def test_agenda_from_the_hearing_link_feeds_the_prompt_and_failures_are_flagged(self):
        document = fixture("sample-hearing")
        document["hearing"]["hearing_url"] = "https://malegislature.gov/Events/Hearings/Detail/1234"
        client = Client()
        bills = [{"bill": "H.1234", "title": "An Act about legal help", "sponsor": "A. Sponsor"}]
        draft = extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client,
                                        fetch_agenda=lambda url: {"url": url, "bills": bills})
        self.assertIn("H.1234 | An Act about legal help | sponsor: A. Sponsor", client.prompts[0])
        self.assertEqual(draft["units"][0]["bill_title"]["value"], "An Act about legal help")
        self.assertEqual(draft["extraction_provenance"]["agenda_bills"], 1)

        def offline(url):
            raise InputError("Could not load the hearing page: offline")
        draft = extraction.gemini_draft(document, KEY, "gemini-3.8-flash", Client(), fetch_agenda=offline)
        self.assertIn("agenda couldn't be loaded", " ".join(i["value"] for i in draft["issues"]))

    def test_a_cut_off_or_garbled_hearing_page_drafts_without_the_agenda(self):
        import http.client
        from hearing_compiler import agenda

        class CutOff:  # A chunked page whose download stops partway.
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, amount=None):
                raise http.client.IncompleteRead(b"<tr><td>H.1234", 500)

        document = fixture("sample-hearing")
        document["hearing"]["hearing_url"] = "https://malegislature.gov/Events/Hearings/Detail/1234"
        for name, opened in (("cut off", CutOff()), ("not HTTP", http.client.BadStatusLine("SSH-2.0-proxy"))):
            def urlopen(request, timeout, context=None):
                if isinstance(opened, BaseException):
                    raise opened
                return opened
            with self.subTest(name), patch.object(agenda.urllib.request, "urlopen", urlopen):
                client = Client()
                draft = extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client)
                self.assertEqual((len(draft["units"]), len(client.prompts)), (1, 1))
                self.assertIn("agenda couldn't be loaded", " ".join(i["value"] for i in draft["issues"]))

    def test_one_testimony_on_several_bills_becomes_one_entry_only_when_positions_agree(self):
        person = {"name": "Joe Calloway", "quote": "I'm Alex Morgan with Example Legal Services", "cues": [1]}
        def entry(bill, position, argument, start, end):
            return {"start_cue": start, "end_cue": end, "bill": bill, "bill_title": "An Act about legal help",
                    "bill_quote": "I support H.1234",
                    "bill_cues": [1], "testifiers": [dict(person)], "position": position,
                    "position_quote": "I support H.1234", "position_cues": [1],
                    "arguments": [{"text": argument, "quote": "The proposal would expand access", "cues": [2]}],
                    "questions": [], "questions_note": "None asked."}
        bills = ["H.1234", "S.1234", "H.1235", "S.1235", "H.1236", "S.1236"]  # Longer than the old 40-character cut.
        same = {"entries": [entry(bill, "Support", "Expands access.", 1 + i % 2, 4) for i, bill in enumerate(bills)],
                "procedural": []}
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client(same))
        (unit,) = draft["units"]
        self.assertEqual(unit["bill"]["value"], "H.1234 / S.1234 / H.1235 / S.1235 / H.1236 / S.1236")
        self.assertEqual(unit["bill_title"]["value"], "An Act about legal help")
        self.assertEqual(len(unit["arguments"]), 1)  # The repeated argument isn't duplicated.
        mixed = {"entries": [entry("H.1234", "Support", "Expands access.", 1, 2),
                             entry("S.1234", "Oppose", "Too costly.", 2, 4)], "procedural": []}
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client(mixed))
        self.assertEqual([u["position"]["value"] for u in draft["units"]], ["Support", "Oppose"])

    def test_testimony_on_bills_that_are_not_companions_stays_separate(self):
        def entry(bill, title, organization=None):
            return {"start_cue": 1, "end_cue": 4, "bill": bill, "bill_title": title, "bill_quote": "I support H.1234",
                    "testifiers": [{"name": "Joe Calloway", "organization": organization,
                                    "quote": "I'm Alex Morgan with Example Legal Services", "cues": [1]}],
                    "position": "Support", "position_quote": "I support H.1234",
                    "arguments": [{"text": f"Point about {bill}.", "quote": "The proposal would expand access"}]}
        cases = {"different titles": [entry("H.1234", "An Act about legal help"), entry("S.5678", "An Act about court fees")],
                 "no titles": [entry("H.1234", None), entry("S.5678", None)],
                 "one title missing": [entry("H.1234", "An Act about legal help"), entry("S.5678", None)],
                 "another capacity": [entry("H.1234", "An Act about legal help", "Example Legal Services"),
                                      entry("S.5678", "An Act about legal help", "Another Advocacy Group")]}
        for name, entries in cases.items():
            with self.subTest(name):
                draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash",
                                                Client({"entries": entries}))
                self.assertEqual([u["bill"]["value"] for u in draft["units"]], ["H.1234", "S.5678"])
                self.assertEqual([[a["text"]["value"] for a in u["arguments"]] for u in draft["units"]],
                                 [["Point about H.1234."], ["Point about S.5678."]])

    def test_merged_companion_bills_keep_the_later_entrys_flags_and_organization(self):
        from hearing_compiler.rich_review import notion
        def entry(bill, title, **later):
            person = {"name": "Alex Morgan", "organization": later.get("organization"), "check": later.get("check"),
                      "quote": "I'm Alex Morgan with Example Legal Services", "cues": [1]}
            return {"start_cue": 1, "end_cue": 4, "bill": bill, "bill_title": title, "bill_quote": "I support H.1234",
                    "testifiers": [person], "position": "Support",
                    "position_quote": later.get("position_quote", "I support H.1234"),
                    "position_check": later.get("position_check"),
                    "arguments": [{"text": "Expands access.", "quote": "The proposal would expand access"}]}
        reply = {"entries": [entry("H.1234", "An Act relative to legal help"),
                             entry("S.1234", "An Act establishing legal help", organization="Example Legal Services",
                                   check="Name heard once; confirm spelling", position_check="Position inferred from tone",
                                   position_quote="It would make eligibility easier to understand")]}
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client(reply))
        (unit,) = draft["units"]  # Companions: the titles differ only by "relative to" and "establishing".
        person = unit["testifiers"][0]
        self.assertEqual(unit["bill"]["value"], "H.1234 / S.1234")
        self.assertEqual(unit["position"]["flag"], "S.1234: Position inferred from tone")
        self.assertEqual(unit["position"]["evidence"], ["s01", "s02"])  # Each bill's position statement.
        self.assertEqual(person["name"]["flag"], "S.1234: Name heard once; confirm spelling")
        self.assertEqual(person["organization"]["value"], "Example Legal Services")
        self.assertIn("Position: Support ⚠", notion(draft))

    def test_a_q_and_a_that_companion_entries_repeat_in_other_words_is_kept_and_flagged(self):
        from hearing_compiler.rich_review import notion
        def entry(bill, questions):
            return {"start_cue": 1, "end_cue": 4, "bill": bill, "bill_title": "An Act about legal help",
                    "bill_quote": "I support H.1234",
                    "testifiers": [{"name": "Alex Morgan", "quote": "I'm Alex Morgan with Example Legal Services",
                                    "cues": [1]}],
                    "position": "Support", "position_quote": "I support H.1234",
                    "arguments": [{"text": "Expands access.", "quote": "The proposal would expand access"}],
                    "questions": questions}
        asked = {"asker": "Unidentified member", "respondent": "Alex Morgan",
                 "quote": "Who would qualify for assistance", "cues": [3]}
        other = {"asker": "Unidentified member", "respondent": "Alex Morgan", "response": None,
                 "quote": "It would make eligibility easier to understand", "cues": [2]}
        reply = {"entries": [
            entry("H.1234", [{**asked, "question": "Asked who would qualify.", "response": "Residents under the limit."}]),
            entry("S.1234", [{**asked, "question": "Asked who is eligible for help.", "response": "Income-eligible residents."},
                             {**other, "question": "Asked whether eligibility gets simpler."},
                             {**other, "question": "Asked whether the forms get shorter."}])]}
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client(reply))
        (unit,) = draft["units"]
        questions = [q["question"] for q in unit["questions"]]
        self.assertEqual([q["value"] for q in questions],  # Nothing is dropped.
                         ["Asked who would qualify.", "Asked who is eligible for help.",
                          "Asked whether eligibility gets simpler.", "Asked whether the forms get shorter."])
        self.assertEqual([q.get("flag") for q in questions], [None, extraction.REPEATED_QA, None, None])
        self.assertIn("Remove Q&A", extraction.REPEATED_QA)  # The button that takes a repeat out of the note.
        self.assertIn(": Asked who is eligible for help. ⚠ — ", notion(draft))

    def drafted_testifier(self, name, role, organization):
        reply = {"entries": [{"start_cue": 1, "end_cue": 4, "bill": "H.1234", "bill_quote": "I support H.1234",
                              "testifiers": [{"name": name, "role": role, "organization": organization,
                                              "quote": "I'm Alex Morgan with Example Legal Services", "cues": [1]}],
                              "position": "Support", "position_quote": "I support H.1234",
                              "arguments": [{"text": "Expands access.", "quote": "The proposal would expand access"}]}]}
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client(reply))
        return draft["units"][0]["testifiers"][0]

    def test_legislators_need_no_organization(self):
        for name, role, chamber, shown in (
                ("Holloway", "Representative", None, "Representative Holloway"), ("Marsh", "Sen.", None, "Sen. Marsh"),
                ("Sen. Marsh", "Senator", None, "Sen. Marsh"),
                # Their chamber as the organization still means a legislator, not "Representative, ...".
                ("Holloway", "Representative", "Massachusetts House of Representatives", "Representative Holloway"),
                ("Marsh", "Senator", "Massachusetts Senate", "Senator Marsh"),
                ("Marsh", "Sen.", "The General Court", "Sen. Marsh")):
            with self.subTest(role=role, chamber=chamber):
                person = self.drafted_testifier(name, role, chamber)
                self.assertEqual(person["name"]["value"], shown)
                self.assertIsNone(person["organization"]["value"])
                self.assertTrue(person["organization"]["not_applicable"])

    def test_people_speaking_for_an_organization_get_no_legislator_title(self):
        # Gemini often gives "Representative" as the role of someone who represents a group: just the group.
        for role, organization, shown in (("Representative", "Example Legal Services", "Example Legal Services"),
                                          ("Representative", "Example Neighbors Collaborative", "Example Neighbors Collaborative"),
                                          ("Representative", "Community safety coalition",
                                           "Community safety coalition"),
                                          ("Director", "Example Legal Services", "Director, Example Legal Services"),
                                          ("Chair of the Example Section", None, "Chair of the Example Section")):
            with self.subTest(role):
                person = self.drafted_testifier("Alex Morgan", role, organization)
                self.assertEqual(person["name"]["value"], "Alex Morgan")
                self.assertEqual(person["organization"]["value"], shown)
                self.assertFalse(person["organization"].get("not_applicable"))

    def test_a_transcript_far_longer_than_a_hearing_is_refused_before_any_paid_call(self):
        client = Client()
        with patch.object(gemini, "MAX_WORDS", 10), patch.object(parts, "MAX_PARTS", 1), \
                self.assertRaisesRegex(InputError, "far longer than a hearing .* Check that the right file was added"):
            extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", client)
        self.assertEqual(client.prompts, [])

    def test_hearing_links_are_checked_and_follow_the_recording_into_drafting(self):
        import io
        from audio_test_helpers import metadata, wav_bytes
        from hearing_compiler.agenda import check_url
        from hearing_compiler.audio import receive_audio
        self.assertEqual(check_url(" https://www.malegislature.gov/Events/Hearings/Detail/1234/ "),
                         "https://malegislature.gov/Events/Hearings/Detail/1234")
        for bad in ("https://evil.example/Events/Hearings/Detail/1234", "http://malegislature.gov/Events/Hearings/Detail/1"):
            with self.subTest(bad), self.assertRaises(InputError):
                check_url(bad)
        store = Store(self.folder / "state")
        raw = wav_bytes(2)
        asset = receive_audio(store.directory, io.BytesIO(raw), len(raw), "hearing.wav")
        with self.assertRaises(InputError):
            store.enqueue_audio({**metadata(), "hearing_url": "https://example.com"}, asset, False)
        job = store.enqueue_audio({**metadata(), "hearing_url": "https://malegislature.gov/Events/Hearings/Detail/1234"},
                                  asset, False)
        transcript = fixture("sample-hearing")
        transcript["hearing"] = store.get(job["id"])["hearing"]
        store.cache_transcript(job["id"], transcript)
        self.assertEqual(store.get(job["id"])["input"]["hearing_url"],
                         "https://malegislature.gov/Events/Hearings/Detail/1234")

    def test_cancelled_waits_stop_drafting_and_backups_keep_saved_replies(self):
        from hearing_compiler.workspace_backup import create_backup, restore_backup
        cancel = threading.Event()
        store = Store(self.folder / "state")
        service = extraction.ExtractionService(store, cancel, Keys(), Client())
        cancel.set()
        with self.assertRaisesRegex(InputError, "app is closing"):
            service._wait(15)
        store.extractor = lambda document: extraction.gemini_draft(
            document, KEY, "gemini-3.8-flash", Client(), cache_dir=store.directory / "extraction-output")
        store.import_one(fixture("sample-hearing"))
        restored = Store(self.folder / "restored")
        restore_backup(restored, create_backup(store))
        self.assertEqual(len(list((restored.directory / "extraction-output").glob("*.json"))), 1)

    class Answers(Client):
        """Gemini replies in turn: text in a STOP reply, or a whole payload."""
        def __init__(self, *replies):
            super().__init__()
            self.replies = list(replies)

        def call_gemini(self, prompt, model, key, log=None, **_):
            self.prompts.append(prompt)
            reply = self.replies.pop(0)
            if isinstance(reply, str):
                reply = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": reply}]}}]}
            return reply, 12.5

    def test_retry_asks_gemini_again_after_an_unusable_reply_but_reuses_a_good_one(self):
        good = json.dumps(REPLY)
        store = Store(self.folder / "state")
        cache = store.directory / "extraction-output"
        client = self.Answers(good.replace(', "procedural"', ' "procedural"'), good)  # One missing comma.
        store.extractor = lambda document: extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client,
                                                                   cache_dir=cache)
        failed = store.import_one(fixture("sample-hearing"))
        self.assertEqual(failed["processing"], "Failed")
        self.assertIn("reply was not readable", failed["error"])
        self.assertEqual(store.retry("sample-hearing")["processing"], "Complete")
        self.assertEqual(len(client.prompts), 2)
        again = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", client, cache_dir=cache)
        self.assertEqual((len(client.prompts), again["extraction_provenance"]["reused_saved_reply"]), (2, True))
        # A readable reply with no testimony is set aside: a retry asks again, and backups leave it out.
        from zipfile import ZipFile
        from hearing_compiler.workspace_backup import create_backup
        store = Store(self.folder / "empty")
        cache = store.directory / "extraction-output"
        client = self.Answers(json.dumps({"entries": []}), good)
        with self.assertRaisesRegex(InputError, "^Gemini found no testimony in this transcript. If people testified "
                                                "in this recording, retry once; a clip of only the chair's opening"):
            extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", client, cache_dir=cache)
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", client, cache_dir=cache)
        self.assertEqual((len(client.prompts), draft["extraction_provenance"]["reused_saved_reply"]), (2, False))
        (aside,) = cache.glob("*.unusable-*.json")
        self.assertEqual(gemini.response_text(json.loads(aside.read_text(encoding="utf-8")))[0], '{"entries": []}')
        with ZipFile(create_backup(store)) as backup:
            replies = [name for name in backup.namelist() if name.startswith("extraction-output/")]
        self.assertEqual(replies, [f"extraction-output/{aside.name.split('.')[0]}.json"])  # Only the good reply.
        blocked = self.Answers({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
        with self.assertRaisesRegex(InputError, r"^Gemini declined to draft this transcript \(PROHIBITED_CONTENT\)\. "
                                                "Retrying won't change that; take this hearing's notes by hand.$"):
            extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", blocked)

    def test_a_cut_off_reply_on_a_short_transcript_is_asked_again_not_split(self):
        cut = self.Answers({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{"}]}}]})
        with self.assertRaisesRegex(InputError, r"ran out of room\. Retry this hearing once \(each attempt is charged\)"):
            extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", cut)
        self.assertEqual(len(cut.prompts), 1)

    def test_a_keychain_timeout_never_writes_the_key_to_the_log(self):
        import subprocess
        import traceback
        timeout = subprocess.TimeoutExpired(["/usr/bin/security", "add-generic-password", "-w", KEY], 20, output=KEY)
        with patch.object(extraction.KeychainKeys, "available", return_value=True), \
                patch.object(extraction.subprocess, "run", side_effect=timeout), \
                self.assertLogs(level="WARNING") as logs, \
                self.assertRaisesRegex(InputError, "^The Mac Keychain did not save the key.") as raised:
            extraction.KeychainKeys().set(KEY)
        self.assertIn("Keychain add-generic-password failed: TimeoutExpired", "\n".join(logs.output))
        self.assertNotIn(KEY, "\n".join(logs.output + traceback.format_exception(raised.exception)))


class ReviewOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_notion_copy_matches_the_reference_format_and_marks_only_open_flags(self):
        from hearing_compiler.rich_review import notion
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client())
        note = notion(draft)
        self.assertIn("Tags: Hearing Notes; Example Committee", note)
        self.assertIn("Bill: H.1234", note)
        self.assertIn("Testifiers: Alex Morgan ⚠ (Example Legal Services)", note)
        self.assertIn("- Expands access to legal assistance.\n", note)
        self.assertIn("- Simplifies eligibility. ⚠", note)
        self.assertIn("- Unidentified member: Who would qualify for assistance? — Alex Morgan: Residents", note)
        for clutter in ("Evidence:", "[draft", "segment", "s01", "Attribution"):
            self.assertNotIn(clutter, note)
        self.assertIn("⚠ = still to check against the audio.", note)
        # Hearing-wide checks (here, a dropped entry) go into the copy until approved.
        self.assertRegex(note, r"⚠ Still to check:\n\n- Gemini listed H\.9999 at \d\d:\d\d:\d\d but drafted no "
                               r"testimony there, so one may be missing\. Listen, and add it if someone testified\. ⚠\n")
        unit = draft["units"][0]
        for item in (unit["testifiers"][0]["name"], unit["arguments"][1]["text"]):
            item["review"] = "APPROVED"
        self.assertIn("H.9999", notion(draft))
        for issue in draft["issues"]:  # The dropped entry, and the testimony running to the transcript's end.
            issue["review"] = "APPROVED"
        self.assertNotIn("⚠", notion(draft))

    def test_redraft_replaces_only_untouched_drafts(self):
        store = Store(self.folder)
        store.import_one(fixture("sample-hearing"))  # Rule baseline draft.
        service = extraction.ExtractionService(store, threading.Event(), Keys(), Client())
        with self.assertRaisesRegex(InputError, "Turn on Gemini"):
            store.extractor = service
            store.redraft("sample-hearing")
        service.configure("gemini", api_key=KEY)
        self.assertEqual(store.redraft("sample-hearing")["processing"], "Waiting")
        queue = AudioQueue(store)
        try:
            eventually(lambda: store.get("sample-hearing")["processing"] == "Complete", timeout=10)
        finally:
            queue.close()
        job = store.get("sample-hearing")
        self.assertEqual(job["draft"]["provider"], "gemini-draft-v1")
        path = "/units/0/bill"
        store.review("sample-hearing", job["revision"], [{"path": path, "review": "APPROVED"}])
        from hearing_compiler.store import Conflict
        with self.assertRaisesRegex(Conflict, "review work"):
            store.redraft("sample-hearing")

    def test_hearing_wide_checks_count_as_items_to_check_until_approved(self):
        from hearing_compiler.rich_review import open_flags, summary
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client())
        dropped, cut = draft["issues"]
        for issue in (dropped, cut):
            self.assertEqual(issue["flag"], "Hearing-wide check: confirm this before copying the note, then click Approve.")
        # Name, unfound quote, dropped entry, and the testimony that runs to the end of the transcript.
        self.assertEqual((open_flags(draft), summary(draft)["flags"]), (4, 4))
        dropped["review"] = cut["review"] = "APPROVED"
        self.assertEqual((open_flags(draft), summary(draft)["flags"]), (2, 2))

    # One testimony whose only exchange is a member's comment with no reply.
    COMMENT_ONLY = {"entries": [{"start_cue": 1, "end_cue": 4, "bill": "H.1234", "bill_quote": "I support H.1234",
                                 "testifiers": [{"name": "Alex Morgan", "role": None, "organization": None,
                                                 "quote": "I'm Alex Morgan with Example Legal Services", "cues": [1]}],
                                 "position": "Support", "position_quote": "I support H.1234",
                                 "arguments": [{"text": "Expands access.", "quote": "The proposal would expand access"}],
                                 "questions": [{"asker": "Unidentified member", "question": "Thanked the witness.",
                                                "respondent": None, "response": None,
                                                "quote": "Who would qualify for assistance", "cues": [3]}]}]}

    def test_a_reply_typed_for_a_missing_response_is_copied_and_marked_until_approved(self):
        from hearing_compiler.rich_review import correct, notion
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client(self.COMMENT_ONLY))
        line = "- Unidentified member: Thanked the witness."

        def copied_line(value, review):
            change = {"path": "/units/0/questions/0/response_resolution", "value": value, "review": review}
            return next(l for l in notion(correct(draft, [change])).splitlines() if l.startswith(line))

        typed = "The witness thanked the committee."
        for review in ("UNRESOLVED", "NEEDS_REVIEW"):
            with self.subTest(review):
                self.assertEqual(copied_line(typed, review), f"{line} — {typed} ⚠")
        self.assertEqual(copied_line(typed, "APPROVED"), f"{line} — {typed}")
        # The No response (comment only) button's text is a decision, not a reply.
        self.assertEqual(copied_line("No response; comment only.", "APPROVED"), line)
        self.assertEqual(copied_line("No response; comment only.", "NEEDS_REVIEW"), f"{line} — No response noted ⚠")

    def test_a_comment_with_no_answer_can_be_cleared_through_the_review_route(self):
        reply = self.COMMENT_ONLY
        server = make_server(self.folder / "http", 0, seed=False)
        server.store.extractor = lambda document: extraction.gemini_draft(document, KEY, "gemini-3.8-flash", Client(reply))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            token = json.loads(urlopen(base + "/api/session").read())["token"]

            def post(path, body):
                return json.loads(urlopen(Request(base + path, data=json.dumps(body).encode(), headers={
                    "Content-Type": "application/json", "X-Review-Token": token})).read())

            def copied():  # What Copy Hearing Notes and Export Markdown send.
                return urlopen(base + "/api/export/sample-hearing?format=md").read().decode("utf-8")

            post("/api/import", {"documents": [fixture("sample-hearing")]})
            line = "- Unidentified member: Thanked the witness."
            self.assertIn(line + " — No response noted ⚠\n", copied())
            # The exact correction the "No response (comment only)" button sends.
            post("/api/review", {"id": "sample-hearing", "revision": 0, "changes": [
                {"path": "/units/0/questions/0/response_resolution", "value": "No response; comment only.",
                 "review": "APPROVED"}]})
            note = copied()
            self.assertIn(line + "\n", note)
            self.assertIn("Testifiers: Alex Morgan ⚠", note)  # The blank organization still needs a decision,
            self.assertIn("⚠ = still to check against the audio.", note)  # so the legend stays without any AI flag.
            post("/api/review", {"id": "sample-hearing", "revision": 1, "changes": [
                {"path": "/units/0/testifiers/0/organization", "value": None, "not_applicable": True,
                 "review": "APPROVED"}, {"path": "/issues/0", "review": "APPROVED"}]})  # It runs to the end.
            self.assertNotIn("⚠", copied())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


LINK = "https://malegislature.gov/Events/Hearings/Detail/1234"
AGENDA = [{"bill": "H.1111", "title": "An Act about clean rivers", "sponsor": "A. Sponsor"},
          {"bill": "S.2222", "title": "An Act about clean rivers", "sponsor": "B. Sponsor"},
          {"bill": "H.3333", "title": "An Act funding river monitoring", "sponsor": "C. Sponsor"},
          {"bill": "H.4444", "title": "An Act about local parks", "sponsor": "D. Sponsor"}]
CLOSING = {"start_cue": 8, "end_cue": 8, "summary": "The chair closed the hearing."}
RIVERS = ("Next, House 1111 and Senate 2222, an act about clean rivers, and House 3333.",
          "I'm Alex Morgan with Example Legal Services.",
          "I support these bills because our rivers need cleaning.",
          "The second bill would fund river monitoring stations in every county.",
          "Thank you. Any questions? Seeing none, thank you.",
          "Next we have Dana Avery on House 4444, an act about local parks.",
          "I'm Dana Avery and I support this bill for local parks in every town.",
          "Thank you all. This hearing is adjourned.")


def hearing(*lines):
    """A synthetic transcript, one cue per line, with the hearing link that loads the agenda."""
    document = fixture("sample-hearing")
    document["hearing_url"] = LINK
    document["segments"] = [{"id": f"c{n}", "speaker": "Speaker 1", "start": n * 10, "end": n * 10 + 9, "text": text}
                            for n, text in enumerate(lines, 1)]
    return document


def drafted(document, entries, procedural=(CLOSING,), agenda=AGENDA):
    if agenda is None:
        document = {k: v for k, v in document.items() if k != "hearing_url"}
    return extraction.gemini_draft(document, KEY, "gemini-3.8-flash",
                                   Client({"entries": entries, "procedural": list(procedural)}),
                                   fetch_agenda=lambda url: {"url": url, "bills": agenda})


def bill_only(bill, check=None, title=None):
    return {"start_cue": 1, "end_cue": 5, "bill": bill, "bill_title": title,
            "bill_quote": "Next, House 1111 and Senate 2222", "bill_cues": [1], "bill_check": check}


def morgan(bill=None, **changes):
    return {"start_cue": 1, "end_cue": 5, "bill": bill, "bill_quote": "I support these bills because our rivers",
            "bill_cues": [3], "bill_check": "Bill numbers not repeated by the testifier",
            "testifiers": [{"name": "Alex Morgan", "role": None, "organization": "Example Legal Services",
                            "quote": "I'm Alex Morgan with Example Legal Services", "cues": [2]}],
            "position": "Support", "position_quote": "I support these bills because our rivers", "position_cues": [3],
            "arguments": [{"text": "Rivers need cleaning.", "quote": "I support these bills because our rivers need",
                           "cues": [3]},
                          {"text": "Funds river monitoring stations.",
                           "quote": "The second bill would fund river monitoring stations", "cues": [4]}],
            "questions": [], "questions_note": "None asked.", **changes}


class EmptyEntryTests(unittest.TestCase):
    def test_a_skipped_testimony_gets_one_warning_naming_its_bills_and_time(self):
        # On a real hearing: Gemini listed H.2011 and S.1227 for a testimony but drafted nothing.
        empty = [{"start_cue": 6, "end_cue": 7, "bill": bill, "bill_quote": "Next we have Dana Avery on House 4444",
                  "bill_cues": [6], "bill_check": None, "testifiers": [], "arguments": [], "questions": []}
                 for bill in ("H.4444", "S.2222")]
        draft = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None)] + empty)
        warnings = [i for i in draft["issues"] if "drafted no testimony" in i["value"]]
        self.assertEqual([i["value"] for i in warnings],
                         ["Gemini listed H.4444 / S.2222 at 00:01:00 but drafted no testimony there, so one may be "
                          "missing. Listen, and add it if someone testified."])
        self.assertEqual(warnings[0]["evidence"], ["c6"])
        self.assertTrue(warnings[0]["flag"])


class StitchedQuoteTests(unittest.TestCase):
    """Gemini sometimes joins two nearby passages with "..." (30 of 33 unfound quotes in one real hearing)."""
    LINES = ("My name is Morgan Reyes and thank you for having me.", "It is good to be here this morning.",
             "I'm a divorce and family law practitioner for 20 years.", "Arbitration lets families tailor the process.")

    def resolved(self, quote, lines=LINES):
        cues = [{"n": n, "start": n * 10.0, "end": n * 10.0 + 9, "text": text} for n, text in enumerate(lines, 1)]
        draft = {"entries": [{"start_cue": 1, "end_cue": 4, "testifiers": [
            {"name": "Morgan Reyes", "quote": quote, "cues": [1], "check": None}]}]}
        stats = gemini.resolve(draft, cues)
        return stats["found"], draft["entries"][0]["testifiers"][0]

    def test_each_verbatim_piece_in_order_counts_as_found(self):
        found, person = self.resolved("My name is Morgan Reyes... I'm a divorce and family law practitioner")
        self.assertEqual((found, person["cues"], person["check"]), (1, [1, 3], None))
        found, person = self.resolved("My name is Morgan Reyes… for 20 years")  # The ellipsis character too.
        self.assertEqual((found, person["cues"]), (1, [1, 3]))

    def test_a_piece_that_isnt_there_keeps_the_flag_but_plays_from_the_part_that_is(self):
        for quote in ("My name is Morgan Reyes... and I chair the bar association committee",
                      "I'm a divorce and family law practitioner... My name is Morgan Reyes"):  # Out of order.
            found, person = self.resolved(quote)
            self.assertEqual(found, 0)
            self.assertIn(gemini.PART_FOUND, person["check"])
        self.assertEqual(person["cues"], [3])
        found, person = self.resolved("Nobody said this sentence... or this one either")
        self.assertIn("supporting quote not found", person["check"])

    def test_pieces_too_far_apart_or_inside_other_words_are_not_joined(self):
        far = (self.LINES[0],) + ("filler words that go on and on for a while here",) * 80 + self.LINES[2:]
        found, _ = self.resolved("My name is Morgan Reyes... I'm a divorce and family law practitioner", far)
        self.assertEqual(found, 0)
        found, _ = self.resolved("My name is Morgan Reyes... ood to be here")  # "good", not "ood".
        self.assertEqual(found, 0)


class DraftRepairTests(unittest.TestCase):
    def test_the_prompt_is_unchanged_so_saved_replies_are_still_reused(self):
        import hashlib
        document = fixture("sample-hearing")
        cues = [{"n": i + 1, "start": s["start"], "end": s["end"], "text": s["text"], "line": None}
                for i, s in enumerate(document["segments"])]
        bills = [{"bill": "H.1234", "title": "An Act about legal help", "sponsor": "A. Sponsor"}]
        prompts = [gemini.build_prompt(cues, document["hearing"], bills), gemini.build_prompt(cues, document["hearing"])]
        # A saved reply is found by sha256(model + prompt); any change to the prompt text pays for a new draft.
        self.assertEqual([hashlib.sha256(p.encode("utf-8")).hexdigest() for p in prompts],
                         ["807df821f891cdbcc3dd39038e19b745516e89159541bdba9a927eed7606cfcf",
                          "4ee61c2b9ffb06e8d9de4a024b868fd8047c516e9b34bdd6330b1e3d892cac86"])

    def test_bill_only_entries_give_their_bills_to_the_testimony_over_the_same_cues(self):
        from hearing_compiler.rich_review import agenda_titles
        avery = {"start_cue": 6, "end_cue": 7, "bill": "H.4444", "bill_quote": "Next we have Dana Avery on House 4444",
                 "bill_cues": [6], "testifiers": [{"name": "Dana Avery", "quote": "I'm Dana Avery and I support this bill",
                                                   "cues": [7]}],
                 "position": "Support", "position_quote": "I'm Dana Avery and I support this bill", "position_cues": [7]}
        points = {"start_cue": 6, "end_cue": 7, "bill": None, "questions_note": "None asked.",
                  "arguments": [{"text": "Supports local parks.", "quote": "I support this bill for local parks",
                                 "cues": [7]}]}
        stray = {"start_cue": 8, "end_cue": 8, "bill": "H.9999", "bill_quote": "Thank you all. This hearing is adjourned",
                 "bill_cues": [8]}
        draft = drafted(hearing(*RIVERS), [bill_only("H.1111"), bill_only("S.2222", "Spoken as Senate 2222"),
                                           bill_only("H.3333"), morgan(), avery, points, stray])
        rivers, parks = draft["units"]
        self.assertEqual(rivers["bill"]["value"], "H.1111 / S.2222 / H.3333")
        self.assertEqual(rivers["bill"]["evidence"], ["c1", "c3"])
        self.assertEqual(rivers["bill_title"]["value"], "An Act about clean rivers / An Act funding river monitoring")
        # H.3333 isn't a companion, so its points may be mixed in; the bill-only entry's own check is kept.
        self.assertEqual(rivers["bill"]["flag"], f"{extraction.MULTI_BILL} Bill numbers not repeated by the testifier; "
                                                 "S.2222: Spoken as Senate 2222")
        self.assertEqual([a["text"]["value"] for a in rivers["arguments"]],
                         ["Rivers need cleaning.", "Funds river monitoring stations."])
        # The testifier-only and arguments-only halves of one testimony are one testimony.
        self.assertEqual((parks["bill"]["value"], [a["text"]["value"] for a in parks["arguments"]]),
                         ("H.4444", ["Supports local parks."]))
        self.assertEqual(parks["testifiers"][0]["name"]["value"], "Dana Avery")
        # Only the entry no testimony shares cues with is left out, with its warning.
        (left_out,) = [i["value"] for i in draft["issues"]]
        self.assertIn("Gemini listed H.9999", left_out)
        self.assertIn("drafted no testimony there, so one may be missing", left_out)
        provenance = draft["extraction_provenance"]
        self.assertEqual((provenance["bill_only_entries_attached"], provenance["split_entries_rejoined"]), (3, 1))
        self.assertEqual(provenance["agenda"][0], {"bill": "H.1111", "title": "An Act about clean rivers",
                                                   "sponsor": "A. Sponsor"})
        self.assertEqual(agenda_titles(draft)["H.3333"], "An Act funding river monitoring")

        companions = drafted(hearing(*RIVERS), [bill_only("H.1111"), bill_only("S.2222", "Spoken as Senate 2222"),
                                                morgan()])["units"][0]
        self.assertEqual((companions["bill"]["value"], companions["bill_title"]["value"]),
                         ("H.1111 / S.2222", "An Act about clean rivers"))
        # No doubt about the companion; H.3333 was called with them but now has no entry.
        self.assertEqual(companions["bill"]["flag"], "This testimony also mentions H.3333; check which points belong to "
                                                     "which bill. Bill numbers not repeated by the testifier; S.2222: "
                                                     "Spoken as Senate 2222")
        # Without an agenda, only the AI's own matching titles make companions.
        for title, flagged in (("An Act about clean rivers", False), (None, True)):
            with self.subTest(title=title):
                unit = drafted(hearing(*RIVERS), [bill_only("H.1111", title=title), bill_only("S.2222", title=title),
                                                  morgan()], agenda=None)["units"][0]
                self.assertEqual(unit["bill"]["value"], "H.1111 / S.2222")
                self.assertEqual(extraction.MULTI_BILL in unit["bill"]["flag"], flagged)

    def test_agenda_bills_said_during_a_testimony_but_not_on_its_list_are_flagged(self):
        lines = ("Next, House 1111, an act about clean rivers.", "I'm Alex Morgan with Example Legal Services.",
                 "I support House 1111, and House 3333 matters too, unlike House 5555.",
                 "Rivers need cleaning across the state.", "Thank you.", "Thank you.", "Thank you.",
                 "Thank you all. This hearing is adjourned.")
        def entry(bill, argument, quote):
            return {"start_cue": 1, "end_cue": 4, "bill": bill, "bill_quote": "Next, House 1111, an act about",
                    "testifiers": [{"name": "Alex Morgan", "quote": "I'm Alex Morgan with Example Legal Services"}],
                    "position": "Support", "position_quote": "I support House 1111, and House",
                    "arguments": [{"text": argument, "quote": quote}]}
        rivers = entry("H.1111", "Rivers need cleaning.", "Rivers need cleaning across the state")
        (unit,) = drafted(hearing(*lines), [rivers])["units"]
        # H.5555 isn't on the agenda, so it isn't flagged.
        self.assertEqual(unit["bill"]["flag"], "This testimony also mentions H.3333; check which points belong to "
                                               "which bill.")
        # Gemini's own entry for the other bill, by the same testifier at the same time, settles it.
        units = drafted(hearing(*lines), [rivers, entry("H.3333", "Monitoring matters.", "and House 3333 matters too")])["units"]
        self.assertEqual([(u["bill"]["value"], u["bill"].get("flag")) for u in units], [("H.1111", None), ("H.3333", None)])

    def test_figures_missing_from_the_quoted_lines_are_flagged(self):
        self.assertEqual(extraction.figures("twenty-five people, 1,200 cases, 40% of them, $3.5 million, one of two"),
                         [("twenty-five", 25, False), ("1,200", 1200, False), ("40%", 40, False),
                          ("$3.5 million", 3.5e6, False)])
        self.assertEqual(extraction.figures("hundreds of thousands, over a million, COVID-19 and K-12 under H.1234"),
                         [("hundreds of thousands", 1e5, True), ("a million", 1e6, True)])
        lines = ("I'm Alex Morgan with Example Legal Services.", "I personally spent over a million dollars on lawyers.",
                 "About 40% of cases settle before trial.", "COVID slowed the courts for years.",
                 "Courts see 300 of these cases a year.", "Thank you.", "Thank you.",
                 "Thank you all. This hearing is adjourned.")
        arguments = [("Spent hundreds of thousands of dollars on lawyers.", "I personally spent over a million dollars",
                      "Amount unclear"),
                     ("About 40% of cases settle.", "About 40% of cases settle before trial", None),
                     ("COVID-19 slowed the courts.", "COVID slowed the courts for years", None),
                     # Said later in the same testimony, beyond the two lines around its quote.
                     ("Courts see 300 such cases a year.", "I'm Alex Morgan with Example Legal Services", None),
                     ("Courts see 3,000 such cases a year.", "Courts see 300 of these cases", None)]
        entry = {"start_cue": 1, "end_cue": 5, "bill": "H.1111", "bill_quote": "I'm Alex Morgan with Example Legal",
                 "testifiers": [{"name": "Alex Morgan", "quote": "I'm Alex Morgan with Example Legal Services"}],
                 "position": "Support", "position_quote": "I'm Alex Morgan with Example Legal",
                 "arguments": [{"text": text, "quote": quote, "check": check} for text, quote, check in arguments]}
        (unit,) = drafted(hearing(*lines), [entry])["units"]
        figure = "Has a figure ({}) that isn't in its quoted lines; check it against the audio."
        self.assertEqual([a["text"].get("flag") for a in unit["arguments"]],
                         [figure.format("hundreds of thousands") + " Amount unclear", None, None, None,
                          figure.format("3,000")])

    def test_each_answer_has_its_own_evidence_after_the_question(self):
        lines = ("I'm Alex Morgan with Example Legal Services.", "The proposal would expand access to legal help.",
                 "Representative Avery: who would qualify for this help?",
                 "Residents who meet the income criteria would qualify.", "That covers about 2,000 families each year.",
                 "Representative Avery: what would it cost the state?", "Roughly one million dollars a year.",
                 "Thank you all. This hearing is adjourned.")
        entry = {"start_cue": 1, "end_cue": 7, "bill": "H.1111", "bill_quote": "I'm Alex Morgan with Example Legal",
                 "testifiers": [{"name": "Alex Morgan", "quote": "I'm Alex Morgan with Example Legal Services"}],
                 "position": "Support", "position_quote": "The proposal would expand access to legal help",
                 "arguments": [{"text": "Expands access.", "quote": "The proposal would expand access to legal help"}],
                 "questions": [{"asker": "Representative Avery", "question": "Who would qualify?",
                                "respondent": "Alex Morgan", "response": "About 2,000 income-eligible families.",
                                "quote": "who would qualify for this help"},
                               {"asker": "Representative Avery", "question": "What would it cost?",
                                "respondent": "Alex Morgan", "response": "About a million dollars a year.",
                                "quote": "what would it cost the state"}]}
        (unit,) = drafted(hearing(*lines), [entry])["units"]
        first, second = unit["questions"]
        self.assertEqual(first["question"]["evidence"], ["c3"])  # The question keeps its own lines.
        # The answer runs from the quote's last line up to the next question, inside the testimony: the quote may
        # be the answer itself, and the chair's closing line isn't part of it.
        answer = first["responses"][0]
        self.assertEqual([answer["text"]["evidence"], answer["respondent"]["name"]["evidence"]],
                         [["c3", "c4", "c5"]] * 2)
        self.assertEqual(second["responses"][0]["text"]["evidence"], ["c6", "c7"])
        self.assertNotIn("flag", first["responses"][0]["text"])  # Its figure is in the answer's lines.
        entry["questions"][1]["response_cues"] = [7]  # An answer with its own lines keeps them.
        self.assertEqual(drafted(hearing(*lines), [entry])["units"][0]["questions"][1]["responses"][0]["text"]["evidence"],
                         ["c7"])

    def test_bills_shared_by_several_testimonies_or_unlike_a_testimonys_own_title_are_flagged(self):
        alice = {**morgan(), "testifiers": [{"name": "Dana Avery", "quote": "I'm Dana Avery", "cues": [2]}]}
        bob = morgan("H.1111", bill_check=None)
        units = drafted(hearing(*RIVERS), [alice, bob, bill_only("H.3333")])["units"]
        # S.2222, H.1111's Senate twin, is said in this testimony, so it joins that bill line too.
        self.assertEqual([u["bill"]["value"] for u in units], ["H.3333", "H.1111 / H.3333 / S.2222"])
        self.assertTrue(all(extraction.MULTI_BILL in u["bill"]["flag"] for u in units))
        titled = morgan(bill_check=None, bill_title="An Act funding river monitoring")
        (unit,) = drafted(hearing(*RIVERS), [titled, bill_only("H.1111")])["units"]
        self.assertIn(extraction.MULTI_BILL, unit["bill"]["flag"])  # Its own title is another bill's.

    def test_a_senate_twin_said_in_a_testimony_joins_its_bill_line_unflagged(self):
        draft = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None)])
        (unit,) = draft["units"]
        self.assertEqual(unit["bill"]["value"], "H.1111 / S.2222")  # Same official title, other chamber.
        self.assertNotIn("S.2222", unit["bill"]["flag"])  # H.3333, another act, is still flagged.
        self.assertIn("also mentions H.3333", unit["bill"]["flag"])
        self.assertFalse(any("S.2222" in issue["value"] for issue in draft["issues"]))
        same_house = AGENDA + [{"bill": "H.5555", "title": "An Act about clean rivers", "sponsor": "E. Sponsor"}]
        lines = ("Next, House 1111 and House 5555, about clean rivers.",) + RIVERS[1:]
        (unit,) = drafted(hearing(*lines), [morgan("H.1111", bill_check=None)], agenda=same_house)["units"]
        self.assertEqual(unit["bill"]["value"], "H.1111")  # Two House bills with one title aren't twins.
        self.assertIn("also mentions H.5555", unit["bill"]["flag"])

    def test_guessed_names_in_question_text_and_answers_from_a_panel_are_flagged(self):
        lines = RIVERS[:4] + ("What would the stations cost each county?", "About a million dollars a year.") + RIVERS[6:]
        def asked(respondent, testifiers=None):
            entry = morgan("H.1111", bill_check=None, questions=[{
                "asker": "Senator Brennan", "question": "Senator Brennan asked what the stations would cost.",
                "respondent": respondent, "response": "About a million dollars a year.",
                "quote": "What would the stations cost each county", "cues": [5]}],
                **({"testifiers": testifiers} if testifiers else {}))
            (unit,) = drafted(hearing(*lines), [entry])["units"]
            q = unit["questions"][0]
            return q["question"].get("flag"), q["responses"][0]["respondent"]["name"].get("flag")
        question, answer = asked("Alex Morgan")
        self.assertTrue(question.startswith(extraction.ASKED_NAME))  # Brennan isn't said nearby.
        self.assertIsNone(answer)  # A lone witness answering their own questions.
        self.assertEqual(asked("Dana Avery")[1], extraction.ANSWERED)  # Not the testifier.
        panel = [{"name": "Alex Morgan", "quote": "I'm Alex Morgan with Example Legal Services", "cues": [2]},
                 {"name": "Dana Avery", "quote": "I'm Alex Morgan with Example Legal Services", "cues": [2]}]
        self.assertEqual(asked("Alex Morgan", panel)[1], extraction.ANSWERED)  # Which panelist answered is a guess.

    def test_only_one_persons_split_entries_are_rejoined_and_a_conflicting_position_is_flagged(self):
        speaker = lambda name: {**morgan(), "testifiers": [{"name": name, "quote": f"I'm {name}", "cues": [2]}],
                                "arguments": [], "questions_note": None}
        points = {**morgan(), "testifiers": [], "position": "Oppose"}
        units = drafted(hearing(*RIVERS), [speaker("Alex Morgan"), speaker("Dana Avery"), points])["units"]
        self.assertEqual([len(u["arguments"]) for u in units], [0, 0, 2])  # Not filed under the first speaker.
        (unit,) = drafted(hearing(*RIVERS), [speaker("Alex Morgan"), points])["units"]
        self.assertEqual((unit["position"]["value"], len(unit["arguments"])), ("Unclear", 2))
        self.assertTrue(unit["position"]["flag"].startswith("The AI gave both Support and Oppose"))

    def test_chamber_titles_need_the_chamber_itself_and_a_district_keeps_the_title(self):
        def person(role, organization):
            entry = morgan("H.1111", testifiers=[{"name": "Jamie Ruiz", "role": role, "organization": organization,
                                                   "quote": "I'm Alex Morgan with Example Legal Services", "cues": [2]}])
            (unit,) = drafted(hearing(*RIVERS), [entry])["units"]
            p = unit["testifiers"][0]
            return p["name"]["value"], p["organization"]["value"], p["organization"].get("not_applicable", False)
        self.assertEqual(person("Senator", "Massachusetts Senate"), ("Senator Jamie Ruiz", None, True))
        self.assertEqual(person("Representative", "7th Essex District"),
                         ("Representative Jamie Ruiz", "7th Essex District", False))
        self.assertEqual(person("Senator", "UMass Amherst Student Senate"),
                         ("Jamie Ruiz", "Senator, UMass Amherst Student Senate", False))
        self.assertEqual(person("Representative", "Massachusetts Youth Legislature"),
                         ("Jamie Ruiz", "Massachusetts Youth Legislature", False))

    def test_a_rebuild_that_yields_no_testimony_keeps_the_saved_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            with self.assertRaisesRegex(InputError, "^Rebuilding from the saved AI reply gave no testimony"):
                extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client({"entries": []}),
                                        cache_dir=cache, rebuilding=True)
            self.assertEqual([p.name.count(".unusable-") for p in cache.glob("*.json")], [0])

    def test_without_an_agenda_years_and_percentages_are_not_read_as_bills(self):
        lines = ("I'm Alex Morgan with Example Legal Services.", "It's 25 years of work. That's 40 percent.",
                 "Thank you.")
        entry = morgan("H.1111", start_cue=1, end_cue=3, arguments=[{"text": "Decades of work.", "quote": "It's 25 years of work"}])
        (unit,) = drafted(hearing(*lines), [entry], procedural=(), agenda=None)["units"]
        self.assertNotIn("also mentions", unit["bill"].get("flag") or "")
        self.assertEqual(extraction.figures("1" * 400), [])  # Hundreds of digits aren't a figure.

    def test_a_recording_that_ends_during_a_testimony_leaves_its_questions_open(self):
        from hearing_compiler.rich_review import notion
        entry = {"start_cue": 1, "end_cue": 4, "bill": "H.1234", "bill_quote": "I support H.1234",
                 "testifiers": [{"name": "Alex Morgan", "quote": "I'm Alex Morgan with Example Legal Services"}],
                 "position": "Support", "position_quote": "I support H.1234",
                 "arguments": [{"text": "Expands access.", "quote": "The proposal would expand access"}],
                 "questions": [], "questions_note": "None asked."}
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client({"entries": [entry]}))
        (issue,) = draft["issues"]
        self.assertEqual(issue["value"], "The recording ends during Alex Morgan's testimony; it may continue in the "
                                         "next part.")
        self.assertEqual((issue["evidence"], issue["review"]), (["s02", "s03", "s04"], "NEEDS_REVIEW"))
        (unit,) = draft["units"]
        self.assertEqual((unit["questions_none"]["value"], unit["question_status"]), (None, "coverage_incomplete"))
        self.assertIn("- None noted during this testimony. ⚠", notion(draft))
        # Gemini's entry for each bill of the cut-off testimony stays open too, and it is named once.
        other = {**entry, "bill": "H.5678", "bill_title": "An Act about court fees"}
        draft = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash",
                                        Client({"entries": [{**entry, "bill_title": "An Act about legal help"}, other]}))
        self.assertEqual([i["value"] for i in draft["issues"]],
                         ["The recording ends during Alex Morgan's testimony; it may continue in the next part."])
        self.assertEqual([u["questions_none"]["value"] for u in draft["units"]], [None, None])
        # The chair closing the hearing in the last lines means it wasn't cut off.
        closed = extraction.gemini_draft(fixture("sample-hearing"), KEY, "gemini-3.8-flash", Client(
            {"entries": [entry], "procedural": [{"start_cue": 4, "end_cue": 4, "summary": "Hearing closed."}]}))
        self.assertEqual((closed["issues"], closed["units"][0]["questions_none"]["value"]), ([], "None asked."))


class SecondCheckTests(unittest.TestCase):
    FLAG = {"id": "/units/0/arguments/1/text", "verdict": "overstated", "reason": "The speaker said every county.",
            "quote": "fund river monitoring stations in every county"}

    def checked(self, reply, lines=RIVERS, cache=None, entry=None, before=None):
        draft = drafted(hearing(*lines), [entry or morgan("H.1111", bill_check=None)])
        if before:
            before(draft)
        client = Client(check_reply=reply)
        summary = second_check.run(draft, KEY, "gemini-3.8-flash", client, cache_dir=cache)
        return draft, summary, client

    def test_only_quoted_findings_flag_existing_items_and_nothing_is_rewritten(self):
        reply = {"items": [self.FLAG,
                           {"id": "/units/0/arguments/0/text", "verdict": "not_said", "reason": "Invented.",
                            "quote": "a phrase nobody said at this hearing"},
                           {"id": "/units/9/arguments/0/text", "verdict": "overstated", "reason": "No such item.",
                            "quote": "I support these bills because"},
                           {"id": "/units/0/position", "verdict": "fine", "reason": "Not a verdict.",
                            "quote": "I support these bills because"}],
                 "omissions": [{"point": "Every county gets a station.", "quote": "stations in every county"},
                               {"point": "Unquoted.", "quote": "words that are not there"}]}
        reviews = {}

        def existing(draft):
            draft["units"][0]["arguments"][1]["text"]["flag"] = "The drafter's own flag."
            reviews.update({path: item["review"] for path, item in fields(draft)})
        draft, summary, _ = self.checked(reply, before=existing)
        unit = draft["units"][0]
        self.assertEqual(unit["arguments"][1]["text"]["flag"],
                         "Second check: The speaker said every county. Transcript: "
                         "“fund river monitoring stations in every county” The drafter's own flag.")
        self.assertNotIn("Second check", unit["arguments"][0]["text"].get("flag") or "")
        self.assertEqual([a["text"]["value"] for a in unit["arguments"]],
                         ["Rivers need cleaning.", "Funds river monitoring stations."])  # Values never change.
        self.assertEqual({path: item["review"] for path, item in fields(draft)}, reviews)
        self.assertEqual((summary["flags"], summary["failed"]), (1, []))
        self.assertEqual(draft["second_check"]["missed"],
                         [{"unit": unit["id"], "point": "Every county gets a station.", "quote": "stations in every county",
                           "evidence": ["c4"]}])

    def test_a_misheard_bill_number_never_replaces_the_agendas(self):
        lines = (RIVERS[0], RIVERS[1], "I support House 1112 because our rivers need cleaning.") + RIVERS[3:]
        entry = morgan("H.1111", bill_check=None, arguments=[
            {"text": "H.1111 helps clean rivers.", "quote": "because our rivers need cleaning", "cues": [3]}])
        reply = {"items": [{"id": "/units/0/arguments/0/text", "verdict": "changed",
                            "reason": "The transcript says House 1112.", "quote": "I support House 1112 because our rivers"},
                           {"id": "/units/0/position", "verdict": "wrong_attribution",  # No bill in "Support" itself.
                            "reason": "This support is for House 1112.", "quote": "I support House 1112 because our rivers"}],
                 "omissions": []}
        draft, summary, _ = self.checked(reply, lines, entry=entry)
        self.assertEqual(summary["flags"], 0)
        self.assertNotIn("Second check", draft["units"][0]["arguments"][0]["text"].get("flag") or "")
        self.assertNotIn("Second check", draft["units"][0]["position"].get("flag") or "")

    def test_saved_check_replies_are_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            draft, _, client = self.checked({"items": [self.FLAG], "omissions": []}, cache=Path(folder))
            self.assertEqual(len(client.second), 1)
            again = Client(check_reply={"items": [], "omissions": []})
            second_check.run(draft, KEY, "gemini-3.8-flash", again, cache_dir=Path(folder))
            self.assertEqual(again.second, [])  # Same testimony, same prompt: no new call.

    def test_a_check_that_cannot_run_leaves_the_draft_and_says_so(self):
        def busy(prompt):
            raise gemini.GeminiError("Gemini is busy.")
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            keys = Keys()
            keys.set(KEY)
            service = extraction.ExtractionService(store, threading.Event(), keys, Client(check_reply=busy))
            draft = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None)])
            values = [a["text"]["value"] for a in draft["units"][0]["arguments"]]
            service.second_check(draft, KEY, "gemini-3.8-flash")
            self.assertEqual([a["text"]["value"] for a in draft["units"][0]["arguments"]], values)
            notice = [issue for issue in draft["issues"] if "couldn't check the testimony of Alex Morgan on H.1111"
                      in issue["value"] and "(Gemini is busy.)" in issue["value"] and issue["flag"]]
            self.assertEqual([issue["evidence"] for issue in notice], [[draft["units"][0]["segment_ids"][0]]])
            keys.delete()
            service.second_check(draft)
            self.assertTrue(any("no Gemini key is saved" in issue["value"] for issue in draft["issues"]))

    def test_closing_the_app_during_the_check_is_not_reported_as_a_failed_check(self):
        def closing(prompt):
            raise InputError("The app is closing.")
        with tempfile.TemporaryDirectory() as folder:
            service = extraction.ExtractionService(Store(Path(folder)), threading.Event(), Keys(), Client(check_reply=closing))
            draft = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None)])
            issues = len(draft["issues"])
            with self.assertRaisesRegex(InputError, "closing"):
                service.second_check(draft, KEY, "gemini-3.8-flash")
            self.assertEqual(len(draft["issues"]), issues)

    def test_replies_in_the_wrong_form_fail_only_their_testimony_and_are_not_saved(self):
        replies = iter([{"items": 1}, {"items": [], "omissions": 5},
                        {"items": [{"id": ["/units/0/position"], "verdict": ["overstated"], "reason": "x", "quote": "y"},
                                   {**self.FLAG}], "omissions": [7]}])
        draft = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None)])
        draft["units"] += [copy.deepcopy(draft["units"][0]) for _ in range(2)]
        for n, unit in enumerate(draft["units"]):
            unit["id"] = f"unit-{n}"
        with tempfile.TemporaryDirectory() as folder:
            client = Client(check_reply=lambda prompt: next(replies))
            second_check.WORKERS, workers = 1, second_check.WORKERS  # In order, so each unit gets its reply.
            try:
                summary = second_check.run(draft, KEY, "gemini-3.8-flash", client, cache_dir=Path(folder))
            finally:
                second_check.WORKERS = workers
            self.assertEqual([row["unit"] for row in summary["failed"]], ["unit-0", "unit-1"])
            self.assertEqual(summary["flags"], 0)  # The bad row is dropped; the good one names unit 0's path.
            self.assertEqual(draft["second_check"], summary)
            self.assertEqual(len(list(Path(folder).glob("*.json"))), 1)  # Only the usable reply was kept.

    def test_closing_the_app_stops_the_testimonies_still_waiting(self):
        closing = threading.Event()

        def wait(seconds):
            if closing.wait(seconds):
                raise InputError("The app is closing.")

        def reply(prompt):
            closing.set()
            return {"items": [], "omissions": []}
        draft = drafted(hearing(*RIVERS), [morgan("H.1111", bill_check=None)])
        draft["units"] += [copy.deepcopy(draft["units"][0]) for _ in range(11)]
        client = Client(check_reply=reply)
        with self.assertRaises(InputError):
            second_check.run(draft, KEY, "gemini-3.8-flash", client, wait=wait)
        self.assertLessEqual(len(client.second), second_check.WORKERS)  # Only those already sent.



# A long hearing in miniature: the limits are patched down so its 32 lines need two parts.
WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")
PEOPLE = (("Alex Morgan", "H.1111", "House 1111"), ("Dana Avery", "H.4444", "House 4444"),
          ("Casey Lee", "H.3333", "House 3333"), ("Riley Chen", "H.1111", "House 1111"))


def long_hearing(pause=30):
    """Each testimony: the chair's hand-off after a pause, then seven lines from the testifier."""
    segments, spans, at = [], [], 0
    for name, _, spoken in PEOPLE:
        first = len(segments) + 1
        at += pause
        segments.append({"text": f"Thank you. Next we have {name} on {spoken}, please come forward now.", "start": at})
        for word in WORDS:
            at += 10
            segments.append({"text": f"I am {name} and my {word} point is that this bill helps {word} families.",
                             "start": at})
        spans.append((first, len(segments)))
    document = fixture("sample-hearing")
    document["hearing_url"] = LINK
    document["segments"] = [{"id": f"c{n}", "speaker": "Speaker 1", "start": seg["start"], "end": seg["start"] + 9,
                             "text": seg["text"]} for n, seg in enumerate(segments, 1)]
    return document, spans


class LongClient(Client):
    """Drafts whichever lines a prompt holds: a testimony whose hand-off isn't in them comes back with no
    testifier or bill, as Gemini is asked to do. ran_out: prompts (whole or part) that run out of room."""

    def __init__(self, spans, ran_out=lambda prompt: False, fail=None, payload=None, after=None):
        super().__init__()
        self.spans, self.ran_out, self.fail, self.payload, self.after = spans, ran_out, fail, payload, after

    def call_gemini(self, prompt, model, key, log=None, **_):
        if second_check.is_check(prompt):
            return super().call_gemini(prompt, model, key)
        self.prompts.append(prompt)
        if self.after:
            self.after()
        if self.fail and self.fail(prompt):
            raise gemini.GeminiError("Gemini is busy.")
        if self.payload:
            return self.payload, 5.0
        lines = {int(n): text for n, text in re.findall(r"^(\d+) \[[^\]]*\] (.*)$", prompt.split("Transcript:\n")[1], re.M)}
        if self.ran_out(prompt):
            return {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{"}]}}]}, 30.0
        entries = []
        for (name, bill, _), (first, last) in zip(PEOPLE, self.spans):
            shown = [n for n in range(first, last + 1) if n in lines]
            if not shown:
                continue
            said = [n for n in shown if n != first]
            entry = {"start_cue": shown[0], "end_cue": shown[-1], "testifiers": [], "bill": None, "bill_quote": None,
                     "bill_cues": [], "bill_check": "Bill not stated in these lines", "position": "Unclear",
                     "position_quote": None, "position_cues": [], "position_check": "Not stated in these lines",
                     "questions": [], "questions_note": "None asked.",
                     "arguments": [{"text": f"The {lines[n].split()[6]} point.", "quote": " ".join(lines[n].split()[:9]),
                                    "cues": [n]} for n in said]}
            if first in lines:
                quote = " ".join(lines[first].split()[2:8])
                entry.update(bill=bill, bill_quote=quote, bill_cues=[first], bill_check=None, position="Support",
                             position_quote=quote, position_cues=[first], position_check=None,
                             testifiers=[{"name": name, "organization": None, "quote": quote, "cues": [first]}])
            entries.append(entry)
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(
            {"entries": entries, "procedural": []})}]}}], "usageMetadata": {"promptTokenCount": 900,
                                                                           "candidatesTokenCount": 300}}, 12.5


class LongHearingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cache = Path(self.temporary.name) / "extraction-output"
        for name, value in (("MAX_WORDS", 300), ("PART_WORDS", 250), ("MIN_CUES", 4)):
            patcher = patch.object(gemini if name == "MAX_WORDS" else parts, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def draft(self, document, client, **options):
        return extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client, cache_dir=self.cache,
                                       fetch_agenda=lambda url: {"url": url, "bills": AGENDA}, **options)

    def test_a_long_hearing_is_drafted_in_parts_cut_at_a_hand_off_with_the_cut_flagged(self):
        document, spans = long_hearing()
        client = LongClient(spans)
        draft = self.draft(document, client)
        self.assertEqual(len(client.prompts), 2)
        self.assertTrue(all("This is only lines" in prompt and "of a 32-line transcript" in prompt
                            for prompt in client.prompts))
        ranges = [(p["first_cue"], p["last_cue"]) for p in draft["extraction_provenance"]["parts"]]
        hand_off = spans[2][0]  # Casey Lee's, after a pause near the middle.
        self.assertEqual(ranges, [(1, hand_off - 1), (hand_off, 32)])
        units = draft["units"]
        self.assertEqual([(u["testifiers"][0]["name"]["value"], u["bill"]["value"]) for u in units],
                         [(name, bill) for name, bill, _ in PEOPLE])
        self.assertEqual([len(u["arguments"]) for u in units], [7, 7, 7, 7])
        (cut,) = [i for i in draft["issues"] if "drafted in 2 parts" in i["value"]]
        at = gemini.clock(document["segments"][hand_off - 1]["start"])
        self.assertIn(f"one starts at {at}. Check the testimony just before and after", cut["value"])
        self.assertEqual(cut["evidence"], [f"c{hand_off - 1}", f"c{hand_off}"])
        self.assertTrue(cut["flag"])

    def test_a_testimony_a_cut_runs_through_is_joined_and_the_cut_says_so(self):
        document, spans = long_hearing()
        middle = spans[1][0] + 3  # Inside Dana Avery's testimony.
        client = LongClient(spans)
        draft = self.draft(document, client, ranges=[{"first_cue": 1, "last_cue": middle - 1},
                                                     {"first_cue": middle, "last_cue": 32}])
        units = draft["units"]
        self.assertEqual([u["testifiers"][0]["name"]["value"] for u in units], [p[0] for p in PEOPLE])
        dana = units[1]
        self.assertEqual((dana["bill"]["value"], dana["position"]["value"], len(dana["arguments"])),
                         ("H.4444", "Support", 7))
        self.assertEqual(dana["segment_ids"][-1], f"c{spans[1][1]}")
        (cut,) = [i for i in draft["issues"] if "drafted in 2 parts" in i["value"]]
        self.assertIn("Dana Avery's testimony ran across that point and was joined into one", cut["value"])

    def test_an_unnamed_testimony_after_a_hand_off_or_pause_is_never_given_to_the_person_before(self):
        def entries():
            return [{"part": 0, "start_cue": 1, "end_cue": 9, "testifiers": [{"name": "Alex Morgan"}],
                     "arguments": [{"text": "First point."}]},
                    {"part": 1, "start_cue": 10, "end_cue": 18, "testifiers": [], "arguments": [{"text": "Next point."}]}]

        def cues(text=None, gap=0):
            lines = [{"n": n, "text": "and so the point is that this bill matters", "start": n * 5.0 + (gap if n >= 10 else 0),
                      "end": n * 5.0 + 4.5 + (gap if n >= 10 else 0)} for n in range(1, 19)]
            if text:
                lines[8]["text"] = text
            return lines
        # The reviewer's cases: "Next" late in the chair's line, "Up now is", and a walk-up pause with no words.
        for case in (cues("Thank you so much for your testimony today, we really appreciate it. Next we have Casey Lee."),
                     cues("Thank you. Up now is Casey Lee."), cues(gap=40)):
            listed = entries()
            self.assertEqual(extraction.join_at_cuts(listed, [10], case, [], {}), [None])
            self.assertEqual(len(listed), 2)
        procedural = [{"start_cue": 9, "end_cue": 9, "summary": "The chair called the next witness."}]
        self.assertEqual(extraction.join_at_cuts(entries(), [10], cues(), procedural, {}), [None])
        # "next year" isn't a hand-off, and speech running on across the cut is joined, flagged.
        listed = entries()
        (whole,) = extraction.join_at_cuts(listed, [10], cues("We expect the costs to rise next year as well"), [], {})
        self.assertEqual((len(listed), whole["end_cue"], [a["text"] for a in whole["arguments"]]),
                         (1, 18, ["First point.", "Next point."]))
        self.assertEqual(whole["arguments"][1]["check"], extraction.JOINED)

    def test_an_introduction_cut_from_its_testimony_is_joined_even_if_the_name_was_heard_differently(self):
        # On a real hearing: the chair introduced "David Halloran", a 30 s gap, then the next part heard "Halran".
        cues = [{"n": n, "text": "and so the point is that this bill matters", "start": n * 5.0 + (30 if n >= 10 else 0),
                 "end": n * 5.0 + 4.5 + (30 if n >= 10 else 0)} for n in range(1, 31)]
        cues[5]["text"] = "Move on now to consideration of H.2011."

        def entries():
            return [{"part": 0, "start_cue": 7, "end_cue": 9, "testifiers": [{"name": "David Halloran", "check": None}],
                     "bill": "H.2011", "arguments": []},
                    {"part": 1, "start_cue": 10, "end_cue": 20, "testifiers": [{"name": "Halran"}], "bill": "H.2011",
                     "arguments": [{"text": "Treated as second class."}]},
                    {"part": 1, "start_cue": 10, "end_cue": 20, "testifiers": [{"name": "Halran"}], "bill": "H.1721",
                     "arguments": [{"text": "Ate at my desk."}]},
                    {"part": 1, "start_cue": 21, "end_cue": 30, "testifiers": [{"name": "Taylor Greene"}],
                     "arguments": [{"text": "Another person."}]}]
        listed = entries()
        (whole,) = extraction.join_at_cuts(listed, [10], cues, [], {})
        self.assertEqual([e["testifiers"][0]["name"] for e in listed], ["David Halloran", "Taylor Greene"])
        self.assertEqual([a["text"] for a in whole["arguments"]], ["Treated as second class.", "Ate at my desk."])
        self.assertIn("heard this name as Halran", whole["testifiers"][0]["check"])
        # But not when the chair moved on at the cut: then the introduction really had nothing after it.
        cues[8]["text"] = "Thank you. Next we have Halran."
        listed = entries()
        self.assertEqual(extraction.join_at_cuts(listed, [10], cues, [], {}), [None])
        self.assertEqual(len(listed), 4)

    def test_every_entry_a_cut_testimony_continues_in_is_joined_once_even_through_a_whole_part(self):
        cues = [{"n": n, "text": "and so the point is that this bill matters", "start": n * 5.0, "end": n * 5.0 + 4.5}
                for n in range(1, 41)]
        alex = [{"name": "Alex Morgan"}]
        listed = [{"part": 0, "start_cue": 1, "end_cue": 10, "testifiers": alex, "bill": "H.1111",
                   "arguments": [{"text": "Rivers."}]},
                  # One unnamed entry per bill, as the prompt asks, repeating a point.
                  {"part": 1, "start_cue": 11, "end_cue": 20, "testifiers": [], "bill": "H.1111",
                   "arguments": [{"text": "Rivers."}, {"text": "Stations."}]},
                  {"part": 1, "start_cue": 11, "end_cue": 20, "testifiers": [], "bill": "S.2222",
                   "arguments": [{"text": "Stations."}]},
                  {"part": 2, "start_cue": 21, "end_cue": 30, "testifiers": [{"name": "Morgan"}],
                   "arguments": [{"text": "Counties."}]},
                  {"part": 2, "start_cue": 31, "end_cue": 40, "testifiers": [{"name": "Dana Avery"}],
                   "arguments": [{"text": "Parks."}]}]
        titles = {"H.1111": "An Act about clean rivers", "S.2222": "An Act about clean rivers"}
        joined = extraction.join_at_cuts(listed, [11, 21], cues, [], titles)
        self.assertEqual([j is listed[0] for j in joined], [True, True])
        self.assertEqual(len(listed), 2)
        self.assertEqual(([a["text"] for a in listed[0]["arguments"]], listed[0]["bill"], listed[0]["end_cue"]),
                         (["Rivers.", "Stations.", "Counties."], "H.1111 / S.2222", 30))

    def test_a_failed_part_is_retried_alone_and_finished_parts_are_reused(self):
        document, spans = long_hearing()
        busy = LongClient(spans, fail=lambda prompt: f"\n{spans[3][0]} [" in prompt)  # The part holding Riley Chen.
        with self.assertRaisesRegex(InputError, r"failed on lines \d+–32 of this long hearing.*reused at no charge"):
            self.draft(document, busy)
        again = LongClient(spans)
        draft = self.draft(document, again)
        self.assertEqual(len(again.prompts), 1)
        self.assertEqual([p["reused_saved_reply"] for p in draft["extraction_provenance"]["parts"]], [True, False])

    def test_a_reply_that_runs_out_of_room_is_split_and_the_split_is_kept(self):
        document, spans = long_hearing()
        with patch.object(gemini, "MAX_WORDS", 30000):  # Fits in one call by words.
            whole = LongClient(spans, ran_out=lambda prompt: "This is only lines" not in prompt)
            draft = self.draft(document, whole)
            self.assertEqual(len(whole.prompts), 3)  # The whole, then its two halves.
            self.assertEqual(len(draft["extraction_provenance"]["parts"]), 2)
            provenance = draft["extraction_provenance"]
            self.assertEqual((provenance["seconds"], provenance["prompt_tokens"]), (55.0, 1800))  # All three calls.
            self.assertEqual(len(draft["units"]), 4)
            again = LongClient(spans, ran_out=lambda prompt: "This is only lines" not in prompt)
            self.draft(document, again)
            self.assertEqual(again.prompts, [])  # The cut-off reply is kept, so it isn't paid for again.

    def test_a_rebuild_keeps_the_first_drafts_cuts_even_if_cutting_changes(self):
        document, spans = long_hearing()
        first = self.draft(document, LongClient(spans))
        kept = first["extraction_provenance"]["parts"]
        with patch.object(parts, "split", lambda cues, count: [cues[:9], cues[9:]]):  # As if cutting changed.
            with self.assertRaisesRegex(InputError, "saved AI reply"):
                self.draft(document, store_module.SavedReplyOnly(), rebuilding=True)
            rebuilt = self.draft(document, store_module.SavedReplyOnly(), rebuilding=True, ranges=kept)
        self.assertEqual(rebuilt["extraction_provenance"]["parts"][1]["first_cue"], kept[1]["first_cue"])
        self.assertEqual(rebuilt["units"], first["units"])

    def test_a_blocked_part_or_closing_the_app_stops_further_paid_calls(self):
        document, spans = long_hearing()
        blocked = LongClient(spans, payload={"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
        with self.assertRaisesRegex(InputError, "declined to draft"):
            self.draft(document, blocked)
        self.assertEqual(len(blocked.prompts), 1)
        closing = threading.Event()

        def wait(seconds):
            if closing.wait(seconds):
                raise InputError("Drafting stopped because the app is closing.")
        shutting = LongClient(spans, after=closing.set)  # The app starts closing during the first call.
        with self.assertRaisesRegex(InputError, "app is closing"):
            self.draft(document, shutting, wait=wait)
        self.assertEqual(len(shutting.prompts), 1)
        again = LongClient(spans)
        self.draft(document, again)
        self.assertEqual(len(again.prompts), 1)  # The part that finished before closing was kept.

    def test_a_part_too_short_to_split_that_runs_out_of_room_keeps_the_finished_parts(self):
        document, spans = long_hearing()
        last = lambda prompt: f"\n{spans[3][0]} [" in prompt  # noqa: E731 - the part holding Riley Chen.
        with patch.object(parts, "MIN_CUES", 100):
            with self.assertRaisesRegex(InputError, r"ran out of room\. Retry this hearing once"):
                self.draft(document, LongClient(spans, ran_out=last))
            again = LongClient(spans)
            draft = self.draft(document, again)
        self.assertEqual(len(again.prompts), 1)  # Only the part that ran out is asked again.
        self.assertEqual(len(draft["units"]), 4)

    def test_saved_cuts_are_kept_only_if_they_still_cover_the_transcript_once(self):
        def ranges(*pairs):
            return [{"first_cue": a, "last_cue": b} for a, b in pairs]
        self.assertEqual(extraction._kept_ranges(ranges((1, 10), (11, 32)), 32), [(1, 10), (11, 32)])
        for bad in (ranges((1, 32)), ranges((1, 10), (12, 32)), ranges((1, 12), (11, 32)), ranges((2, 10), (11, 32)),
                    ranges((1, 10), (11, 31)), [{"first_cue": "x"}], None, "parts"):
            self.assertIsNone(extraction._kept_ranges(bad, 32), bad)


class SkippingClient(LongClient):
    """Like Gemini on one run of a real hearing: the whole-transcript reply lists Dana Avery's bill but drafts
    nothing for her. A prompt for only some lines is drafted in full (or not at all, with empty_again)."""

    def __init__(self, spans, empty_again=False, skip=(1,), empty_times=0, drop_bill=False, **options):
        super().__init__(spans, **options)
        self.empty_again, self.skip, self.empty_times, self.drop_bill = empty_again, skip, empty_times, drop_bill

    def call_gemini(self, prompt, model, key, log=None, **_):
        payload, seconds = super().call_gemini(prompt, model, key)
        if second_check.is_check(prompt):
            return payload, seconds
        redraft = "This is only lines" in prompt
        if redraft and self.empty_times:
            self.empty_times -= 1
        elif redraft and not self.empty_again:
            if self.drop_bill:  # The bill was named before these lines.
                reply = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
                for entry in reply["entries"]:
                    entry.update(bill=None, bill_quote=None, bill_cues=[], bill_check="Bill not stated")
                payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(reply)
            return payload, seconds
        reply = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
        for entry in reply["entries"]:
            if redraft or any(entry["start_cue"] == self.spans[k][0] for k in self.skip):
                entry.update(testifiers=[], arguments=[])
        payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(reply)
        return payload, seconds


class RedraftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cache = Path(self.temporary.name) / "extraction-output"
        patcher = patch.object(extraction, "REDRAFT_MIN", 5)  # The miniature testimony is 8 lines.
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def draft(self, document, client, **options):
        return extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client, cache_dir=self.cache,
                                       fetch_agenda=lambda url: {"url": url, "bills": AGENDA}, **options)

    def test_a_testimony_gemini_skipped_is_drafted_again_from_its_own_lines(self):
        document, spans = long_hearing()
        client = SkippingClient(spans)
        draft = self.draft(document, client)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn(f"This is only lines {spans[1][0]} to {spans[1][1]} of a 32-line transcript", client.prompts[1])
        self.assertEqual([u["testifiers"][0]["name"]["value"] for u in draft["units"]], [p[0] for p in PEOPLE])
        self.assertEqual((draft["units"][1]["bill"]["value"], len(draft["units"][1]["arguments"])), ("H.4444", 7))
        self.assertFalse([i for i in draft["issues"] if "drafted no testimony" in i["value"]])
        self.assertEqual(draft["extraction_provenance"]["redrafted"],
                         [{"first_cue": spans[1][0], "last_cue": spans[1][1], "recovered": 1, "gemini": "asked",
                           "tries": 1}])
        self.assertEqual(draft["units"][1]["segment_ids"], [f"c{n}" for n in range(spans[1][0], spans[1][1] + 1)])
        again = SkippingClient(spans)
        self.draft(document, again)
        self.assertEqual(again.prompts, [])  # Both replies were saved.

    def test_an_empty_redraft_keeps_the_warning_and_a_rebuild_never_pays_for_one(self):
        document, spans = long_hearing()
        client = SkippingClient(spans, empty_again=True)
        draft = self.draft(document, client)
        self.assertEqual(len(client.prompts), 3)  # The draft, a redraft, and one more try.
        (warning,) = [i["value"] for i in draft["issues"] if "drafted no testimony" in i["value"]]
        self.assertIn("Gemini listed H.4444", warning)
        self.assertEqual(draft["extraction_provenance"]["redrafted"][0]["recovered"], 0)
        # Drafted before redrafting existed: a rebuild finds no saved redraft, so it makes no call and keeps the warning.
        older = Path(self.temporary.name) / "older"
        with patch.object(extraction, "REDRAFT_MIN", 10 ** 6):
            extraction.gemini_draft(document, KEY, "gemini-3.8-flash", SkippingClient(spans), cache_dir=older,
                                    fetch_agenda=lambda url: {"url": url, "bills": AGENDA})
        rebuilt = extraction.gemini_draft(document, "", "gemini-3.8-flash", store_module.SavedReplyOnly(), cache_dir=older,
                                          fetch_agenda=lambda url: {"url": url, "bills": AGENDA}, rebuilding=True)
        self.assertEqual(len(rebuilt["units"]), 3)
        self.assertTrue([i for i in rebuilt["issues"] if "drafted no testimony" in i["value"]])

    def test_an_empty_redraft_is_tried_once_more_and_a_recovered_testimony_keeps_the_listed_bill(self):
        # A real testimony: skipped in the draft, empty on one redraft, found on the next, with no bill
        # in its own lines.
        document, spans = long_hearing()
        client = SkippingClient(spans, empty_times=1, drop_bill=True)
        draft = self.draft(document, client)
        self.assertEqual(len(client.prompts), 3)
        self.assertEqual([(r["recovered"], r["tries"]) for r in draft["extraction_provenance"]["redrafted"]], [(1, 2)])
        dana = draft["units"][1]
        self.assertEqual((dana["testifiers"][0]["name"]["value"], dana["bill"]["value"]), ("Dana Avery", "H.4444"))
        self.assertTrue(dana["bill"]["flag"].startswith(extraction.REDRAFT_BILL))
        self.assertFalse([i for i in draft["issues"] if "drafted no testimony" in i["value"]])
        again = SkippingClient(spans, empty_times=1, drop_bill=True)
        self.draft(document, again)
        self.assertEqual(again.prompts, [])  # Both tries were saved apart and reused.

    def test_only_lines_no_testimony_covers_are_redrafted_so_nothing_is_drafted_twice(self):
        document, spans = long_hearing()
        for empty in ({"start_cue": 1, "end_cue": 32}, {"start_cue": 3, "end_cue": 8}):  # The whole hearing; inside Alex's.
            class Stray(LongClient):
                def call_gemini(self, prompt, model, key, log=None, **_):
                    payload, seconds = super().call_gemini(prompt, model, key)
                    reply = json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
                    reply["entries"].insert(1, {**empty, "bill": "H.3333", "bill_quote": None, "bill_cues": [],
                                                "testifiers": [], "arguments": []})
                    payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(reply)
                    return payload, seconds
            client = Stray(spans)
            draft = extraction.gemini_draft(document, KEY, "gemini-3.8-flash", client,
                                            fetch_agenda=lambda url: {"url": url, "bills": AGENDA})
            self.assertEqual((len(client.prompts), len(draft["units"])), (1, 4), empty)

    def test_a_failed_redraft_stops_the_rest_and_redrafts_are_capped(self):
        document, spans = long_hearing()
        busy = SkippingClient(spans, skip=(1, 3), fail=lambda prompt: "This is only lines" in prompt)
        draft = self.draft(document, busy)
        self.assertEqual(len(busy.prompts), 2)  # The whole, one redraft that failed, and no second redraft.
        self.assertEqual([r["gemini"] for r in draft["extraction_provenance"]["redrafted"]], ["failed"])
        self.assertEqual(len([i for i in draft["issues"] if "drafted no testimony" in i["value"]]), 2)
        with patch.object(extraction, "REDRAFT_CALLS", 1):
            capped = SkippingClient(spans, skip=(1, 3))  # Not side by side: two separate redrafts.
            draft = extraction.gemini_draft(document, KEY, "gemini-3.8-flash", capped,
                                            fetch_agenda=lambda url: {"url": url, "bills": AGENDA})
        self.assertEqual(len(capped.prompts), 2)
        self.assertEqual(len(draft["units"]), 3)

    def test_closing_the_app_stops_a_redraft(self):
        document, spans = long_hearing()
        closing = threading.Event()

        def wait(seconds):
            if closing.wait(seconds):
                raise InputError("Drafting stopped because the app is closing.")
        client = SkippingClient(spans, after=closing.set)
        with self.assertRaisesRegex(InputError, "app is closing"):
            self.draft(document, client, wait=wait)
        self.assertEqual(len(client.prompts), 1)

    def test_a_testimony_with_the_chairs_seeing_none_well_inside_it_is_flagged(self):
        lines = ["I'm Alex Morgan with Example Legal Services."] + [f"Point {w} is that this bill helps {w} families."
                                                                     for w in WORDS[:4]]
        lines += ["Any questions? Seeing none, thank you very much."]
        lines += [f"Hello, I'm here in support. Item {w} matters for {w} people today." for w in WORDS[:6]]
        document = hearing(*lines)
        entry = morgan("H.1111", bill_check=None, start_cue=1, end_cue=12, arguments=[
            {"text": f"Point {i}.", "quote": " ".join(lines[i - 1].split()[:6]), "cues": [i]} for i in range(2, 13)])
        draft = drafted(document, [entry])
        (flag,) = [i for i in draft["issues"] if "may hold two" in i["value"]]
        self.assertIn("Alex Morgan's testimony may hold two: the chair closed one at 00:01:00", flag["value"])
        self.assertEqual(flag["evidence"], ["c6"])
        # One warning for a testimony on two bills that aren't companions, and no "Unknown's testimony".
        unknown = {**entry, "testifiers": [{**entry["testifiers"][0], "name": "Unknown"}]}
        draft = drafted(document, [unknown, {**unknown, "bill": "H.3333"}])
        (flag,) = [i["value"] for i in draft["issues"] if "may hold two" in i["value"]]
        self.assertTrue(flag.startswith("A testimony may hold two"), flag)
        lines[0] = "Seeing none, we move to Alex Morgan with Example Legal Services."  # Opening a testimony is normal.
        lines[5] = "Thank you very much."
        draft = drafted(hearing(*lines), [entry])
        self.assertFalse([i for i in draft["issues"] if "may hold two" in i["value"]])


class CutPlacementTests(unittest.TestCase):
    @staticmethod
    def cues(texts, starts):
        return [{"n": n, "text": text, "start": start, "end": start + 4} for n, (text, start) in
                enumerate(zip(texts, starts), 1)]

    def test_a_cut_lands_on_the_longest_pause_or_a_hand_off_near_the_middle(self):
        # 100 lines of 5 words: the even cut is before line 51, and a cut may move about 4 lines either way.
        def cut(texts, starts):
            return [p[0]["n"] for p in parts.split(self.cues(texts, starts), 2)][1]
        plain, flat = ["one two three four five"] * 100, [n * 5 for n in range(100)]
        self.assertEqual(cut(plain, flat), 51)
        far = [s + (100 if n >= 69 else 0) for n, s in enumerate(flat)]  # A long pause, but too far off.
        self.assertEqual(cut(plain, far), 51)
        paused = [s + (40 if n >= 52 else 0) for n, s in enumerate(flat)]  # A 40-second pause before line 53.
        self.assertEqual(cut(plain, paused), 53)
        texts = list(plain)
        texts[47] = "Thank you. Next we have the next panel."
        self.assertEqual(cut(texts, flat), 48)
        untimed = [{**c, "start": None, "end": None} for c in self.cues(texts, flat)]
        self.assertEqual([p[0]["n"] for p in parts.split(untimed, 2)][1], 48)
        # As on a real hearing: the chair's "We'll move now to ..." and then a long pause. The hand-off opens part 2.
        texts = list(plain)
        texts[48] = "We'll move now to the final bill"
        self.assertEqual(cut(texts, [s + (25 if n >= 49 else 0) for n, s in enumerate(flat)]), 49)
        texts[48] = "We expect costs to rise next year"  # Not a hand-off.
        self.assertEqual(cut(texts, flat), 51)
        # A hand-off beats a longer pause nearby (a 30 s gap inside a real testimony), and "Move on now to" is one.
        texts = list(plain)
        texts[48] = "Seeing none, thank you. Move on now to consideration of H.2011."
        self.assertEqual(cut(texts, paused), 49)

    def test_parts_cover_every_line_once_and_are_needed_only_over_the_limit(self):
        cues = self.cues(["word " * 50] * 30, range(0, 300, 10))
        for count in (2, 3, 5):
            pieces = parts.split(cues, count)
            self.assertEqual([c["n"] for piece in pieces for c in piece], list(range(1, 31)))
            self.assertTrue(all(pieces) and len(pieces) == count)
        with patch.object(gemini, "MAX_WORDS", 1500):
            self.assertEqual(parts.needed(cues), 1)
        with patch.object(gemini, "MAX_WORDS", 1499), patch.object(parts, "PART_WORDS", 600):
            self.assertEqual(parts.needed(cues), 3)
