"""Browser check of the first-session path, in WebKit (Safari's engine) or Chromium.

A developer tool; nothing here ships in the app. It runs the real app server on a temporary
workspace with a fake Gemini client and a fake Keychain (no network, no key), builds a draft
through the real gemini_draft code path, then drives the page and prints PASS, FAIL or SKIP
for each check. Exit status is 1 if any check fails.

  python scripts/friday_check.py --engine webkit|chromium [--real-hearing DIR] [--port N]

By default it uses the small synthetic hearing below. --real-hearing DIR uses a saved public
hearing folder instead (git-ignored data): its SRT, MP3, agenda-*.json and a saved Gemini 3.8
Flash reply (gemini-results*/gemini-3.8-flash-*/raw-response.json). Sound is never checked. The
listening checks assert on the audio element's position and paused state; where the browser
can't load the MP3 (possible with WebKit on Windows), a stand-in media element takes the same
calls, and the check prints an INFO line saying so. Set PLAYWRIGHT_BROWSERS_PATH if the browsers aren't in .scratch/playwright-browsers.

The rebuild button and the progress timer depend on server fields (can_rebuild, POST
/api/rebuild, stage_started_at, duration_seconds). They are checked against stubbed queue
replies in a second tab; the real server's fields are checked too when it sends them.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from hearing_compiler import agenda, audio_check, gemini, second_check  # noqa: E402
from hearing_compiler.pipeline import InputError, fields  # noqa: E402

FAKE_KEY = "AIzaFAKE-friday-check-0000000000000000"
SLOW_TITLE = "Stop-app check hearing"
STOPPING = "Stopping the app. Wait about 15 seconds before opening it again. Your reviews and corrections are saved."
STOP_QUESTION = ("A hearing is being transcribed or drafted right now. Stopping interrupts it, and you'll need "
                 "to click Retry next time. Stop anyway?")
COPY_FALLBACK = "Copy didn't work in this browser. Use Export Markdown and copy from that file."
NO_RESPONSE_MARK = "No response noted ⚠"
# Blank fields the copied note marks ⚠ (as in web/app.js). Next ⚠ visits these once the flags are done.
MARKED_BLANK = re.compile(r"^/units/\d+/(bill|position)$|/(name|organization|response_resolution)$")
IN_APP_SENTENCE = ("Every draft still needs your review: items the AI wasn't sure about are marked ⚠ (Next ⚠ jumps "
                   "to them), hearing-wide checks are listed under Other things to check, and timestamps jump to "
                   "the audio. Existing completed reviews are kept.")
INFERRED_ASKER = "Who asked is inferred from context; confirm by listening."
REBUILD_QUESTION = ("Rebuild this draft with the app's latest fixes? It reuses the saved AI reply. The new second "
                    "check may cost a few cents the first time it runs. Nothing you've reviewed is affected.")
SECOND_REASON = "The speaker hedged this; the draft states it as certain. (Fake second check.)"
MISSED_POINT = "A point the draft left out. (Fake second check.)"
# The legislator name check, faked: "Senator Lee" asks a question, and the member list has Morgan Lees.
COMMITTEE = "Joint Committee on Example Matters"
ROSTER = {"legislators": [{"name": "Jordan Hale", "code": "JXD1", "branch": "House"},
                          {"name": "Morgan Lees", "code": "MXL0", "branch": "Senate"}],
          "committee": {"name": COMMITTEE, "url": "https://malegislature.gov/Committees/Detail/J99/Members",
                        "members": [{"name": "Jordan Hale", "code": "JXD1", "branch": "House", "role": "Chair"},
                                    {"name": "Morgan Lees", "code": "MXL0", "branch": "Senate", "role": "Chair"},
                                    {"name": "Casey Wren", "code": "CXW1", "branch": "House", "role": None}]},
          "fetched_utc": "2026-10-21T00:00:00+00:00"}
AUDIO_HEARD = "fourteen (fake audio check)"
ROSTER_FLAG = "⚠ Check: The Legislature's member list has Morgan Lees; check the spelling."
REBUILD_STUB_ERROR = ("The saved AI reply for this draft wasn't found, so it can't be rebuilt without a new paid call. "
                      "(Stubbed by the check.)")
LATE = "This has taken over an hour. If it doesn't finish soon, write a comment on the checklist."
AUDIO = """() => { const a = document.querySelector("#source-audio");
  return {time: a.currentTime, paused: a.paused, ready: a.readyState, duration: a.duration}; }"""
# Where the browser can't decode the recording, this takes the app's media calls on the same
# element: seeking sets the position, play and pause set the paused state. No sound, no clock.
MEDIA_STANDIN = """(duration) => {
  const audio = document.querySelector("#source-audio");
  if (audio.__standin) return;
  let position = 0, paused = true;
  const fire = (name) => audio.dispatchEvent(new Event(name));
  Object.defineProperties(audio, {
    readyState: {get: () => 4, configurable: true},
    duration: {get: () => duration, configurable: true},
    paused: {get: () => paused, configurable: true},
    currentTime: {get: () => position, configurable: true,
                  set: (value) => { position = Math.min(Math.max(Number(value) || 0, 0), duration); fire("timeupdate"); }},
  });
  audio.play = () => { if (paused) { paused = false; fire("play"); } return Promise.resolve(); };
  audio.pause = () => { if (!paused) { paused = true; fire("pause"); } };
  audio.load = () => {};
  audio.__standin = true;
  fire("loadedmetadata");
}"""

# A small fictional hearing: four testimonies, seven flagged fields (one a questioner inferred from
# context), one comment with no reply, a 28-second stretch no testimony covers and an agenda bill
# that is mentioned but has no entry.
CUES = [
    (0, 6, "Good morning and welcome to this hearing of the Joint Committee on Example Matters."),
    (6, 12, "I'm Representative Jordan Hale, House chair. Our first panel will testify on House Bill 1001."),
    (12, 20, "Thank you, Chair. I'm Alex Morgan with Example Legal Services and I support House Bill 1001."),
    (20, 28, "The bill would expand access to legal help for tenants facing eviction in housing court."),
    (28, 36, "Last year about four thousand tenants appeared without a lawyer in our county alone."),
    (36, 42, "Who would pay for the expanded program under this bill?"),
    (42, 50, "The program would be funded through the existing court fee account, without new taxes."),
    (50, 56, "Thank you. Next we will hear testimony on House Bill 1002."),
    (56, 64, "Good morning. My name is Priya Raman, I'm testifying for myself as a parent, and I oppose "
             "House Bill 1002."),
    (64, 72, "The reporting rule in this bill would add months of paperwork for small family day care programs."),
    (72, 80, "Many providers in my town already struggle to meet the current inspection schedule."),
    (80, 86, "Senator Lee: Thank you for coming in today and sharing your family's experience with us."),
    (86, 92, "Our next witness will speak to Senate Bill 2003."),
    (92, 100, "I'm Dana Whitfield from the Example Bar Association. We have concerns about Senate Bill 2003."),
    (100, 108, "The appeal deadline in section two is shorter than in any neighboring state."),
    (108, 116, "We would ask the committee to extend the filing window to sixty days."),
    (116, 124, "Representative Hale: Does the association support the rest of the bill?"),
    (124, 132, "We support the goal, but we cannot support the bill as filed."),
    (132, 140, "We will now take a short break while we fix the microphone in the back of the room."),
    (140, 150, "Testing, testing. Can the people on the livestream hear us now?"),
    (150, 160, "It sounds like we have audio again. Someone also asked about House Bill 1005 during the break."),
    (160, 168, "Our last witness is on House Bill 1004."),
    (168, 176, "Hi, I'm Sam Ortiz with the Example Tenants Union, and we support House Bill 1004."),
    (176, 184, "It would require landlords to give ninety days of notice before a rent increase."),
    (184, 192, "Thank you all. This hearing is adjourned."),
]
AGENDA = [
    {"bill": "H.1001", "title": "An Act expanding access to counsel in eviction cases", "sponsor": "Example Sponsor"},
    {"bill": "H.1002", "title": "An Act relative to reporting by family child care programs", "sponsor": "Example Sponsor"},
    {"bill": "S.2003", "title": "An Act relative to appeal deadlines", "sponsor": "Example Sponsor"},
    {"bill": "H.1004", "title": "An Act relative to notice of rent increases", "sponsor": "Example Sponsor"},
    {"bill": "H.1005", "title": "An Act relative to court interpreters", "sponsor": "Example Sponsor"},
]


def _entry(start, end, bill, bill_quote, person, position, position_quote, position_cue, arguments, questions,
           position_check=None):
    return {"start_cue": start, "end_cue": end, "bill": bill, "bill_title": None, "bill_quote": bill_quote,
            "bill_cues": [start], "bill_check": None, "testifiers": [person], "position": position,
            "position_quote": position_quote, "position_cues": [position_cue], "position_check": position_check,
            "arguments": arguments, "questions": questions, "questions_note": None}


def _item(text, quote, cue, check=None):
    return {"text": text, "quote": quote, "cues": [cue], "check": check}


REPLY = {"entries": [
    _entry(3, 7, "H.1001", "I support House Bill 1001",
           {"name": "Alex Morgan", "role": None, "organization": "Example Legal Services",
            "quote": "I'm Alex Morgan with Example Legal Services", "cues": [3], "check": None},
           "Support", "I support House Bill 1001", 3,
           [_item("Expands access to legal help for tenants facing eviction.",
                  "expand access to legal help for tenants facing eviction", 4),
            _item("About 4,000 tenants in the county had no lawyer last year.",
                  "about four thousand tenants appeared without a lawyer", 5,
                  "Figure heard once; confirm the number by listening.")],
           [{"asker": "Representative Jordan Hale", "question": "Who would pay for the expanded program?",
             "respondent": "Alex Morgan", "response": "The existing court fee account, without new taxes.",
             "quote": "Who would pay for the expanded program under this bill", "cues": [6], "check": None}]),
    _entry(9, 12, "H.1002", "I oppose House Bill 1002",
           {"name": "Priya Raman", "role": None, "organization": None, "quote": "My name is Priya Raman",
            "cues": [9], "check": "Name heard once; confirm the spelling."},
           "Oppose", "I oppose House Bill 1002", 9,
           [_item("The reporting rule would add months of paperwork for small family day care programs.",
                  "would add months of paperwork for small family day care programs", 10),
            _item("Providers already struggle to meet the current inspection schedule.",
                  "already struggle to meet the current inspection schedule", 11)],
           [{"asker": "Senator Lee", "question": "Thanked the witness for sharing her family's experience.",
             "respondent": None, "response": None,
             "quote": "Thank you for coming in today and sharing your family's experience", "cues": [12],
             "check": None}]),
    _entry(14, 18, "S.2003", "We have concerns about Senate Bill 2003",
           {"name": "Dana Whitfield", "role": None, "organization": "Example Bar Association",
            "quote": "I'm Dana Whitfield from the Example Bar Association", "cues": [14], "check": None},
           "Oppose", "we cannot support the bill as filed", 18,
           [_item("The appeal deadline in section 2 is shorter than in any neighboring state.",
                  "The appeal deadline in section two is shorter than in any neighboring state", 15),
            _item("Asks the committee to extend the filing window to 60 days.",
                  "extend the filing window to sixty days", 16, "Number heard once; confirm sixty days.")],
           [{"asker": "Representative Jordan Hale", "question": "Does the association support the rest of the bill?",
             "respondent": "Dana Whitfield", "response": "It supports the goal but not the bill as filed.",
             "quote": "Does the association support the rest of the bill", "cues": [17],
             "check": "Who answered is inferred; confirm by listening."}],
           position_check="Concerns stated; opposition is inferred from 'cannot support the bill as filed'."),
    _entry(23, 24, "H.1004", "we support House Bill 1004",
           {"name": "Sam Ortiz", "role": None, "organization": "Example Tenants Union",
            "quote": "I'm Sam Ortiz with the Example Tenants Union", "cues": [23],
            "check": "Surname heard once; confirm the spelling."},
           "Support", "we support House Bill 1004", 23,
           [_item("Requires landlords to give 90 days' notice before a rent increase.",
                  "require landlords to give ninety days of notice before a rent increase", 24)],
           []),
], "procedural": [{"start_cue": n, "end_cue": m, "summary": "Chair procedure."}
                  for n, m in ((1, 2), (8, 8), (13, 13), (22, 22), (25, 25))]}

# Records each clipboard write and whether it began while the click was still being handled
# (window capture listeners run first and window bubble listeners last).
CLIPBOARD_SPY = """(() => {
  window.__copies = [];
  window.__inClick = false;
  window.addEventListener("click", () => { window.__inClick = true; }, true);
  window.addEventListener("click", () => { window.__inClick = false; });
  const clipboard = navigator.clipboard;
  if (!clipboard) return;
  const track = (entry, result) => {
    window.__copies.push(entry);
    result.then(() => { entry.ok = true; }, (error) => { entry.ok = false; entry.error = `${error.name}: ${error.message}`; });
    return result;
  };
  if (clipboard.write) {
    const write = clipboard.write.bind(clipboard);
    clipboard.write = (items) => {
      const entry = {via: "write", sync: window.__inClick};
      const result = track(entry, write(items));
      try {
        Promise.resolve(items[0].getType("text/plain")).then((blob) => blob.text())
          .then((text) => { entry.text = text; }, () => {});
      } catch (error) {}
      return result;
    };
  }
  if (clipboard.writeText) {
    const writeText = clipboard.writeText.bind(clipboard);
    clipboard.writeText = (text) => track({via: "writeText", sync: window.__inClick, text: String(text)}, writeText(text));
  }
})();"""
SHOWN = """() => {
  const field = document.activeElement?.closest("[data-field]");
  if (!field) return null;
  const box = field.getBoundingClientRect();
  return {path: field.dataset.field, flagged: field.classList.contains("flagged"),
          visible: box.bottom > 0 && box.top < innerHeight};
}"""


# Next ⚠'s stop after the flags: the blank field (or No questions button) and its one-click fix.
BLANK_SHOWN = """() => {
  const active = document.activeElement;
  const field = active?.closest("[data-field]");
  const target = field || (active?.matches("[data-no-questions]") ? active : null);
  if (!target) return null;
  const path = field ? field.dataset.field : `/units/${active.dataset.noQuestions}/questions_none`;
  const exchange = path.replace(/\\/response_resolution$/, "");
  const fix = !field ? active : field.querySelector("[data-na]")
    || [...document.querySelectorAll("#review-view [data-no-response]")].find((node) => node.dataset.noResponse === exchange);
  const name = fix && ["data-na", "data-no-response", "data-no-questions"].find((attribute) => fix.hasAttribute(attribute));
  const box = target.getBoundingClientRect(), fixBox = fix?.getBoundingClientRect();
  return {path, blank: target.classList.contains("blank"), visible: box.bottom > 0 && box.top < innerHeight,
          fix: name || null, value: name ? fix.getAttribute(name) : null,
          fix_visible: Boolean(fixBox && fixBox.top >= 0 && fixBox.bottom <= innerHeight)};
}"""


def srt(cues) -> str:
    def clock(seconds):
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02},000"
    return "".join(f"{n}\n{clock(a)} --> {clock(b)}\n{text}\n\n" for n, (a, b, text) in enumerate(cues, 1))


def clock(seconds) -> str:
    """The review page's player time (web/app.js clock): 2:05, 12:35, 1:02:03."""
    n = max(0, int(seconds))
    return f"{n // 3600}:{n // 60 % 60:02}:{n % 60:02}" if n >= 3600 else f"{n // 60}:{n % 60:02}"


def is_timed(segment) -> bool:
    return bool(segment) and not segment.get("timing_unavailable") and isinstance(segment.get("start"), (int, float))


def first_timed(draft, ids):
    """The earliest timed segment among evidence ids, as "Listen from" picks it."""
    segments = {segment["id"]: segment for segment in draft["segments"]}
    return min((segments[i] for i in ids or [] if is_timed(segments.get(i))), key=lambda s: s["start"], default=None)


def evidence_under(node) -> list:
    """Every evidence and segment id inside a draft item, as a repair dialog's Listen collects them."""
    if isinstance(node, list):
        return [i for child in node for i in evidence_under(child)]
    if not isinstance(node, dict):
        return []
    return [i for key, child in node.items() if key != "review_log"
            for i in (child if key in ("evidence", "segment_ids") and isinstance(child, list) else evidence_under(child))]


def at(draft, path):
    node = draft
    for part in path.strip("/").split("/"):
        node = node[int(part)] if part.isdigit() else node[part]
    return node


def iso_ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def reply_payload(reply) -> dict:
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(reply)}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}, "modelVersion": "fake"}


class FakeKeys:
    """Stands in for the Mac Keychain; the key stays in this process."""

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


def check_reply(prompt) -> dict:
    """The fake second check: testimony 1's first argument is flagged and one point is listed as missed, each
    quoting its own transcript lines (no digits, so no bill number). Other testimonies pass."""
    drafted, lines = prompt.split("DRAFT ITEMS (id | kind | text):\n", 1)[1].split("\n\nTRANSCRIPT LINES:\n", 1)
    argument = next((line.split(" | ")[0] for line in drafted.splitlines() if line.startswith("/units/0/arguments/")), None)
    quotes = [" ".join(words[:8]) for words in (re.sub(r"^\d+ \[[^\]]*\] ", "", line).split() for line in lines.splitlines())
              if len(words) >= 8 and not re.search(r"\d", " ".join(words[:8]))]
    if argument is None or len(quotes) < 2:
        return {"items": [], "omissions": []}
    return {"items": [{"id": argument, "verdict": "overstated", "reason": SECOND_REASON, "quote": quotes[0]}],
            "omissions": [{"point": MISSED_POINT, "quote": quotes[-1]}]}


class FakeGemini:
    """Returns saved replies. The stop check's hearing waits until released, so it stays 'Analyzing'.

    Second-check prompts are kept apart in checks, so "one drafting call" counts stay exact."""

    def __init__(self, payload):
        self.payload, self.prompts, self.checks, self.audio = payload, [], [], []
        self.slow_started, self.release = threading.Event(), threading.Event()

    def check_key(self, key, model, **_):
        return {"name": f"models/{model}"}

    def call_gemini(self, prompt, model, key, log=None, **options):
        if audio_check.is_check(prompt):  # The first clip ever sent "sounds different"; the rest match.
            self.audio.append(options.get("parts"))
            first = len(self.audio) == 1
            return reply_payload({"items": [{"clip": 1, "verdict": "different", "heard": AUDIO_HEARD}] if first
                                  else []}), 1.0
        if second_check.is_check(prompt):
            self.checks.append(prompt)
            return reply_payload(check_reply(prompt)), 1.0
        self.prompts.append(prompt)
        if SLOW_TITLE in prompt:
            self.slow_started.set()
            self.release.wait(120)
            return reply_payload(REPLY), 1.0
        return copy.deepcopy(self.payload), 1.0


class FailingTranscriber:
    """Stands in for MacWhisper's mw when it refuses to transcribe."""

    def run(self, path):
        raise InputError("MacWhisper failed: Error: transcribing from the command line needs MacWhisper Pro. "
                         "(Simulated by the check.)")


def _blocked(*_, **__):
    raise RuntimeError("This check never calls the real Gemini API.")


def synthetic_inputs() -> dict:
    from audio_test_helpers import mp3_bytes
    name = "2026-10-16-example-hearing"
    return {"label": "synthetic hearing", "agenda": AGENDA, "committee": COMMITTEE, "payload": reply_payload(REPLY),
            "recording": {"name": f"{name}.mp3", "mimeType": "audio/mpeg", "buffer": mp3_bytes(200)},
            "transcript": {"name": f"{name}.srt", "mimeType": "text/plain", "buffer": srt(CUES).encode("utf-8")},
            "hearing": {"title": "Example hearing on tenant and family bills", "date": "2026-10-16",
                        "committee": "Joint Committee on Example Matters",
                        "url": "https://malegislature.gov/Events/Hearings/Detail/9999"}}


def real_inputs(folder: Path) -> dict:
    def pick(*patterns, prefer=""):
        for pattern in patterns:
            found = sorted(folder.glob(pattern))
            if found:
                return sorted(found, key=lambda path: prefer not in path.name)[0] if prefer else found[-1]
        raise SystemExit(f"--real-hearing: nothing matches {' or '.join(patterns)} in {folder}")
    saved = json.loads(pick("agenda-*.json", "gemini-results*/gemini-3.8-flash-*/agenda.json").read_text(encoding="utf-8"))
    reply = pick("gemini-results*/gemini-3.8-flash-*/raw-response.json")
    hearing = saved["hearing"]
    return {"label": f"real hearing ({folder.name}, reply {reply.parent.name})", "agenda": saved["agenda"],
            "payload": json.loads(reply.read_text(encoding="utf-8")),
            "recording": str(pick("*.mp3", prefer="mono64k")), "transcript": str(pick("**/*.srt")),
            "hearing": {"title": hearing["title"], "date": hearing["date"], "committee": hearing["committee"],
                        "url": hearing["source_url"]}}


class Results:
    def __init__(self):
        self.statuses = []

    def report(self, status, name, detail=""):
        self.statuses.append(status)
        print(f"{status} {name}" + (f" ({detail})" if detail else ""), flush=True)

    def __call__(self, name, ok, failure="", success=""):
        self.report("PASS" if ok else "FAIL", name, success if ok else failure)
        return bool(ok)


class Session:
    def __init__(self, page, context, base, server, fake, inputs, engine, results, expect):
        self.page, self.context, self.base, self.server, self.fake = page, context, base, server, fake
        self.inputs, self.engine, self.check, self.expect = inputs, engine, results, expect
        self.errors, self.requests, self.dialogs = [], [], []
        self.accept_dialogs, self.stopped, self.no_response, self.real_rebuild = True, False, None, False
        page.on("pageerror", lambda error: self.errors.append(f"page error: {error}"))
        page.on("console", self._console)
        page.on("request", lambda request: self.requests.append((request.method, urlparse(request.url).path)))
        page.on("dialog", self._dialog)

    def _console(self, message):
        url = (message.location or {}).get("url", "")
        # Browsers ask for a favicon the app doesn't have; media support differs by platform.
        if message.type == "error" and not url.endswith("/favicon.ico") and "/api/audio/" not in url:
            self.errors.append(f"console error: {message.text}")

    def _dialog(self, dialog):
        self.dialogs.append(dialog.message)
        dialog.accept() if self.accept_dialogs else dialog.dismiss()

    # Server-side helpers.
    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=60) as response:
            return json.loads(response.read())

    def text(self, path):
        with urllib.request.urlopen(self.base + path, timeout=60) as response:
            return response.read().decode("utf-8")

    def post(self, path, body):
        token = self.get("/api/session")["token"]
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "X-Review-Token": token})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    def job(self, job_id):
        return next(job for job in self.get("/api/hearings") if job["id"] == job_id)

    def job_when(self, job_id, condition, timeout, what):
        return self.wait_for(lambda: (lambda job: job if condition(job) else None)(self.job(job_id)), timeout, what)

    def within(self, condition, timeout):
        deadline = time.monotonic() + timeout
        while True:
            value = condition()
            if value or time.monotonic() > deadline:
                return value
            self.page.wait_for_timeout(250)  # Playwright delivers page events only during its own calls.

    def wait_for(self, condition, timeout, what):
        value = self.within(condition, timeout)
        if not value:
            raise AssertionError(f"Timed out waiting for {what}")
        return value

    def note(self, job_id):
        return self.text(f"/api/export/{job_id}?format=md").replace("\r\n", "\n")

    # Page helpers.
    def visible(self, selector, timeout=10000):
        try:
            self.expect(self.page.locator(selector).first).to_be_visible(timeout=timeout)
            return True
        except AssertionError:
            return False

    def settle(self):
        """Wait for smooth scrolling to stop."""
        last, stable = None, 0
        for _ in range(100):
            position = self.page.evaluate("Math.round(scrollY)")
            stable = stable + 1 if position == last else 0
            if stable >= 3:
                return
            last = position
            self.page.wait_for_timeout(80)

    def revision(self):
        return self.page.evaluate("state.job.revision")

    def saved_after(self, revision):
        self.page.wait_for_function("(r) => !state.busy && state.job && state.job.revision > r", arg=revision,
                                    timeout=30000)

    def next_flag(self):
        self.page.locator('#review-view [data-action="next-flag"]').click()
        self.settle()
        return self.page.evaluate(SHOWN)

    def to_check(self):
        return int(re.match(r"\d+", self.page.locator("#review-view .review-tools strong").inner_text()).group())

    def repair(self, op, path_pattern):
        buttons = self.page.locator("#review-view [data-repair]")
        for index, raw in enumerate(buttons.evaluate_all("nodes => nodes.map(n => n.dataset.repair)")):
            operation = json.loads(raw)
            if operation["op"] == op and re.fullmatch(path_pattern, operation.get("path", "")):
                buttons.nth(index).click()
                self.expect(self.page.locator("#structure-dialog")).to_be_visible()
                return operation
        return None

    def audio(self):
        return self.page.evaluate(AUDIO)

    def pause_at(self, seconds):
        self.page.evaluate("(t) => { const a = document.querySelector('#source-audio'); a.pause(); a.currentTime = t; }",
                           seconds)

    def moved_by(self, button):
        """How far a click moves the position. Waits out any seek first: WebKit can land an MP3 seek
        a little away from the time asked for, which is the browser's, not the button's, doing."""
        self.within(lambda: not self.page.evaluate("document.querySelector('#source-audio').seeking"), 5)
        before = self.audio()["time"]
        button.click()
        return self.audio()["time"] - before

    def playing_from(self, start, timeout=10):
        """The audio element once it is playing from start (playback may have moved on a little)."""
        def ready():
            audio = self.audio()
            return audio if start - 0.3 <= audio["time"] <= start + 3 and not audio["paused"] else None
        return self.within(ready, timeout) or self.audio()

    def media(self, draft):
        """Waits for the recording's metadata; where this browser can't load it, installs the stand-in."""
        if self.within(lambda: self.audio()["ready"] >= 1, 15):
            return "the recording"
        ends = [s.get("end") or s["start"] for s in draft["segments"] if is_timed(s)]
        self.page.evaluate(MEDIA_STANDIN, max(ends, default=0) + 30)
        print(f"INFO {self.engine} can't load the MP3 here, so the listening checks use a stand-in media element "
              "(position and paused state, no sound)", flush=True)
        return "a stand-in media element"

    def player_layout(self):
        """Scrolls halfway down the review and reports where the player and a fresh toast sit."""
        return self.page.evaluate("""() => {
          window.scrollTo(0, (document.documentElement.scrollHeight - innerHeight) / 2);
          toast("Checking where messages appear.");
          const player = document.querySelector("#audio-player"), box = player.getBoundingClientRect();
          const message = document.querySelector("#toast").getBoundingClientRect();
          const play = document.querySelector("#audio-play").getBoundingClientRect();
          document.querySelector("#toast").hidden = true;
          return {on_screen: box.top >= 0 && box.bottom <= innerHeight, sticky: getComputedStyle(player).position,
                  toast_above: message.bottom <= box.top + 1, play: Math.round(play.width),
                  skip: Boolean(document.querySelector("#audio-back") && document.querySelector("#audio-forward")),
                  fits: document.documentElement.scrollWidth <= innerWidth, height: Math.round(box.height)};
        }""")

    def listening(self, job_id):
        """▶ times, the bottom player, Listen in the Edit and repair dialogs, and inferred questioners."""
        page, check, expect = self.page, self.check, self.expect
        draft = self.get(f"/api/hearings/{job_id}")["draft"]
        segments = {segment["id"]: segment for segment in draft["segments"]}
        source = self.media(draft)
        note = page.locator("#review-view .draft-pane > p.muted").first.inner_text()
        check("the Gemini draft note says unmarked items haven't been checked yet",
              "Unmarked items haven't been checked yet" in note and "usually right" not in page.locator("body").inner_text(),
              repr(note))

        buttons = page.locator("#review-view [data-evidence]").evaluate_all(
            "nodes => nodes.map(n => ({id: n.dataset.evidence, text: n.textContent, label: n.getAttribute('aria-label')}))")
        wrong = []
        for button in buttons:
            segment = segments[button["id"]]
            want = ((f"▶ {clock(segment['start'])}", f"Play the recording from {clock(segment['start'])}")
                    if is_timed(segment) else (f"Text line {segment['source_line']}",
                                               f"Show in the transcript: Text line {segment['source_line']}"))
            if (button["text"], button["label"]) != want:
                wrong.append({**button, "want": want})
        late = sum(is_timed(segments[b["id"]]) and segments[b["id"]]["start"] >= 3600 for b in buttons)
        check("every field and transcript time is a ▶ button (m:ss, h:mm:ss past an hour) with a Play label",
              buttons and not wrong, f"{len(wrong)} of {len(buttons)} wrong, e.g. {wrong[:2]}",
              f"{len(buttons)} buttons, {late} past the first hour")

        # A field's ▶: plays from its time, highlights the passage and says what's playing.
        cited = page.locator("#review-view [data-field] .evidence-links [data-evidence]").evaluate_all(
            "nodes => nodes.map(n => ({id: n.dataset.evidence, path: n.closest('[data-field]').dataset.field}))")
        argument = next((c for c in cited if re.fullmatch(r"/units/\d+/arguments/\d+/text", c["path"])
                         and segments[c["id"]]["start"] >= 5), None) or cited[0]
        start = segments[argument["id"]]["start"]
        unit = int(argument["path"].split("/")[2]) + 1
        self.pause_at(0)
        page.locator(f'#review-view [data-field="{argument["path"]}"] [data-evidence="{argument["id"]}"]').click()
        state = self.playing_from(start)
        label = page.locator("#player-now").inner_text()
        selected = page.locator("#review-view .segment.selected").evaluate_all("nodes => nodes.map(n => n.dataset.segment)")
        check("a field's ▶ plays the recording from its time, shows the passage and says what's playing",
              start - 0.3 <= state["time"] <= start + 3 and not state["paused"] and selected == [argument["id"]]
              and label == f"Testimony {unit} · Main argument · from {clock(start)}",
              f"{source}: {state}, label {label!r}, selected {selected}", f"{label} ({source})")

        segment = next(s for s in draft["segments"][len(draft["segments"]) // 3:] if is_timed(s) and s["id"] != argument["id"])
        self.pause_at(0)
        page.locator(f'#review-view .evidence-content [data-segment="{segment["id"]}"] [data-evidence]').click()
        state = self.playing_from(segment["start"])
        label = page.locator("#player-now").inner_text()
        speaker = "" if segment.get("speaker_unavailable") else f"{segment['speaker']} · "
        check("a transcript ▶ plays from that passage",
              segment["start"] - 0.3 <= state["time"] <= segment["start"] + 3 and not state["paused"]
              and label == f"Transcript · {speaker}from {clock(segment['start'])}", f"{state}, label {label!r}", label)

        # The bottom player: on screen while reviewing, larger play button, and ±10 s.
        self.pause_at(segment["start"] + 2)
        forward, back = self.moved_by(page.locator("#audio-forward")), self.moved_by(page.locator("#audio-back"))
        layouts = {}
        for width, height in ((1440, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            layouts[width] = self.player_layout()
        # A half-width window stacks the transcript under the draft: ▶ must not carry her away from her place.
        page.set_viewport_size({"width": 720, "height": 800})
        chip = page.locator(f'#review-view [data-field="{argument["path"]}"] [data-evidence="{argument["id"]}"]')
        chip.scroll_into_view_if_needed()
        self.settle()
        before = chip.evaluate("node => node.getBoundingClientRect().top")
        self.pause_at(0)
        chip.click()
        self.settle()
        moved = chip.evaluate("node => node.getBoundingClientRect().top") - before
        check("on a half-width window, a field's ▶ keeps her place in the draft", abs(moved) < 60, f"moved {moved:.0f} px")
        page.set_viewport_size({"width": 1440, "height": 900})
        check("the bottom player stays on screen at 1440 and 390 px wide, with −10 s and +10 s, under any message",
              abs(forward - 10) < 2.5 and abs(back + 10) < 2.5 and all(
                  item["on_screen"] and item["sticky"] == "sticky" and item["toast_above"] and item["skip"]
                  and item["fits"] and item["play"] >= 40 for item in layouts.values()),
              f"+10 s moved {forward:.2f} s, -10 s moved {back:.2f} s; {layouts}",
              f"player {layouts[1440]['height']} px tall wide, {layouts[390]['height']} px narrow")

        # Edit dialog: modal, so it has its own Listen controls on the same audio element.
        item = at(draft, argument["path"])
        first = first_timed(draft, item["evidence"])
        page.locator(f'#review-view [data-edit="{argument["path"]}"]').click()
        expect(page.locator("#edit-dialog")).to_be_visible()
        bar = page.locator("#edit-listen")
        shown = bar.is_visible() and bar.locator('[data-audio="listen"]').inner_text()
        result = {"shown": shown}
        if shown:
            self.pause_at(0)
            bar.locator('[data-audio="listen"]').click()
            result["listen"] = self.playing_from(first["start"])
            toggle = bar.locator('[data-audio="toggle"]')
            toggle.click()
            result["toggle"] = self.within(lambda: toggle.inner_text() == "Play" and "Play", 5) or toggle.inner_text()
            result["pause"] = self.audio()
            result["forward"] = self.moved_by(bar.locator('[data-audio="forward"]'))
            result["back"] = self.moved_by(bar.locator('[data-audio="back"]'))
            result["label"] = page.locator("#player-now").text_content()
        page.locator("#cancel-edit").click()
        expect(page.locator("#edit-dialog")).to_be_hidden()
        listened = result.get("listen") or {}
        check("the Edit dialog's ▶ Listen plays from the field's first time; Pause, −10 s and +10 s work there",
              shown == f"▶ Listen from {clock(first['start'])}" and first["start"] - 0.3 <= listened.get("time", -9) <= first["start"] + 3
              and not listened.get("paused", True) and result["pause"]["paused"] and result["toggle"] == "Play"
              and abs(result["forward"] - 10) < 0.5 and abs(result["back"] + 10) < 0.5
              and result["label"] == f"Testimony {unit} · Main argument · from {clock(first['start'])}",
              json.dumps(result, default=str), shown)

        # Repair dialogs: Listen from the item being repaired, and none for a new testimony.
        operation = self.repair("rich_add_argument", rf"/units/{unit - 1}/arguments")
        bar = page.locator("#structure-listen")
        first = first_timed(draft, evidence_under(at(draft, operation["path"].rsplit("/", 1)[0])))
        shown = bar.is_visible() and bar.locator('[data-audio="listen"]').inner_text()
        state = {}
        if shown:
            self.pause_at(0)
            bar.locator('[data-audio="listen"]').click()
            state = self.playing_from(first["start"])
        page.locator("#cancel-structure").click()
        expect(page.locator("#structure-dialog")).to_be_hidden()
        # Adding a testimony has no time of its own: Play and ±10 s stay, but no Listen.
        whole = self.repair("rich_add_unit", r"/units") and page.locator('#structure-listen [data-audio="listen"]').is_visible()
        page.locator("#cancel-structure").click()
        check("a repair dialog's ▶ Listen plays from its testimony; adding a testimony has none",
              shown == f"▶ Listen from {clock(first['start'])}" and first["start"] - 0.3 <= state.get("time", -9) <= first["start"] + 3
              and whole is False, f"shown {shown!r}, {state}, Add testimony listen {whole}", shown)

        # A questioner inferred from context: ▶ Listen first, before Approve, playing the question.
        inferred = [path for path, item in fields(draft) if item.get("flag") == INFERRED_ASKER and item["review"] != "APPROVED"]
        if inferred:
            orders = {path: page.locator(f'#review-view [data-field="{path}"] .field-actions button').evaluate_all(
                "nodes => nodes.map(n => n.dataset.listen ? 'listen' : n.dataset.approve ? 'approve' : n.dataset.edit ? 'edit' : 'other')")
                for path in inferred}
            path = inferred[0]
            question = at(draft, re.sub(r"/asker(/name)?$", "/question", path))
            first = first_timed(draft, question["evidence"]) or first_timed(draft, at(draft, path)["evidence"])
            self.pause_at(0)
            page.locator(f'#review-view [data-field="{path}"] [data-listen]').click()
            lead = max(0.0, first["start"] - 15)  # The cited line is often the answer, so it starts earlier.
            state = self.playing_from(lead)
            check("an inferred questioner's ▶ Listen comes first, before Approve, and plays from just before the question",
                  all(order[:1] == ["listen"] and "approve" in order for order in orders.values())
                  and page.locator(f'#review-view [data-field="{path}"] [data-listen]').inner_text() == "▶ Listen from just before"
                  and lead - 0.3 <= state["time"] <= lead + 3 and not state["paused"],
                  f"{orders}, {state}", f"{len(inferred)} inferred questioner(s)")
        else:
            self.check.report("SKIP", "inferred questioner ▶ Listen", "this draft has no questioner inferred from context")
        page.evaluate("document.querySelector('#source-audio').pause()")

    def queue_extras(self, job_id):
        """Rebuild and the progress timer, in a second tab whose queue replies are stubbed."""
        check, expect = self.check, self.expect
        real = self.job(job_id)
        jobs, posts, dialogs, answer = [], [], [], {"accept": True}

        def stub(job_id_, title, processing, **extra):
            job = {key: value for key, value in copy.deepcopy(real).items()
                   if key not in ("can_rebuild", "stage_started_at", "duration_seconds")}
            job.update(id=job_id_, processing=processing, revision=1, error=None, failed_stage=None, **extra)
            job["hearing"] = {**job["hearing"], "title": title}
            jobs.append(job)
            return job
        stub("stub-rebuild", "Stub: untouched Gemini draft", "Complete", can_rebuild=True)
        stub("stub-no-rebuild", "Stub: reviewed draft", "Complete", can_rebuild=False)
        long = stub("stub-transcribing", "Stub: transcribing a 2-hour recording", "Transcribing", kind="audio",
                    audio=True, stage_started_at=iso_ago(minutes=14, seconds=5), duration_seconds=7200)
        stub("stub-unknown-length", "Stub: transcribing, length unknown", "Transcribing",
             stage_started_at=iso_ago(minutes=3, seconds=5).replace("+00:00", "Z"), duration_seconds=None)
        stub("stub-analyzing", "Stub: analyzing for over an hour", "Analyzing", stage_started_at=iso_ago(minutes=75, seconds=5))
        stub("stub-analyzing-long", "Stub: analyzing a 4-hour hearing in parts", "Analyzing",
             stage_started_at=iso_ago(minutes=4, seconds=5), duration_seconds=4 * 3600)
        stub("stub-waiting", "Stub: waiting", "Waiting", stage_started_at=iso_ago(seconds=20))
        stub("stub-no-start", "Stub: transcribing, no start time", "Transcribing")
        hearing_page = {**long, "audio": {"filename": "stub.mp3"}, "review": {"status": "PENDING", "fields": 0,
                                                                             "approved": 0, "unresolved": 0}}

        def rebuild(route):
            posts.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=409, json={"error": REBUILD_STUB_ERROR})

        def dialog(item):
            dialogs.append(item.message)
            item.accept() if answer["accept"] else item.dismiss()
        page = self.context.new_page()
        page.on("dialog", dialog)
        page.route(re.compile(r"/api/hearings$"), lambda route: route.fulfill(json=copy.deepcopy(jobs)))
        page.route(re.compile(r"/api/hearings/stub-transcribing$"), lambda route: route.fulfill(json=hearing_page))
        page.route(re.compile(r"/api/rebuild$"), rebuild)
        texts = {}
        try:
            page.goto(self.base + "/")
            expect(page.locator('[data-progress="stub-waiting"]')).not_to_be_empty()
            buttons = page.locator("#hearings-view [data-rebuild]")
            shown = buttons.evaluate_all("nodes => nodes.map(n => [n.dataset.rebuild, n.textContent])")
            answer["accept"] = False
            page.locator('[data-rebuild="stub-rebuild"]').click()
            page.wait_for_timeout(500)
            cancelled = list(posts)
            answer["accept"] = True
            page.locator('[data-rebuild="stub-rebuild"]').click()
            expect(page.locator("#global-error")).to_be_visible()
            error = page.locator("#global-error").inner_text()
            check("Rebuild with the latest fixes shows only when can_rebuild, asks first and shows the server's error as is",
                  shown == [["stub-rebuild", "Rebuild with the latest fixes"]] and dialogs == [REBUILD_QUESTION] * 2
                  and cancelled == [] and posts == [{"id": "stub-rebuild"}] and error == REBUILD_STUB_ERROR,
                  f"buttons {shown}, dialogs {dialogs}, posts {cancelled} then {posts}, error {error!r}")

            for job in jobs:
                node = page.locator(f'[data-progress="{job["id"]}"]')
                texts[job["id"]] = " ".join(node.text_content().split())
            want = {"stub-rebuild": "", "stub-no-rebuild": "", "stub-no-start": "",
                    "stub-transcribing": "Transcribing · 14 min so far · usually 10–30 min for this 2-hour recording",
                    "stub-unknown-length": "Transcribing · 3 min so far · usually 10–30 min for a 2-hour hearing",
                    "stub-analyzing": f"Analyzing · 1 h 15 min so far · usually 3–5 min{LATE}",
                    "stub-analyzing-long": "Analyzing · 4 min so far · usually 6–12 min for a hearing this long",
                    "stub-waiting": "Waiting · under a minute so far · waiting for the hearing ahead"}
            # A minute later the next poll updates the same element in place (no re-render, no new timer).
            page.evaluate("""() => { document.querySelector('[data-progress="stub-transcribing"]').__same = true;
                const now = Date.now; Date.now = () => now() + 61000; }""")
            later = self.within(lambda: page.evaluate("""() => { const node = document.querySelector('[data-progress="stub-transcribing"]');
                return node.__same && /15 min so far/.test(node.textContent) ? node.textContent : null; }"""), 5)
            page.locator('[data-open="stub-transcribing"]').click()
            expect(page.locator("#review-title")).to_be_visible()
            on_page = " ".join(page.locator('#review-view [data-progress="stub-transcribing"]').text_content().split())
            check("the queue and the hearing page show time in stage and a typical time, kept current by the polling",
                  texts == want and later and on_page == want["stub-transcribing"].replace("14 min", "15 min"),
                  f"got {texts}, a minute later {later!r}, hearing page {on_page!r}", texts["stub-transcribing"])
        finally:
            page.close()

        # The real server's fields, when it sends them.
        self.real_rebuild = "can_rebuild" in real
        if not self.real_rebuild:
            self.check.report("SKIP", "the server's can_rebuild and POST /api/rebuild", "this server doesn't send can_rebuild yet")
            return
        check("a draft made by this build doesn't offer Rebuild", real["can_rebuild"] is False,
              f"can_rebuild {real['can_rebuild']!r}")
        with self.server.store.lock:  # As if an older build drafted it, like her Part 2.
            self.server.store.data["jobs"][job_id]["draft"]["extraction_provenance"].pop("drafting", None)
            self.server.store._save()
        real = next(job for job in self.get("/api/hearings") if job["id"] == job_id)
        page = self.page
        page.locator('nav [data-view="hearings"]').click()
        button = page.locator(f'#hearings-view [data-rebuild="{job_id}"]')
        if not real["can_rebuild"]:
            check("an untouched draft from an older build offers Rebuild (server can_rebuild)", False, f"can_rebuild {real['can_rebuild']!r}")
        else:
            prompts, checks, dialogs_before = len(self.fake.prompts), len(self.fake.checks), len(self.dialogs)
            button.click()
            self.wait_for(lambda: not page.locator("#global-error").is_hidden() or not page.locator("#toast").is_hidden(),
                          60, "the rebuild reply")
            error = None if page.locator("#global-error").is_hidden() else page.locator("#global-error").inner_text()
            done = self.job_when(job_id, lambda job: job["processing"] in ("Complete", "Failed"), 120, "the rebuilt draft")
            rebuilt = self.get(f"/api/hearings/{job_id}")
            check("Rebuild asks first, makes no new AI call (drafting or second check) and leaves a complete draft",
                  self.dialogs[dialogs_before:] == [REBUILD_QUESTION] and not error and done["processing"] == "Complete"
                  and rebuilt.get("draft") and len(self.fake.prompts) == prompts and len(self.fake.checks) == checks
                  and (rebuilt["draft"].get("second_check") or {}).get("flags"),
                  f"dialogs {self.dialogs[dialogs_before:]}, error {error!r}, {done['processing']}, "
                  f"AI calls {prompts} -> {len(self.fake.prompts)}, checks {checks} -> {len(self.fake.checks)}")
        expect(page.locator(f'#hearings-view [data-open="{job_id}"]')).to_have_text("Open draft", timeout=15000)
        page.locator(f'#hearings-view [data-open="{job_id}"]').click()
        expect(page.locator("#review-view .review-tools")).to_be_visible()

    def copy_note(self):
        """Click Copy Hearing Notes; returns (outcome, spy entry)."""
        page = self.page
        before = page.evaluate("document.querySelector('#toast').hidden = true; window.__copies.length")
        page.locator('#output-view [data-action="copy"]').click()
        page.wait_for_function("""() => !document.querySelector("#global-error").hidden
            || !document.querySelector("#toast").hidden && /copied/i.test(document.querySelector("#toast").textContent)""",
                               timeout=20000)
        entry = page.evaluate(f"window.__copies[{before}] ?? null")
        error = page.locator("#global-error")
        if error.is_visible():
            return ("fallback" if error.inner_text().strip() == COPY_FALLBACK else f"error: {error.inner_text()}"), entry
        return "copied", entry

    def run(self):
        page, check, expect = self.page, self.check, self.expect
        hearing = self.inputs["hearing"]
        page.goto(self.base + "/")
        expect(page.locator("#queue-title")).to_have_text("Batch queue")
        expect(page.locator("#save-status")).to_have_text("Progress saved locally")
        check("version label shows", self.visible("#app-version") and re.search(
            r"v\d+\.\d+\.\d+", page.locator("#app-version").inner_text()), "no version label",
            page.locator("#app-version").inner_text())
        check("page asks /api/update (keeps self-update working)",
              self.within(lambda: ("GET", "/api/update") in self.requests, 10))
        self.update_banner()
        page.locator('.local-badge [data-action="extraction"]').click()
        sentence = " ".join(page.locator("#extraction-dialog form > p").first.inner_text().split())
        check("AI drafting dialog points to ⚠, Next ⚠ and Other things to check", IN_APP_SENTENCE in sentence, sentence)
        page.locator("#extraction-provider").select_option("gemini")
        page.locator("#gemini-key").fill(FAKE_KEY)
        page.locator('#extraction-form button[type="submit"]').click()
        expect(page.locator("#extraction-dialog")).to_be_hidden()
        check("Gemini drafting turns on with the pasted key", self.server.store.extractor.keys.get() == FAKE_KEY
              and "drafted by Gemini" in page.locator("#extraction-note").inner_text())

        # The recording fails to transcribe, the way it would if her MacWhisper refuses the command line.
        page.locator("#audio-files").set_input_files(self.inputs["recording"])
        expect(page.locator("#audio-import-dialog")).to_be_visible()
        page.locator("#audio-committee").fill(hearing["committee"])
        page.locator("#audio-title-0").fill(hearing["title"])
        page.locator("#audio-date-0").fill(hearing["date"])
        page.locator("#audio-link-0").fill(hearing["url"])
        page.locator("#audio-public").check()
        page.locator("#save-audio-import").click()
        expect(page.locator("#audio-import-dialog")).to_be_hidden(timeout=180000)
        job_id = self.wait_for(lambda: next((j["id"] for j in self.get("/api/hearings")), None), 30, "the hearing")
        failed = self.job_when(job_id, lambda job: job["processing"] == "Failed", 60, "the transcription failure")
        check("failed transcription is Failed at the transcription stage", failed["failed_stage"] == "transcription",
              json.dumps({k: failed[k] for k in ("processing", "failed_stage", "error")}))
        check("failed transcription offers Attach transcript in the queue",
              self.visible(f'#hearings-view [data-attach-transcript="{job_id}"]'))
        page.locator(f'#hearings-view [data-open="{job_id}"]').click()
        expect(page.locator("#review-title")).to_be_visible()
        heading = page.locator("#review-view .empty-state h2").inner_text()
        attach_here = self.visible(f'#review-view [data-attach-transcript="{job_id}"]', 5000)
        check("failed transcription's hearing page says so and offers Attach",
              heading == "Transcription failed" and attach_here, f"heading {heading!r}, button {attach_here}")
        transcript = self.inputs["transcript"]
        if attach_here:
            with page.expect_file_chooser() as chooser:
                page.locator(f'#review-view [data-attach-transcript="{job_id}"]').click()
            chooser.value.set_files(transcript)
        else:
            print("INFO attaching the transcript through the API so the other checks can run", flush=True)
            name, data = ((transcript["name"], transcript["buffer"]) if isinstance(transcript, dict)
                          else (Path(transcript).name, Path(transcript).read_bytes()))
            self.post("/api/text", {"attach_to": job_id, "text": data.decode("utf-8"), "filename": name})
        # The old transcription failure stays until the attached transcript is taken.
        done = self.job_when(job_id, lambda job: job["processing"] == "Complete" or job["processing"] == "Failed"
                             and job["failed_stage"] != "transcription", 240, "the draft")
        draft = self.get(f"/api/hearings/{job_id}").get("draft") or {"units": []}
        provenance = draft.get("extraction_provenance") or {}
        summary = draft.get("second_check") or {}
        check("the second check asks once per testimony and keeps only quoted findings",
              summary.get("units") == len(draft["units"]) == len(self.fake.checks) and summary.get("flags") == 1
              and [gap["point"] for gap in summary.get("missed", [])] == [MISSED_POINT] and not summary.get("failed"),
              f"{len(draft['units'])} testimonies, {len(self.fake.checks)} check calls, summary {summary}")
        if not check("draft is built through gemini_draft with the agenda (fake Gemini, no network)",
                     done["processing"] == "Complete" and provenance.get("provider") == "gemini"
                     and provenance.get("agenda_bills") and len(self.fake.prompts) == 1,
                     f"{done['processing']}: {done.get('error')} {provenance}",
                     f"{len(draft['units'])} testimonies, {provenance.get('agenda_bills')} agenda bills"):
            raise AssertionError("No draft to review; later checks need it.")
        page.locator('nav [data-view="hearings"]').click()
        expect(page.locator(f'#hearings-view [data-open="{job_id}"]')).to_have_text("Open draft", timeout=15000)
        page.locator(f'#hearings-view [data-open="{job_id}"]').click()
        expect(page.locator("#draft-heading")).to_be_visible()
        expect(page.locator("#review-view .review-tools")).to_be_visible()
        self.listening(job_id)
        self.queue_extras(job_id)
        self.review(job_id)
        self.output(job_id)
        self.blanks(job_id)
        if self.real_rebuild:
            page.locator('nav [data-view="hearings"]').click()
            summary = self.job(job_id)
            check("Rebuild is gone once the draft has review work", summary.get("can_rebuild") is False
                  and not page.locator(f'#hearings-view [data-rebuild="{job_id}"]').count(),
                  f"can_rebuild {summary.get('can_rebuild')!r}")
        check("the Gemini key stays out of workspace files (state, saved replies, transcripts)", not any(
            FAKE_KEY.encode() in path.read_bytes() for path in self.server.store.directory.rglob("*")
            if path.is_file() and path.name != "workspace.lock"))  # The held lock can't be read on Windows.
        self.stop()

    def update_banner(self):
        """A second tab is told an update exists; its install request is held while the banner is read."""
        page, held = self.context.new_page(), []
        page.route(re.compile(r"/api/update$"), lambda route: route.fulfill(json={
            "current": "v0.1.0 · build 20", "available": True, "can_install": True, "auto": False,
            "latest": "v0.1.0 · build 99 · 0000000", "reason": None}))
        page.route(re.compile(r"/api/update/install$"), lambda route: held.append(route))
        try:
            page.goto(self.base + "/")
            page.locator('#update-note [data-action="install-update"]').click()
            self.within(lambda: held, 10)
            banner = page.locator("#update-note").inner_text()
            if held:
                held[0].fulfill(status=400, json={"error": "Nothing was installed; this was a check."})
        finally:
            page.close()
        self.check("update banner says to keep the tab open while it downloads",
                   banner == "Downloading and checking the update… Keep this tab open; the app restarts by itself.",
                   repr(banner))

    def review(self, job_id):
        page, check = self.page, self.check
        draft = self.get(f"/api/hearings/{job_id}")["draft"]
        open_flags = [path for path, item in fields(draft) if item.get("flag") and item["review"] != "APPROVED"]
        issues = [path for path, _ in fields(draft) if path.startswith("/issues/")]
        if issues:
            badge = page.locator(f'#hearings-view tr:has([data-open="{job_id}"]) .queue-flags').text_content()
            unflagged = [path for path in issues if path not in open_flags]
            check("flagged hearing-wide checks count in 'N to check' and the queue badge",
                  not unflagged and self.to_check() == len(open_flags) == self.job(job_id)["review"]["flags"]
                  and badge.strip() == f"⚠ {len(open_flags)} to check",
                  f"page {self.to_check()}, queue {badge!r}, expected {len(open_flags)} with {len(issues)} hearing-wide"
                  + (f"; not flagged: {unflagged}" if unflagged else ""))
            flags = {path: item.get("flag") for path, item in fields(draft) if path in issues}
            notes = {path: page.locator(f'#review-view [data-field="{path}"] .field-note').inner_text() for path in issues}
            check("flagged hearing-wide checks get ⚠ styling and show their instruction", all(
                flags[path] and "flagged" in page.locator(f'#review-view [data-field="{path}"]').get_attribute("class").split()
                and notes[path].startswith(f"⚠ {flags[path]}") for path in issues), notes)
        else:
            self.check.report("SKIP", "flagged hearing-wide checks count", "this draft has no hearing-wide checks")

        # The second check's flag and its possibly missed point, as she sees them.
        second = [path for path, item in fields(draft) if (item.get("flag") or "").startswith("Second check:")]
        notes = [page.locator(f'#review-view [data-field="{path}"] .field-note').inner_text() for path in second]
        gap = (draft.get("second_check") or {}).get("missed") or [{}]
        missed = page.locator("#unit-0 .possibly-missed")
        listed = missed.inner_text() if missed.count() else ""
        times = missed.locator("[data-evidence]").evaluate_all("nodes => nodes.map(n => n.dataset.evidence)") if listed else []
        check("a second-check flag says what the transcript says, and possibly missed points sit under their testimony",
              len(second) == 1 and notes[0].startswith(f"⚠ Second check: {SECOND_REASON} Transcript: “")
              and listed.startswith("Possibly missed (1)") and MISSED_POINT in listed and times == gap[0].get("evidence"),
              f"flags {second}, notes {notes}, list {listed!r}, times {times} vs {gap[0].get('evidence')}")

        # The audio check: its one "different" clip is flagged with what was heard, and nothing is changed.
        heard = [path for path, item in fields(draft) if (item.get("flag") or "").startswith("Audio check:")]
        summary = draft.get("audio_check") or {}
        notes = [page.locator(f'#review-view [data-field="{path}"] .field-note').inner_text() for path in heard]
        check("the audio check listens to unflagged names and figures and flags what sounds different",
              summary.get("items", 0) >= 1 and len(self.fake.audio) >= 1 and summary.get("flags") == 1
              and len(heard) == 1 and notes[0].startswith(f"⚠ Audio check: Gemini hears “{AUDIO_HEARD}”"),
              f"summary {summary}, {len(self.fake.audio)} audio calls, flagged {heard}: {notes}")

        # The legislator name check: the committee's members are listed, and a near-miss name is flagged.
        if (draft.get("roster") or {}).get("committee"):
            panel = page.locator("#review-view .committee-members")
            listed = panel.locator("summary").inner_text() if panel.count() else ""
            asker = next((f"/units/{i}/questions/{j}/asker/name" for i, unit in enumerate(draft["units"])
                          for j, q in enumerate(unit["questions"]) if q["asker"]["name"]["value"] == "Senator Lee"), None)
            note = page.locator(f'#review-view [data-field="{asker}"] .field-note').inner_text() if asker else ""
            check("the committee's members are listed and a legislator's near-miss name is flagged, never changed",
                  listed == "Committee members (3)" and note.startswith(ROSTER_FLAG)
                  and draft["roster"]["flagged"] == 1,
                  f"panel {listed!r}, asker {asker}: {note!r}, roster {draft.get('roster')}")
        else:
            self.check.report("SKIP", "the legislator name check", "this hearing's draft has no committee")

        # Next ⚠ from the top: three presses, three different flags, each on screen.
        page.evaluate("window.scrollTo(0, 0)")
        self.settle()
        shown = [self.next_flag() for _ in range(3)]
        paths = [item and item["path"] for item in shown]
        check("Next ⚠ visits 3 different flags in 3 presses",
              len(set(paths)) == 3 and all(item and item["flagged"] and item["visible"] for item in shown), paths)
        third = paths[2]
        revision = self.revision()
        page.locator(f'#review-view [data-approve="{third}"]').click()
        self.saved_after(revision)
        after = self.next_flag()
        later = after and page.evaluate("""([a, b]) => {
            const nodes = [...document.querySelectorAll("#review-view [data-field]")];
            const first = nodes.find(n => n.dataset.field === a), second = nodes.find(n => n.dataset.field === b);
            return Boolean(first && second && first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
        }""", [third, after["path"]])
        check("Next ⚠ moves on after an approval", later and after["flagged"] and after["path"] not in paths,
              f"approved {third}, then shown {after}")

        # A full tour from there goes through the rest in page order, including hearing-wide checks, then wraps.
        order = page.eval_on_selector_all("#review-view .field.flagged", "nodes => nodes.map(n => n.dataset.field)")
        start = order.index(after["path"]) if after and after["path"] in order else 0
        expected = order[start + 1:] + order[:start + 1]
        tour = [(self.next_flag() or {}).get("path") for _ in expected]
        check("Next ⚠ continues in page order and wraps to the first flag", tour == expected,
              f"expected {expected}, got {tour}")
        if issues:
            check("Next ⚠ reaches the hearing-wide checks", any(path in tour for path in issues), tour)
            count = self.to_check()
            revision = self.revision()
            page.locator(f'#review-view [data-approve="{issues[0]}"]').click()
            self.saved_after(revision)
            approved = self.get(f"/api/hearings/{job_id}")["draft"]
            index = int(issues[0].rsplit("/", 1)[1])
            check("a hearing-wide check is approved with its normal Approve button",
                  approved["issues"][index]["review"] == "APPROVED" and self.to_check() == count - 1,
                  f"review {approved['issues'][index]['review']}, count {count} -> {self.to_check()}")

        # Approve everything not flagged leaves every open flag, hearing-wide ones included.
        job = self.get(f"/api/hearings/{job_id}")
        before = [path for path, item in fields(job["draft"]) if item.get("flag") and item["review"] != "APPROVED"]
        revision = self.revision()
        dialogs = len(self.dialogs)
        page.locator('#review-view [data-action="approve-unflagged"]').click()
        self.saved_after(revision)
        job_after = self.get(f"/api/hearings/{job_id}")
        after_bulk = [path for path, item in fields(job_after["draft"]) if item.get("flag") and item["review"] != "APPROVED"]
        check("Approve everything not flagged leaves the flagged items",
              before == after_bulk and job_after["review"]["approved"] > job["review"]["approved"]
              and "Flagged (⚠) items stay" in " ".join(self.dialogs[dialogs:]),
              f"open flags {len(before)} -> {len(after_bulk)}, approved {job['review']['approved']} -> "
              f"{job_after['review']['approved']}")

        # Repair testimony association opens on where the Q&A is, and an untouched Save changes nothing.
        operation = self.repair("rich_move_question", r"/units/\d+/questions/\d+")
        if operation:
            unit = int(operation["path"].split("/")[2])
            select = page.locator("#rich-destination")
            options = select.locator("option")
            labels = options.all_inner_texts()
            chosen = select.locator("option:checked").inner_text()
            check("Repair testimony association preselects the current testimony",
                  select.input_value() == f"/units/{unit}/questions" and chosen.startswith(f"Testimony {unit + 1} · ")
                  and options.last.get_attribute("value") == "/unassigned_questions"
                  and all(re.fullmatch(r"Testimony \d+ · .+ · (\d\d:\d\d:\d\d|Text line \d+)( \(current\))?", label)
                          for label in labels[:-1]),
                  f"selected {select.input_value()!r} {chosen!r}, last {options.last.inner_text()!r}, e.g. {labels[:2]}")
            posts, revision = len(self.requests), self.revision()
            page.locator("#save-structure").click()
            self.expect(page.locator("#structure-dialog")).to_be_hidden()
            page.wait_for_timeout(500)
            check("an unchanged testimony association closes without saving",
                  ("POST", "/api/review") not in self.requests[posts:] and self.revision() == revision)
        else:
            self.check.report("SKIP", "Repair testimony association", "no Q&A inside a testimony")

        # Correction boxes turn off autocorrect but keep the spelling underline.
        edit, attributes = page.locator("#edit-value"), []
        if self.repair("rich_add_argument", r"/units/\d+/arguments"):
            attributes = [(page.locator(selector).get_attribute("autocorrect"), page.locator(selector).get_attribute("autocapitalize"))
                          for selector in ("#rich-name", "#rich-organization", "#rich-value")]
            page.locator("#cancel-structure").click()
            self.expect(page.locator("#structure-dialog")).to_be_hidden()
        check("correction boxes have autocorrect and autocapitalize off",
              (edit.get_attribute("autocorrect"), edit.get_attribute("autocapitalize")) == ("off", "off")
              and edit.get_attribute("spellcheck") != "false"
              and attributes and all(pair == ("off", "off") for pair in attributes),
              f"edit {(edit.get_attribute('autocorrect'), edit.get_attribute('autocapitalize'))}, repair {attributes}")

        # A comment with no reply can be cleared with one click.
        button = page.locator("#review-view [data-no-response]").first
        if button.count():
            path = button.get_attribute("data-no-response")
            marks = self.note(job_id).count(NO_RESPONSE_MARK)
            revision = self.revision()
            button.click()
            self.saved_after(revision)
            target = self.get(f"/api/hearings/{job_id}")["draft"]
            for part in path.strip("/").split("/"):
                target = target[int(part)] if part.isdigit() else target[part]
            resolution = target.get("response_resolution") or {}
            check("No response (comment only) saves an approved resolution",
                  (resolution.get("value"), resolution.get("review")) == ("No response; comment only.", "APPROVED")
                  and not page.locator(f'#review-view [data-no-response="{path}"]').count(), resolution)
            self.no_response = marks
        else:
            check("an exchange with no reply offers No response (comment only)", False, "no such button")

    def output(self, job_id):
        page, check = self.page, self.check
        page.locator('[data-view="output"]').click()
        self.expect(page.locator("#output-title")).to_be_visible()
        attempts = []
        if self.engine == "chromium":
            attempts.append(("without clipboard permission", *self.copy_note()))
            self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.base)
            page.wait_for_timeout(300)
        attempts.append(("", *self.copy_note()))
        note = self.note(job_id)
        good, texts = True, []
        for label, outcome, entry in attempts:
            copied = (entry or {}).get("text")
            if outcome == "copied" and copied is not None:
                texts.append(copied.replace("\r\n", "\n"))
                good &= texts[-1] == note
            elif outcome not in ("copied", "fallback"):
                good = False
        check("Copy Hearing Notes copies the saved note or shows the fallback text", good,
              "; ".join(f"{label or 'copy'}: {outcome}" for label, outcome, _ in attempts),
              "; ".join(f"{label or 'copy'}: {outcome}" for label, outcome, _ in attempts))
        entries = [entry for _, _, entry in attempts if entry]
        check("Copy starts the clipboard write while handling the click (Safari's rule)",
              entries and all(entry.get("sync") for entry in entries), entries and [
                  {k: entry.get(k) for k in ("via", "sync", "ok", "error")} for entry in entries])
        if self.no_response is not None:
            marks = self.no_response
            copied = texts[-1] if texts else note
            left = copied.count(NO_RESPONSE_MARK)
            check("No response (comment only) clears its ⚠ in the copied note", left == marks - 1, f"{marks} -> {left}")

    def blanks(self, job_id):
        """Once every flag is approved, Next ⚠ goes on to the blanks the copied note still marks ⚠."""
        page, check = self.page, self.check
        job = self.get(f"/api/hearings/{job_id}")
        changes = [{"path": path, "review": "APPROVED"} for path, item in fields(job["draft"])
                   if item.get("flag") and item["review"] != "APPROVED"]
        if changes:  # Flag handling was checked above; approve the rest at once.
            job = self.post("/api/review", {"id": job_id, "revision": job["revision"], "changes": changes})
        draft = job["draft"]
        # What the copied note still marks ⚠ once the flags are approved.
        expected = sorted([path for path, item in fields(draft) if not item["value"] and not item.get("not_applicable")
                           and MARKED_BLANK.search(path)]
                          + [f"/units/{i}/questions_none" for i, unit in enumerate(draft["units"])
                             if not unit["questions"] and not (unit.get("questions_none") or {}).get("value")])
        page.locator('nav [data-view="review"]').click()
        page.wait_for_function("(r) => !state.busy && state.job && state.job.revision >= r && "
                               "!document.querySelector('#review-view .field.flagged')", arg=job["revision"],
                               timeout=20000)
        page.evaluate("window.scrollTo(0, 0)")
        self.settle()
        stops, done = [], ""
        for _ in range(len(expected) + 2):
            page.evaluate("() => { const toast = document.querySelector('#toast'); toast.hidden = true; toast.textContent = ''; }")
            page.locator('#review-view [data-action="next-flag"]').click()
            self.settle()
            hint = page.locator("#toast").text_content()
            if hint.startswith("Nothing is flagged"):
                done = hint
                break
            shown = page.evaluate(BLANK_SHOWN) or {"path": None}
            stops.append({**shown, "hint": hint})
            if not shown["path"] or [stop["path"] for stop in stops].count(shown["path"]) > 1:
                break  # Lost, or back at a blank it already showed.
            revision = self.revision()
            if shown["fix"]:  # Not applicable, No response (comment only) or No questions.
                page.locator(f'#review-view [{shown["fix"]}="{shown["value"]}"]').click()
            else:  # A blank with no one-click fix is filled in.
                page.locator(f'#review-view [data-edit="{shown["path"]}"]').click()
                if shown["path"].endswith("/position"):
                    page.locator("#position-value").select_option("Unclear")
                else:
                    page.locator("#edit-value").fill("Checked by listening")
                page.locator("#edit-review").select_option("APPROVED")
                page.locator("#save-edit").click()
                self.expect(page.locator("#edit-dialog")).to_be_hidden()
            self.saved_after(revision)
        summary = [{k: stop.get(k) for k in ("path", "blank", "visible", "fix", "fix_visible")} for stop in stops]
        if expected or stops:
            check("after the last flag, Next ⚠ stops at each blank the copied note marks ⚠, with its fix on screen",
                  sorted(stop["path"] or "" for stop in stops) == expected and all(
                      stop.get("blank") and stop.get("visible") and stop["hint"].startswith("No ⚠ flags left.")
                      and (not stop.get("fix") or stop.get("fix_visible")) for stop in stops),
                  f"expected {expected}, got {summary}", f"{len(stops)} blanks: {[stop['path'] for stop in stops]}")
        else:
            self.check.report("SKIP", "Next ⚠ stops at blanks after the last flag", "this draft has no such blanks")
        note = self.note(job_id)
        check("then Next ⚠ says nothing is flagged, and the copied note has no ⚠", done and "⚠" not in note,
              f"{done!r}; ⚠ lines: {[line for line in note.splitlines() if '⚠' in line][:6]}")
        page.locator('[data-view="output"]').click()
        self.expect(page.locator("#output-title")).to_be_visible()
        outcome, _ = self.copy_note()
        said = page.locator("#toast").inner_text() if outcome == "copied" else ""
        check("Copy doesn't say items marked ⚠ need checking when none are",
              outcome == "copied" and said in ("Reviewed hearing notes copied.",
                                               "Hearing notes copied. Nothing in them is marked ⚠."),
              f"{outcome}: {said!r}", said)

    def stop(self):
        page, check = self.page, self.check
        # A second hearing drafts slowly, so one is 'Analyzing' while Stop app is clicked.
        transcript = srt(CUES)
        self.post("/api/text", {"text": transcript, "filename": "stop-check.srt", "source_scope": "public_hearing",
                                "hearing": {"title": SLOW_TITLE, "date": "2026-10-16", "committee": "Example committee",
                                            "source_file": "stop-check.srt"}})
        self.wait_for(self.fake.slow_started.is_set, 60, "the slow draft to start")
        page.wait_for_function("() => state.hearings.some(j => j.processing === 'Analyzing')", timeout=15000)
        slow = next(job for job in self.get("/api/hearings") if job["processing"] == "Analyzing")
        if isinstance(slow.get("stage_started_at"), str):
            page.locator('nav [data-view="hearings"]').click()
            shown = self.within(lambda: page.locator(f'[data-progress="{slow["id"]}"]').text_content(), 5)
            check("the server's stage_started_at shows in the queue while a hearing is Analyzing",
                  " ".join((shown or "").split()) == "Analyzing · under a minute so far · usually 3–5 min", repr(shown))
        else:
            self.check.report("SKIP", "the server's stage_started_at in the queue", "this server doesn't send it yet")
        self.accept_dialogs, dialogs, requests = False, len(self.dialogs), len(self.requests)
        page.locator('.app-footer [data-action="stop"]').click()
        self.within(lambda: len(self.dialogs) > dialogs, 10)
        page.wait_for_timeout(1000)
        check("Stop app asks first while a hearing is Analyzing, and Cancel keeps it running",
              self.dialogs[dialogs:] == [STOP_QUESTION] and ("POST", "/api/shutdown") not in self.requests[requests:]
              and "Stopping" not in page.locator("#save-status").inner_text(),
              f"dialogs {self.dialogs[dialogs:]}, status {page.locator('#save-status').inner_text()!r}")
        self.fake.release.set()
        self.wait_for(lambda: all(j["processing"] in ("Complete", "Failed") for j in self.get("/api/hearings")), 60,
                      "the slow draft to finish")
        page.wait_for_function("() => !state.hearings.some(j => ['Transcribing', 'Analyzing', 'Waiting'].includes(j.processing))",
                               timeout=15000)
        check("no page or console errors", not self.errors, self.errors[:5])
        self.accept_dialogs, dialogs, requests = True, len(self.dialogs), len(self.requests)
        page.locator('.app-footer [data-action="stop"]').click()
        self.stopped = self.within(lambda: ("POST", "/api/shutdown") in self.requests[requests:], 10)
        page.wait_for_timeout(500)
        status = page.locator("#save-status").inner_text()
        check("Stop app doesn't ask when nothing is processing, and says how long to wait",
              self.dialogs[dialogs:] == [] and status == STOPPING, f"dialogs {self.dialogs[dialogs:]}, status {status!r}")


def run(args, results) -> None:
    if (ROOT / ".scratch/playwright-browsers").is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / ".scratch/playwright-browsers"))
    from playwright.sync_api import expect, sync_playwright
    from hearing_compiler.server import make_server
    inputs = real_inputs(args.real_hearing) if args.real_hearing else synthetic_inputs()
    print(f"INFO {args.engine}, {inputs['label']}", flush=True)
    agenda.fetch = lambda url: {"url": agenda.check_url(url), "bills": copy.deepcopy(inputs["agenda"]),
                                "committee": inputs.get("committee"), "fetched_utc": "2026-10-16T00:00:00+00:00"}
    gemini.call_gemini = gemini.check_key = _blocked
    fake = FakeGemini(inputs["payload"])
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
        server = make_server(Path(temporary) / "state", args.port, transcription_provider=FailingTranscriber())
        server.store.extractor.keys, server.store.extractor.client = FakeKeys(), fake
        server.store.extractor.fetch_roster = lambda committee, path: copy.deepcopy(ROSTER)  # Never the real site.
        server.RequestHandlerClass.log_message = lambda *_: None  # One line per poll would bury the results.
        default_error = server.handle_error

        def handle_error(request, address):
            if isinstance(sys.exc_info()[1], ConnectionError):  # The browser dropped a stream (e.g. audio).
                return print("INFO the browser closed a connection mid-reply (server log noise on Windows)", flush=True)
            default_error(request, address)
        server.handle_error = handle_error
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        session = None
        try:
            with sync_playwright() as playwright:
                browser = getattr(playwright, args.engine).launch(headless=True)
                print(f"INFO browser {browser.version}, app {base}", flush=True)
                context = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
                context.add_init_script(CLIPBOARD_SPY)
                page = context.new_page()
                page.set_default_timeout(30000)
                try:
                    session = Session(page, context, base, server, fake, inputs, args.engine, results, expect)
                    session.run()
                except Exception:
                    shot = ROOT / ".scratch/friday-check" / f"failure-{args.engine}.png"
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        page.screenshot(path=str(shot), full_page=False)
                        print(f"INFO screenshot {shot}", flush=True)
                    except Exception:
                        pass
                    raise
                finally:
                    browser.close()
            if session and session.stopped:
                thread.join(30)
                results("the app stops after Stop app", not thread.is_alive())
        finally:
            fake.release.set()
            if thread.is_alive():
                server.shutdown()
                thread.join(30)
            try:
                server.server_close()
            except InputError as error:
                print(f"INFO {error}", flush=True)


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Browser check of the first-session path.")
    parser.add_argument("--engine", choices=("webkit", "chromium"), required=True)
    parser.add_argument("--real-hearing", type=Path, metavar="DIR", help="saved public hearing folder")
    parser.add_argument("--port", type=int, default=0, help="app port (default: any free port)")
    args = parser.parse_args(argv)
    results = Results()
    try:
        run(args, results)
    except Exception as error:
        results.report("FAIL", "the check finished", f"{type(error).__name__}: {error}")
        traceback.print_exc()
    failed = results.statuses.count("FAIL")
    print(f"{'FAIL' if failed else 'PASS'} {len(results.statuses)} checks, {failed} failed, "
          f"{results.statuses.count('SKIP')} skipped", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
