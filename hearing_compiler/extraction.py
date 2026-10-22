"""Draft methods: the limited rule baseline, or Gemini for public hearing text.

Gemini drafts become the same reviewable units as other AI drafts. Every field
starts as NEEDS_REVIEW, every model "check" becomes a review issue, and evidence
comes from verbatim quotes located by code. The API key lives in the macOS
Keychain, never in workspace files, exports or backups.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import uuid

from . import agenda, audio_check, gemini, parts, roster, second_check
from .pipeline import InputError, extract, field, fingerprint, validate
from .rich_review import FORMAT, NOT_ON_AGENDA, lead_flag
from .tls import context as tls_context

MODELS = (gemini.DEFAULT_MODEL,)
# Raise this whenever the code that turns a Gemini reply into a draft changes, so drafts made by older code
# offer "Rebuild with the latest fixes" and current ones don't.
DRAFTING = 29
ASKED_NAME = "This names who asked, and that name is inferred from context; confirm by listening."
ANSWERED = "Who answered is inferred from context; confirm by listening."
KEYCHAIN_SERVICE = "org.hearingcompiler.gemini"
KEYCHAIN_ACCOUNT = "api-key"
LEGISLATOR_TITLE = re.compile(r"(?i)^(rep\.?|representative|sen\.?|senator|chair|co-chair)\b")
BARE_TITLE = re.compile(r"(?i)(?:rep|representative|sen|senator|chair|co-chair)\.?")
CHAMBER_TITLE = re.compile(r"(?i)(?:rep|representative|sen|senator)\.?")
LEGISLATURE = re.compile(r"(?i)(?:the )?(?:massachusetts |ma )?(?:state )?"
                         r"(?:house of representatives|house|senate|general court|legislature)")
DISTRICT = re.compile(r"(?i)\bdistrict\b")
MULTI_BILL = "This testimony may cover more than one bill; check which points belong to which."
SMALL = dict(zip("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
                 "seventeen eighteen nineteen".split(), range(1, 20)))
TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
SCALES = {"hundred": 100, "thousand": 10 ** 3, "million": 10 ** 6, "billion": 10 ** 9, "dozen": 12}
VAGUE = {"tens": 10, "dozens": 12, "hundreds": 100, "thousands": 10 ** 3, "millions": 10 ** 6, "billions": 10 ** 9}
ROUGH = {"over", "about", "around", "nearly", "almost", "approximately", "roughly", "than", "upwards", "least",
         "under", "some", "several", "estimated", "close"}


class KeychainKeys:
    """macOS Keychain storage; other systems read GEMINI_API_KEY for development."""

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin"

    @staticmethod
    def _security(*args):
        try:
            return subprocess.run(["/usr/bin/security", *args], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as error:
            # Never log the error text: a timeout's text holds the whole command line, including the key.
            logging.warning("Keychain %s failed: %s", args[0], type(error).__name__)
            return None

    def get(self) -> str | None:
        if self.available():
            found = self._security("find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w")
            if found and found.returncode == 0 and found.stdout.strip():
                return found.stdout.strip()
        return os.environ.get("GEMINI_API_KEY", "").strip() or None

    def set(self, key: str):
        if not self.available():
            raise InputError("Saving a key needs the Mac app. On this computer, set GEMINI_API_KEY instead.")
        saved = self._security("add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT,
                               "-l", "Hearing Compiler Gemini key", "-w", key)
        if not saved or saved.returncode:
            raise InputError("The Mac Keychain did not save the key. If your Mac asked for a password, allow it, "
                             "then try again.")

    def delete(self):
        if self.available():
            self._security("delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT)


def _text(value, limit=4000):
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def figures(text, small=False):
    """(as written, value, rough) for each figure: digits, number words and magnitudes ("hundreds of
    thousands"). Bill numbers aren't figures. Spoken numbers under ten are skipped unless small is set."""
    text = gemini.BILL_MENTION.sub(lambda m: " " * len(m.group()) if len(m.group(2)) >= 3 else m.group(), text)
    tokens = list(re.finditer(r"\d+(?:,\d{3})*(?:\.\d+)?|[a-z]+", text, re.I))
    words = [t.group().lower() for t in tokens]
    found, i = [], 0
    while i < len(words):
        word, j, value, start = words[i], i + 1, None, tokens[i].start()
        rough = any(w in ROUGH for w in words[max(0, i - 2):i])
        if word[0].isdigit():
            if not (text[start - 1:start] == "-" and text[start - 2:start - 1].isalpha()):  # Not COVID-19 or K-12.
                value = float(word.replace(",", ""))
                value = value if math.isfinite(value) else None  # A run of hundreds of digits isn't a figure.
        elif word in SMALL or word in TENS:
            value = SMALL.get(word) or TENS[word]
            if word in TENS and j < len(words) and SMALL.get(words[j], 10) < 10:
                value, j = value + SMALL[words[j]], j + 1
        elif word in SCALES:  # "a million", "several hundred"
            value, j, rough = 1, i, rough or words[i - 1:i] != ["a"]
            start = tokens[i - 1].start() if words[i - 1:i] == ["a"] else start
        if value is not None:
            while j < len(words) and words[j] in SCALES:
                value, j = value * SCALES[words[j]], j + 1
            if small or word[0].isdigit() or value >= 10:
                end = tokens[j - 1].end()
                start -= text[start - 1:start] == "$"
                end += text[end:end + 1] == "%"
                found.append((text[start:end], value, rough))
        elif word in VAGUE:
            value = VAGUE[word]
            while j + 1 < len(words) and words[j] == "of" and words[j + 1] in VAGUE:
                value, j = value * VAGUE[words[j + 1]], j + 2
            found.append((text[start:tokens[j - 1].end()], value, True))
        i = j
    return found


def _same(figure, heard) -> bool:
    _, value, rough = figure
    size = math.floor(math.log10(value)) if value > 0 else None
    return any(abs(value - other) < 1e-6 * max(1.0, value)
               or rough and size is not None and other > 0 and math.floor(math.log10(other)) == size
               for _, other, _ in heard)


class _Builder:
    """Turns checked Gemini entries into reviewable units over the original segments."""

    def __init__(self, segments):
        self.ids = [s["id"] for s in segments]
        self.segments = segments
        self.issues = []
        self.agenda = None  # Agenda bill numbers, when the agenda loaded.
        self.titles = {}  # Agenda bill number -> official title.

    def cues(self, numbers):
        return [n for n in numbers or [] if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(self.ids)]

    def evidence(self, numbers):
        return list(dict.fromkeys(self.ids[n - 1] for n in self.cues(numbers)))

    def unheard(self, text, numbers, covered):
        """Gemini rewords figures ("hundreds of thousands" for "over a million"). Each figure must be said in
        the testimony; a rough one, said within two lines of the quote with the same order of magnitude."""
        near = sorted({m for n in self.cues(numbers) for m in range(n - 2, n + 3) if 1 <= m <= len(self.ids)})
        if not text or not near:
            return None
        close = figures(" ".join(self.segments[n - 1]["text"] for n in near), small=True)
        said = close + figures(" ".join(self.segments[n - 1]["text"] for n in covered), small=True)
        missing = [f[0] for f in figures(text) if not _same(f[:2] + (False,), said) and not _same(f, close)]
        if missing:
            return (f"Has a figure ({', '.join(dict.fromkeys(missing))}) that isn't in its quoted lines; "
                    "check it against the audio.")
        return None

    def claim(self, value, numbers, check=None, where=None):
        value = _text(value)
        item = field(value, self.evidence(numbers), True)
        basis = "direct" if value else "missing"
        reason = None if value else "Not stated in the AI draft."
        item.update(basis=basis, missing_reason=reason, metadata_refs=[], source_quotes=[], original_source_quotes=[],
                    original_basis=basis, original_missing_reason=reason)
        if check:
            # The flag sits on the field it is about, so review shows it in place.
            item["flag"] = _text(check, 500)
        return item

    def mentions(self, units):
        """Flags agenda bills said during a testimony that are on neither its bill list nor that of the same
        testifiers' overlapping entries (Gemini gives each bill of one testimony its own entry)."""
        if not self.agenda:
            return  # Without the agenda, "it's 25 years" reads as S.25.
        order = {sid: n for n, sid in enumerate(self.ids, 1)}
        def who(unit):
            return sorted((p["name"]["value"] or "").lower() for p in unit["testifiers"])
        for unit in units:
            span = set(unit["segment_ids"])
            listed = {b.strip() for other in units if other is unit or who(other) == who(unit)
                      and span & set(other["segment_ids"]) for b in (other["bill"]["value"] or "").split("/")}
            spoken = [b for b in gemini.mentioned_bills([{"n": order[s], "text": self.segments[order[s] - 1]["text"]}
                                                         for s in unit["segment_ids"]], self.agenda) if b not in listed]
            mine = _parts(unit["bill"]["value"])
            twins = [b for b in spoken if any(self.companion(b, m) for m in mine)]
            if twins:  # A Senate twin of a House bill (same official title) joins the bill line, unflagged.
                before, joined = unit["bill"]["value"], " / ".join(mine + twins)
                for item in [unit["bill"]] + [q["bill"] for q in unit["questions"]]:
                    if item["value"] == before:
                        item["value"] = item["original_value"] = joined
            others = [b for b in spoken if b not in twins]
            if others:
                unit["bill"]["flag"] = lead_flag(f"This testimony also mentions {', '.join(others)}; check which "
                                                 "points belong to which bill.", unit["bill"].get("flag"))

    def companion(self, bill, other) -> bool:
        """House and Senate versions of one act: different chambers, the same official title."""
        title = _words(self.titles.get(bill), TITLE_FILLER)
        return bool(title) and bill[:1] != other[:1] and title == _words(self.titles.get(other), TITLE_FILLER)

    def issue(self, message, evidence):
        item = field(message, list(evidence), True)
        # These are the only signs of possibly missed testimony, so they count and copy as ⚠ until approved.
        item.update(kind="gemini_check", source_target=None, source_quotes=[],
                    flag="Hearing-wide check: confirm this before copying the note, then click Approve.")
        self.issues.append(item)

    def person(self, name, numbers, check=None):
        return {"speaker_labels": [], "name": self.claim(name, numbers, check)}

    def asker(self, name, numbers):
        """Gemini gets no speaker labels, so a questioner's name is a guess unless it is said nearby."""
        given = name if isinstance(name, str) else ""
        words = gemini._norm(re.sub(r"\([^)]*\)", " ", given)).split()  # The surname, not a "(Chair)" note.
        cited = [n for n in numbers or [] if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(self.ids)]
        near = set(cited) | {n for n in (min(cited, default=0) - 1, min(cited, default=0) - 2) if n >= 1}
        heard = gemini._norm(" ".join(self.segments[n - 1]["text"] for n in sorted(near))).split()
        named = words and not {"unidentified", "unknown"} & set(gemini._norm(given).split())
        guessed = named and words[-1] not in heard
        return self.person(name, numbers, "Who asked is inferred from context; confirm by listening." if guessed else None)

    def unit(self, number, entry):
        bill = _text(entry.get("bill"), 400)
        where = bill or "Unknown bill"
        start, end = entry.get("start_cue"), entry.get("end_cue")
        cited = self.cues(n for _, item in gemini.items(entry) for n in item["cues"])
        if isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= len(self.ids):
            covered = range(start, end + 1)
        else:
            covered = range(min(cited), max(cited) + 1) if cited else range(0)
        testifiers = []
        for person in entry.get("testifiers") or []:
            if not isinstance(person, dict):
                continue
            name, role, organization = (_text(person.get(k), 300) for k in ("name", "role", "organization"))
            chamber = bool(role and organization and CHAMBER_TITLE.fullmatch(role))
            if chamber and LEGISLATURE.fullmatch(organization):
                organization = None  # "Senator, Massachusetts Senate" is a legislator; a "Student Senate" isn't.
            district = chamber and bool(organization) and bool(DISTRICT.search(organization))
            # Gemini also gives "Representative" for someone representing a group, so only a bare title
            # with no organization makes a legislator. Representing a group leaves just the group;
            # any other role stays with the organization.
            title = bool(role and (not organization or district) and BARE_TITLE.fullmatch(role))
            if title:
                if name and role.lower() not in name.lower() and not LEGISLATOR_TITLE.match(name):
                    name = f"{role} {name}"
            elif role and not (organization and role.lower() == "representative"):
                organization = ", ".join(x for x in (role, organization) if x)
            legislator = bool(title or name and LEGISLATOR_TITLE.match(name))
            org = self.claim(organization, person.get("cues"))
            if legislator and not organization:
                org["not_applicable"] = True  # Legislators testify as themselves.
            testifiers.append({"speaker_labels": [], "name": self.claim(name, person.get("cues"), person.get("check"),
                                                                        f"{where} testifier"),
                               "organization": org})
        position = _text(entry.get("position"), 20)
        position_check = entry.get("position_check")
        if position not in gemini.POSITIONS:
            position_check = f"The AI gave {position!r}; choose Support, Oppose or Unclear."
            position = "Unclear"
        arguments = [{"contributors": [], "text": self.claim(a.get("text"), a.get("cues"), lead_flag(
                          self.unheard(a.get("text"), a.get("cues"), covered), a.get("check")), f"{where} argument")}
                     for a in entry.get("arguments") or [] if isinstance(a, dict) and _text(a.get("text"))]
        asked = [q for q in entry.get("questions") or [] if isinstance(q, dict) and _text(q.get("question"))]
        starts = [min(self.cues(q.get("cues")), default=0) for q in asked]
        questions = []
        for q in asked:
            response, cues = _text(q.get("response")), self.cues(q.get("cues"))
            # One quote per Q&A, so the answer gets the lines after the question, up to the next question.
            answer = [n for n in self.cues(q.get("response_cues")) if n in covered]
            if not answer and cues:
                # The quote may be the question or the answer, so start at its last line and stay inside this
                # testimony, short of the next question.
                stop = min((s for s in starts if s > max(cues)), default=len(self.ids) + 1)
                answer = [n for n in range(max(cues), min(max(cues) + 5, stop)) if n in covered]
            answer = answer or cues
            asker = self.asker(q.get("asker"), q.get("cues"))
            surname = _surname(q.get("asker"))
            # A guessed asker's name repeated in the question text ("Senator Brennan asked...") is a guess too.
            named = asker["name"].get("flag") and surname and surname in gemini._norm(q.get("question") or "").split()
            # Without speaker labels, who answered is a guess unless a lone witness answers their own questions.
            answered = _surname(q.get("respondent"))
            guessed = answered and (len(testifiers) > 1 or answered not in {
                _surname(p["name"]["value"]) for p in testifiers})
            questions.append({
                "id": "question-" + uuid.uuid4().hex, "asker": asker,
                "asker_role": self.claim("legislator", q.get("cues")),
                "question": self.claim(q.get("question"), q.get("cues"),
                                       lead_flag(ASKED_NAME, q.get("check")) if named else q.get("check"), f"{where} question"),
                "bill": self.claim(bill, q.get("cues")),
                "responses": [{"respondent": self.person(q.get("respondent"), answer, ANSWERED if guessed else None),
                               "text": self.claim(response, answer, self.unheard(response, cues + answer, covered))}]
                             if response else [],
                "response_status": "answered" if response else "no_response_detected"})
        evidence = self.evidence(covered)
        return {
            "id": f"gemini-{number}", "source_unit_id": str(number), "segment_ids": evidence,
            "testifiers": testifiers, "joint_basis": [],
            "joint_confirmation": field("Presented together (AI draft).", evidence[:3], True) if len(testifiers) > 1 else None,
            "bill": self.claim(bill, entry.get("bill_cues"), entry.get("bill_check"), f"{where} bill"),
            "bill_title": self.claim(entry.get("bill_title"), entry.get("bill_cues")),
            "position": self.claim(position, entry.get("position_cues"), position_check, f"{where} position"),
            "arguments": arguments, "questions": questions,
            "question_status": "present" if questions else "none_detected_in_supplied_material",
            "questions_none": None if questions else field(_text(entry.get("questions_note")), evidence[-3:], True),
        }


def _saved_payload(path) -> dict | None:
    """A complete, readable earlier reply for exactly this prompt and model, if one was saved."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        text, finish = gemini.response_text(payload)
        gemini.normalize(json.loads(text))  # An unreadable reply is requested again, never reused.
    except (OSError, ValueError, TypeError, AttributeError, LookupError):
        return None
    return payload if finish in (None, "STOP") else None


def _save_payload(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        logging.exception("Could not save the Gemini reply; a retry will request it again")
    finally:
        if temporary.exists():
            temporary.unlink()


def _ran_out(path) -> bool:
    """A saved reply that ran out of room: those lines are split again instead of paying for the same reply."""
    try:
        return gemini.response_text(json.loads(path.read_text(encoding="utf-8")))[1] == "MAX_TOKENS"
    except (OSError, ValueError, TypeError, AttributeError, LookupError):
        return False


def _drafted_parts(cues, hearing, bills, model, key, client, cache_dir, wait, ranges=None) -> tuple:
    """Each part's (cues, normalized reply, payload, seconds, reused, saved path), covering every cue in order,
    and (payload, seconds) for every reply used, including any that ran out of room and were split.

    One call drafts the whole transcript when it fits. Otherwise it's split (parts.py), and a part whose reply
    runs out of room is split again. ranges: the cue ranges a rebuild keeps from the first draft. Each part's
    reply is saved, so a retry pays only for the parts that didn't finish."""
    whole = len(cues)

    def ask(part, splittable):
        prompt = gemini.build_prompt(part, hearing, bills, None if len(part) == whole else whole)
        saved = (cache_dir / (hashlib.sha256(f"{model}\n{prompt}".encode("utf-8")).hexdigest() + ".json")
                 if cache_dir is not None else None)
        payload = _saved_payload(saved) if saved else None
        if payload is None and splittable and saved is not None and _ran_out(saved):
            payload = json.loads(saved.read_text(encoding="utf-8"))  # Split again; a short one is asked again.
        if payload is not None:
            return payload, 0.0, True, saved
        if wait is not None:
            wait(0)  # The app is closing: don't start another paid call.
        where = "" if len(part) == whole else f" on lines {part[0]['n']}–{part[-1]['n']} of this long hearing"
        try:
            payload, seconds = client.call_gemini(prompt, model, key, log=logging.info, context=tls_context(),
                                                  wait=wait)
        except gemini.GeminiError as error:
            raise InputError(f"Gemini drafting failed{where}, so no draft was saved: {error}" + (
                " Retry this hearing; the parts already drafted are reused at no charge." if where else "")) from None
        if saved is not None:
            _save_payload(saved, payload)  # Saved before parsing, so any later failure can reuse it.
        return payload, seconds, False, saved

    pending = [cues[first - 1:last] for first, last in ranges] if ranges else parts.split(cues, parts.needed(cues))
    done, spent = [], []
    while pending:
        part = pending.pop(0)
        splittable = (not ranges and len(part) >= 2 * parts.MIN_CUES
                      and len(done) + len(pending) + 2 <= parts.MAX_PARTS)
        payload, seconds, reused, saved = ask(part, splittable)
        spent.append((payload, seconds))
        text, finish = gemini.response_text(payload)
        blocked = (payload.get("promptFeedback") or {}).get("blockReason")
        if blocked:  # Checked as each reply comes back, so no further part is paid for.
            raise InputError(f"Gemini declined to draft this transcript ({blocked}). Retrying won't change that; "
                             "take this hearing's notes by hand.")
        if splittable and finish == "MAX_TOKENS":
            pending[:0] = parts.split(part, 2)  # The reply ran out of room: the same lines in two calls.
            continue
        if finish not in (None, "STOP"):
            raise InputError(f"Gemini stopped early ({finish}), so no draft was saved. "
                             + ("Its reply ran out of room. Retry this hearing once (each attempt is charged); if "
                                "it stops the same way, write a comment on the checklist." if finish == "MAX_TOKENS"
                                else "Retry this hearing."))
        try:
            piece = gemini.normalize(json.loads(text))
        except ValueError:
            raise InputError("Gemini's reply was not readable, so no draft was saved. Retry this hearing.") from None
        done.append((part, piece, payload, seconds, reused, saved))
    return done, spent


def _kept_ranges(ranges, count):
    """A rebuild's saved cue ranges, if they still cover the transcript exactly once, in order."""
    try:
        pairs = [(int(r["first_cue"]), int(r["last_cue"])) for r in ranges or []]
    except (KeyError, TypeError, ValueError):
        return None
    edges = [first for first, _ in pairs] + [count + 1]
    ok = len(pairs) > 1 and pairs[0][0] == 1 and all(last + 1 == nxt for (_, last), nxt in zip(pairs, edges[1:]))
    return pairs if ok and all(first <= last for first, last in pairs) else None


JOINED = "This came from the next drafting part and was joined to this testimony; confirm the same person said it."


def _fold(whole, part, titles, unnamed):
    """Adds the later part of a testimony to the earlier one, skipping points it repeats."""
    for key, text in (("arguments", "text"), ("questions", "question")):
        seen = {gemini._norm(item.get(text) or "") for item in whole.get(key) or []}
        for item in part.get(key) or []:
            if gemini._norm(item.get(text) or "") not in seen:
                if unnamed and key == "arguments" and not any(JOINED in (a.get("check") or "")
                                                              for a in whole.get(key) or []):
                    item["check"] = lead_flag(JOINED, item.get("check"))  # The first joined point, once.
                whole.setdefault(key, []).append(item)
                seen.add(gemini._norm(item.get(text) or ""))
    # The later part saw the end of the testimony, so it knows whether questions followed.
    whole["questions_note"] = part.get("questions_note") or whole.get("questions_note")
    later = {_surname(p.get("name")): p for p in part.get("testifiers") or []}
    for person in whole.get("testifiers") or []:
        other = later.get(_surname(person.get("name"))) or {}
        person["organization"] = person.get("organization") or other.get("organization")
    stated = ("Support", "Oppose")
    if part.get("position") in stated and whole.get("position") not in stated:
        whole.update({k: part.get(k) for k in ("position", "position_quote", "position_cues", "position_check")})
    elif part.get("position") in stated and part["position"] != whole.get("position"):
        whole["position_check"] = lead_flag(f"The AI gave both {whole['position']} and {part['position']}; "
                                            "check which.", whole.get("position_check"))
        whole["position"] = "Unclear"
    if _parts(part.get("bill")):
        _add_bill(whole, part, titles)
    whole["end_cue"] = max(whole["end_cue"], part.get("end_cue") or whole["end_cue"])


def join_at_cuts(entries, firsts, cues, procedural, titles) -> list:
    """A testimony a drafting cut ran through comes back as an entry ending at the cut and entries starting there
    (one per bill) for the same people, or naming nobody. Those become one testimony. An unnamed one is joined
    only where the speech seems to go on across the cut (parts.continues), since a new testimony the later part
    couldn't name must not be given to the person before it.

    Returns, for each cut, the joined entry or None. Entries carry the 0-based "part" they were drafted in."""
    joined = []
    for i, first in enumerate(firsts):
        testimony = [e for e in entries if e.get("testifiers") or e.get("arguments")]
        before = [e for e in testimony if e.get("part") == i]
        whole = before[-1] if before else None
        if whole is None or not isinstance(whole.get("end_cue"), int) or whole["end_cue"] < first - 4:
            joined.append(None)
            continue
        named = {_surname(p.get("name")) for p in whole.get("testifiers") or []} - {""}
        goes_on = parts.continues(cues, first, procedural, whole.get("start_cue") or 0)
        # Only the introduction before the cut (a name, no point yet): what follows is that testimony, even if
        # the later part heard the name differently (on a real hearing, two spellings of one surname).
        intro = (bool(named) and not whole.get("arguments")
                 and not parts.moved_on(cues, first, procedural, whole.get("start_cue") or 0))
        taken, heard = [], None
        for part in [e for e in testimony if e.get("part") == i + 1]:
            names = {_surname(p.get("name")) for p in part.get("testifiers") or []} - {""}
            same = names and named and names <= named
            if (not isinstance(part.get("start_cue"), int) or part["start_cue"] > first + 3
                    or not (same or not names and goes_on or intro and (heard is None or names == heard))):
                break
            if intro and names and not same:
                heard = names
            taken.append((part, not names))
        for part, unnamed in taken:
            _fold(whole, part, titles, unnamed)
            entries.remove(part)
        if heard:
            other = " and ".join(p["name"] for p in taken[0][0].get("testifiers") or [] if p.get("name"))
            for person in whole.get("testifiers") or []:
                person["check"] = lead_flag(f"The next drafting part heard this name as {other}; confirm it.",
                                            person.get("check"))
        if taken:
            whole["part"] = i + 1  # A testimony running through a whole part can join at the next cut too.
        joined.append(whole if taken else None)
    return joined


def _mark_agenda(entries, titles):
    """Official titles for agenda bills, and a flag on bills the agenda doesn't list."""
    for entry in entries:
        if entry.get("bill") in titles and not entry.get("bill_title"):
            entry["bill_title"] = titles[entry["bill"]]
        # check() only counts these; the flag shows them before merging carries them into a bill list.
        if (titles and not entry.get("bill_check")
                and any(p.strip() not in titles for p in (entry.get("bill") or "").split("/") if p.strip())):
            entry["bill_check"] = NOT_ON_AGENDA


REDRAFT_LINES = 600  # About half an hour: a gap longer than this isn't one skipped testimony.
REDRAFT_MIN = 10  # Fewer lines than this is a bill mentioned in passing, not a testimony (real skipped testimonies spanned ~70 lines).
REDRAFT_CALLS = 8  # the worst real run skipped 5; more than this is a reply gone wrong, not worth paying for.
REDRAFT_TRIES = 2  # A redraft that comes back empty is asked once more.
REDRAFT_BILL = ("Bill taken from the first draft, which listed it here; this testimony doesn't say it. "
                "Check it against the hearing.")


def _redraft_skipped(entries, cues, hearing, bills, titles, model, key, client, cache_dir, wait, rebuilding) -> list:
    """Gemini sometimes lists a testimony's bills but drafts nothing for it (5 times on one run of a real 4-hour hearing, and
    not the next). The lines no testimony covers around such an entry are drafted once more with the same rules,
    and what comes back takes the empty entries' place. If nothing comes back, the empty entries stay and the
    draft warns that a testimony may be missing.

    Only lines no drafted testimony covers are sent, so nothing is drafted twice. At most REDRAFT_CALLS calls, and
    none after one fails (Gemini is then probably busy for all of them). Each reply is saved; a rebuild uses only
    saved replies. Returns what was redrafted, for the provenance."""
    def content(entry):
        return entry.get("testifiers") or entry.get("arguments")
    covered = {n for e in entries if content(e) and isinstance(e.get("start_cue"), int)
               and isinstance(e.get("end_cue"), int) for n in range(e["start_cue"], e["end_cue"] + 1)}
    done, i, calls, failed = [], 0, 0, False
    while i < len(entries):
        if content(entries[i]):
            i += 1
            continue
        j = i
        while j < len(entries) and not content(entries[j]):
            j += 1
        group = entries[i:j]
        spots = [n for e in group if isinstance(e.get("start_cue"), int) and isinstance(e.get("end_cue"), int)
                 for n in range(max(1, e["start_cue"]), min(len(cues), e["end_cue"]) + 1) if n not in covered]
        if not spots or not any(_parts(e.get("bill")) for e in group) or failed or calls >= REDRAFT_CALLS:
            i = j
            continue
        first = last = spots[0]  # The uncovered lines around the empty entry's first uncovered line.
        while first > 1 and first - 1 not in covered:
            first -= 1
        while last < len(cues) and last + 1 not in covered:
            last += 1
        if not REDRAFT_MIN <= last - first + 1 <= REDRAFT_LINES:
            i = j
            continue
        recovered, how, tries = [], None, 0
        # Gemini also skips on a redraft now and then (once in four tries on one real testimony), so an
        # empty one is asked once more.
        while not recovered and tries < REDRAFT_TRIES and not failed and calls < REDRAFT_CALLS:
            tries += 1
            found, how = _redraft(cues[first - 1:last], len(cues), hearing, bills, titles, model, key, client,
                                  cache_dir, wait, rebuilding, tries)
            calls += how == "asked"
            failed = how == "failed"
            recovered = [e for e in found if content(e) and isinstance(e.get("start_cue"), int)
                         and isinstance(e.get("end_cue"), int) and first <= e["start_cue"] and e["end_cue"] <= last]
            if how == "not asked":
                break
        listed = list(dict.fromkeys(b for e in group for b in _parts(e.get("bill"))))
        for entry in recovered:
            if not _parts(entry.get("bill")) and listed:  # The bill was named before these lines (seen on a real hearing).
                entry["bill"] = " / ".join(listed)
                entry["bill_check"] = lead_flag(REDRAFT_BILL, entry.get("bill_check"))
        if recovered:
            named = {b for e in recovered for b in _parts(e.get("bill"))}
            kept = [e for e in group if not set(_parts(e.get("bill"))) <= named]  # A bill still missing is warned.
            entries[i:j] = recovered + kept
            j = i + len(recovered) + len(kept)
            covered.update(range(first, last + 1))
        done.append({"first_cue": first, "last_cue": last, "recovered": len(recovered), "gemini": how,
                     "tries": tries})
        i = j
    return done


def _redraft(part, whole, hearing, bills, titles, model, key, client, cache_dir, wait, rebuilding,
             attempt=1) -> tuple:
    """(entries Gemini drafts for these lines alone, ready to join the draft; how: "saved", "asked", "not asked"
    on a rebuild without a saved reply, or "failed"). Closing the app still stops it. A second attempt sends the
    same prompt but is saved apart, so it isn't answered by the first, empty reply."""
    prompt = gemini.build_prompt(part, hearing, bills, whole)
    again = f"\nattempt {attempt}" if attempt > 1 else ""
    saved = (cache_dir / (hashlib.sha256(f"{model}\n{prompt}{again}".encode("utf-8")).hexdigest() + ".json")
             if cache_dir is not None else None)
    payload, how = (_saved_payload(saved) if saved else None), "saved"
    if payload is None:
        if rebuilding:
            return [], "not asked"  # A rebuild never pays for drafting; the warning stays.
        if wait is not None:
            wait(0)
        try:
            payload, _ = client.call_gemini(prompt, model, key, log=logging.info, context=tls_context(), wait=wait)
        except gemini.GeminiError as error:
            logging.warning("Redrafting lines %s-%s failed: %s", part[0]["n"], part[-1]["n"], error)
            return [], "failed"
        how = "asked"
        if saved is not None:
            _save_payload(saved, payload)
    text, finish = gemini.response_text(payload)
    if finish not in (None, "STOP") or (payload.get("promptFeedback") or {}).get("blockReason"):
        return [], how
    try:
        raw = gemini.normalize(json.loads(text))
    except ValueError:
        return [], how
    gemini.merge_fragments(raw)
    gemini.resolve(raw, part)
    _mark_agenda(raw["entries"], titles)
    attach_bill_only(raw["entries"], titles)
    merge_multi_bill(raw["entries"])
    return raw["entries"], how


CLOSED = re.compile(r"\bseeing none\b", re.I)


def _flag_merged(builder, units, cues):
    """Gemini sometimes runs two testimonies together (on a real hearing, one "Unknown" entry held two people). The
    chair's "Seeing none" after asking for questions closes a testimony, so one well inside a drafted testimony is
    flagged. It matched nothing else in 142 testimonies of that hearing."""
    order, warned = {sid: n for n, sid in enumerate(builder.ids, 1)}, set()
    for unit in units:
        lines = [order[s] for s in unit["segment_ids"] if s in order]
        if not lines:
            continue
        first, last = min(lines), max(lines)
        inside = [n for n in range(first + 3, last - 5) if CLOSED.search(cues[n - 1]["text"])]
        if not inside or inside[0] in warned:
            continue  # One warning per place: a testimony on several bills is several units over the same lines.
        warned.add(inside[0])
        names = [p["name"]["value"] for p in unit["testifiers"] if p["name"]["value"]
                 and p["name"]["value"].lower() not in ("unknown", "unclear", "unidentified")]
        start = cues[inside[0] - 1].get("start")
        at = gemini.clock(start) if isinstance(start, (int, float)) else f"transcript line {cues[inside[0] - 1]['line'] or inside[0]}"
        who = f"{' and '.join(names)}'s testimony" if names else "A testimony"
        builder.issue(f"{who} may hold two: the chair closed one at {at} (\"Seeing none\") and it goes on after "
                      "that. Listen there and check who spoke.", [builder.ids[inside[0] - 1]])


def _same_span(entry, other) -> bool:
    """Two entries over the same lines, by their ranges or their bill quotes."""
    ranges = [(e.get("start_cue"), e.get("end_cue")) for e in (entry, other)]
    if all(isinstance(n, int) for pair in ranges for n in pair):
        return ranges[0][0] <= ranges[1][1] and ranges[1][0] <= ranges[0][1]
    return bool(set(entry.get("bill_cues") or []) & set(other.get("bill_cues") or []))


def _surname(name) -> str:
    """The last word of a name, without a "(Chair)" note; empty for unidentified speakers."""
    words = gemini._norm(re.sub(r"\([^)]*\)", " ", name if isinstance(name, str) else "")).split()
    return "" if not words or {"unidentified", "unknown", "unclear"} & set(words) else words[-1]


def _name(person) -> str:
    return (person.get("name") or "").strip().lower()


def _names(entry) -> tuple:
    return tuple(sorted(_name(p) for p in entry.get("testifiers") or []))


TITLE_FILLER = {"an", "act", "relative", "to", "the", "establishing"}
ORGANIZATION_FILLER = {"a", "and", "for", "of", "s", "the"}
REPEATED_QA = ("May repeat another Q&A in this testimony (same moment in the audio). "
               "If it does, click Remove Q&A on one of them.")


def _words(text, filler) -> set:
    return set(gemini._norm(text or "").split()) - filler


def _doubt(kept, later, bill):
    """A merged-in entry's check stays on its field, marked with the bill it was about."""
    if not later or kept and later in kept:
        return kept
    later = f"{bill}: {later}" if bill else later
    return f"{kept}; {later}" if kept else later


def _companions(previous, entry) -> bool:
    """Companion bills share a title. A testifier who names another organization for the next bill
    spoke in another capacity, so those entries stay separate too."""
    title = _words(entry.get("bill_title"), TITLE_FILLER)
    if not title or title != _words(previous.get("bill_title"), TITLE_FILLER):
        return False
    later = {_name(p): p for p in entry.get("testifiers") or []}
    for person in previous.get("testifiers") or []:
        kept = _words(person.get("organization"), ORGANIZATION_FILLER)
        other = _words((later.get(_name(person)) or {}).get("organization"), ORGANIZATION_FILLER)
        if kept and other and not other <= kept:
            return False
    return True


def _parts(value) -> list:
    return [p.strip() for p in (value or "").split(" / ") if p.strip()]


def _add_bill(entry, other, titles, shared=False):
    """Puts other's bills on entry's bill list. A companion (same agenda title, or the AI's title without an
    agenda) joins silently; another bill flags the testimony, since its points may belong to either."""
    bills, shown = _parts(entry.get("bill")), _parts(entry.get("bill_title"))
    known = [_words(t, TITLE_FILLER) for t in [titles.get(b) for b in bills] + shown if t]
    check = entry.get("bill_check") or ""
    several = MULTI_BILL in check
    for bill in _parts(other.get("bill")):
        if bill in bills:
            continue
        title = titles.get(bill) or (other.get("bill_title") if len(_parts(other.get("bill"))) == 1 else None)
        words = _words(title, TITLE_FILLER)
        companion = bool(words) and words in known
        several = several or bool(bills or shown or shared) and not companion
        bills.append(bill)
        if title and not companion:
            shown.append(title)
        if words:
            known.append(words)
    entry["bill"] = " / ".join(bills) or None
    entry["bill_title"] = " / ".join(dict.fromkeys(shown)) or None
    entry["bill_cues"] = sorted(set(entry.get("bill_cues") or []) | set(other.get("bill_cues") or []))
    check = _doubt(check.replace(MULTI_BILL, "").strip() or None, other.get("bill_check"), other.get("bill"))
    entry["bill_check"] = lead_flag(MULTI_BILL, check) if several else check


def attach_bill_only(entries: list, titles: dict) -> tuple[int, int]:
    """Gemini often gives one testimony as several entries over the same cues: bill-only entries with no
    testifier or argument beside a testimony with no bill, or a testifier-only entry beside an arguments-only
    one. Those become one testimony. Returns (bill-only entries attached, split entries rejoined)."""
    groups = {}
    for entry in entries:
        span = tuple(entry.get("model_range") or (entry.get("start_cue"), entry.get("end_cue")))
        if all(isinstance(n, int) for n in span):  # The model's own range: resolve() moves each entry separately.
            groups.setdefault(span, []).append(entry)
    gone, attached, rejoined = set(), 0, 0
    for group in groups.values():
        speakers = [e for e in group if e.get("testifiers") and not e.get("arguments")]
        if len({_names(e) for e in speakers}) > 1:
            speakers = []  # Several people: whose points are whose isn't known, so the halves stay apart, flagged.
        for part in [e for e in group if e.get("arguments") and not e.get("testifiers")] if speakers else []:
            whole = speakers[0]
            for key in ("arguments", "questions"):
                whole[key] = (whole.get(key) or []) + (part.get(key) or [])
            whole["questions_note"] = whole.get("questions_note") or part.get("questions_note")
            if whole.get("position") not in gemini.POSITIONS and part.get("position") in gemini.POSITIONS:
                whole.update({k: part.get(k) for k in ("position", "position_cues", "position_check")})
            elif part.get("position") in gemini.POSITIONS and part["position"] != whole.get("position"):
                whole["position_check"] = lead_flag(f"The AI gave both {whole['position']} and {part['position']}; "
                                                    "check which.", whole.get("position_check"))
                whole["position"] = "Unclear"
            _add_bill(whole, part, titles)
            gone.add(id(part))
            rejoined += 1
        testimony = [e for e in group if id(e) not in gone and (e.get("testifiers") or e.get("arguments"))]
        for empty in [e for e in group if not e.get("testifiers") and not e.get("arguments")]:
            if testimony and _parts(empty.get("bill")):
                for entry in testimony:  # Several testimonies over the same cues: which bill is whose isn't known.
                    _add_bill(entry, empty, titles, shared=len(testimony) > 1)
                gone.add(id(empty))
                attached += 1
    entries[:] = [e for e in entries if id(e) not in gone]
    return attached, rejoined


def merge_multi_bill(entries: list) -> int:
    """One testimony about companion bills, with one position, becomes one entry listing every bill.
    Other bills stay separate, because a position, argument or organization may belong to only one."""
    merged, out = 0, []
    for entry in entries:
        previous = out[-1] if out else None
        if (previous is not None and _names(entry) and _names(entry) == _names(previous)
                and entry.get("position") == previous.get("position") and entry.get("bill")
                and isinstance(previous.get("end_cue"), int) and isinstance(entry.get("start_cue"), int)
                and entry["start_cue"] <= previous["end_cue"] + 2 and _companions(previous, entry)):
            bill = entry["bill"]
            for key in ("bill", "bill_title"):  # Joined values are split again so a repeat isn't listed twice.
                parts = (previous.get(key) or "").split(" / ") + (entry.get(key) or "").split(" / ")
                previous[key] = " / ".join(dict.fromkeys(filter(None, parts))) or None
            for key in ("bill_cues", "position_cues"):
                previous[key] = sorted(set(previous.get(key) or []) | set(entry.get(key) or []))
            for key in ("bill_check", "position_check"):
                previous[key] = _doubt(previous.get(key), entry.get(key), bill)
            later = {_name(p): p for p in entry.get("testifiers") or []}
            for person in previous.get("testifiers") or []:
                other = later.get(_name(person)) or {}
                person["check"] = _doubt(person.get("check"), other.get("check"), bill)
                person["organization"] = person.get("organization") or other.get("organization")
                person["cues"] = sorted(set(person.get("cues") or []) | set(other.get("cues") or []))
            previous["end_cue"] = max(previous["end_cue"], entry.get("end_cue") or previous["end_cue"])
            for key, text in (("arguments", "text"), ("questions", "question")):
                kept = list(previous.get(key) or [])
                seen = {gemini._norm(item.get(text) or "") for item in kept}
                for item in entry.get(key) or []:
                    if gemini._norm(item.get(text) or "") not in seen:
                        # Each companion entry tends to describe the same exchange in other words. A Q&A citing
                        # the same moment as a kept one stays, flagged, so a real second exchange isn't lost.
                        # Arguments aren't compared: separate points often share a line.
                        if key == "questions" and any(set(item.get("cues") or []) & set(other.get("cues") or [])
                                                      for other in kept):
                            item["check"] = _doubt(item.get("check"), REPEATED_QA, None)
                        previous.setdefault(key, []).append(item)
                        seen.add(gemini._norm(item.get(text) or ""))
            previous["questions_note"] = previous.get("questions_note") or entry.get("questions_note")
            merged += 1
        else:
            out.append(entry)
    entries[:] = out
    return merged


def gemini_draft(document: dict, key: str, model: str, client=gemini, cache_dir=None, wait=None,
                 fetch_agenda=None, rebuilding=False, ranges=None) -> dict:
    """ranges: the cue ranges a long hearing was first drafted in (its provenance "parts"), kept by a rebuild."""
    hearing_url = document.get("hearing_url") or (document.get("hearing") or {}).get("hearing_url")
    validated = validate(document)
    segments = validated["segments"]
    cues = [{"n": i + 1, "start": s.get("start"), "end": s.get("end"), "text": s["text"],
             "line": s.get("source_line")} for i, s in enumerate(segments)]
    if parts.needed(cues) > parts.MAX_PARTS:
        words = sum(len(s["text"].split()) for s in segments)
        raise InputError(f"This transcript is far longer than a hearing ({words:,} words). Check that the right "
                         "file was added; Retry won't change this.")
    builder = _Builder(segments)
    bills, agenda_url, committee = None, None, None
    if hearing_url:
        try:
            loaded = (fetch_agenda or agenda.fetch)(hearing_url)
            bills, agenda_url, committee = loaded["bills"], loaded["url"], loaded.get("committee")
        except InputError as error:
            builder.issue(f"The hearing page's agenda couldn't be loaded ({error}), so bill numbers and titles "
                          "weren't checked against it.", [])
    drafted, spent = _drafted_parts(cues, validated["hearing"], bills, model, key, client, cache_dir, wait,
                                    _kept_ranges(ranges, len(cues)))
    raw, merged = {"entries": [], "procedural": []}, 0
    for number, (_, piece, *_) in enumerate(drafted):
        merged += gemini.merge_fragments(piece)  # Within a part; join_at_cuts handles a testimony across parts.
        for entry in piece["entries"]:
            entry["part"] = number
        raw["entries"] += piece["entries"]
        raw["procedural"] += piece["procedural"]
    seconds, reused, usage = sum(took for _, took in spent), all(d[4] for d in drafted), {}
    for answer, _ in spent:  # Every call, including one that ran out of room and was split.
        for name, count in (answer.get("usageMetadata") or {}).items():
            if isinstance(count, int):
                usage[name] = usage.get(name, 0) + count
    payload = drafted[0][2]
    stats = gemini.resolve(raw, cues)
    checks = gemini.check(raw, cues, [b["bill"] for b in bills] if bills else None)
    titles = {b["bill"]: b["title"] for b in bills or []}
    builder.agenda, builder.titles = set(titles) or None, titles
    _mark_agenda(raw["entries"], titles)
    attached, rejoined = attach_bill_only(raw["entries"], titles)
    combined = merge_multi_bill(raw["entries"])
    firsts = [part[0]["n"] for part, *_ in drafted[1:]]
    joined = join_at_cuts(raw["entries"], firsts, cues, raw["procedural"], titles)
    for entry in raw["entries"]:
        entry.pop("part", None)
    redrafted = _redraft_skipped(raw["entries"], cues, validated["hearing"], bills, titles, model, key, client,
                                 cache_dir, wait, rebuilding)
    units = []
    empty = []  # Entries naming a bill with nothing drafted: on a real hearing, Gemini sometimes skipped a whole testimony.
    for number, entry in enumerate(raw["entries"], 1):
        if entry.get("testifiers") or entry.get("arguments"):
            units.append(builder.unit(number, entry))
        elif empty and _same_span(empty[-1][-1], entry):
            empty[-1].append(entry)  # One warning per place, not one per bill.
        else:
            empty.append([entry])
    for group in empty:
        named_bills = " / ".join(dict.fromkeys(b for e in group for b in _parts(e.get("bill")))) or "a bill"
        cited = sorted({n for e in group for n in e.get("bill_cues") or [] if isinstance(n, int)}) or [
            e["start_cue"] for e in group if isinstance(e.get("start_cue"), int)][:1]
        start = cues[cited[0] - 1].get("start") if cited and 1 <= cited[0] <= len(cues) else None
        at = f" at {gemini.clock(start)}" if isinstance(start, (int, float)) else ""
        builder.issue(f"Gemini listed {_text(named_bills, 60)}{at} but drafted no testimony there, so one may be missing. "
                      "Listen, and add it if someone testified.", builder.evidence(cited))
    if not units and rebuilding:  # A rebuild keeps the saved reply; the draft it already made stays.
        raise InputError("Rebuilding from the saved AI reply gave no testimony with this build's fixes, so nothing "
                         "was changed. The existing draft is still there to review.")
    if not units:
        for *_, saved in drafted:
            if saved is not None and saved.exists():
                try:  # Set aside, so a retry asks Gemini again. Backups take only <sha256>.json replies.
                    os.replace(saved, saved.with_name(f"{saved.stem}.unusable-{uuid.uuid4().hex}.json"))
                except OSError:
                    logging.exception("Could not set aside the Gemini reply with no testimony; a retry will reuse it")
        raise InputError("Gemini found no testimony in this transcript. If people testified in this recording, "
                         "retry once; a clip of only the chair's opening has nothing to draft.")
    builder.mentions(units)
    _flag_merged(builder, units, cues)
    # A testimony running into the last two lines was probably cut off, unless the chair closes there.
    order = {sid: n for n, sid in enumerate(builder.ids, 1)}
    cut = [u for u in units if u["segment_ids"] and order[u["segment_ids"][-1]] >= len(segments) - 1]
    start = min((order[u["segment_ids"][0]] for u in cut), default=0)
    if cut and not any(isinstance(p.get("start_cue"), int) and isinstance(p.get("end_cue"), int)
                       and p["start_cue"] > start and p["end_cue"] >= len(segments) - 1 for p in raw["procedural"]):
        names = list(dict.fromkeys(p["name"]["value"] for u in cut for p in u["testifiers"] if p["name"]["value"]
                                   and p["name"]["value"].lower() not in ("unknown", "unclear", "unidentified")))
        builder.issue("The recording ends during " + (f"{' and '.join(names)}'s testimony" if names else
                      "the last testimony") + "; it may continue in the next part.", cut[-1]["segment_ids"][-3:])
        for unit in cut:
            if not unit["questions"]:  # Its questions may come after the cut, so "None noted" stays open.
                unit.update(question_status="coverage_incomplete",
                            questions_none=field(None, unit["segment_ids"][-3:], True))
    for n, entry in zip(firsts, joined):
        start = cues[n - 1].get("start")
        at = gemini.clock(start) if isinstance(start, (int, float)) else f"transcript line {cues[n - 1]['line'] or n}"
        names = [p["name"] for p in (entry or {}).get("testifiers") or [] if isinstance(p.get("name"), str) and p["name"]]
        builder.issue(f"This long hearing was drafted in {len(drafted)} parts, and one starts at {at}. " + (
            (f"{' and '.join(names)}'s testimony" if names else "A testimony") + " ran across that point and was "
            "joined into one; check it there." if entry else "Check the testimony just before and after that point: "
            "one may be cut short or listed twice."), builder.evidence([max(1, n - 1), n]))
    filled = [(r["first_cue"], r["last_cue"]) for r in redrafted if r["recovered"]]
    for gap in checks["uncovered_spans_20s_or_more"]:
        first, last = (int(n) for n in gap["cues"].split("-"))
        if any(a <= first and last <= b for a, b in filled):
            continue  # Drafted again, and testimony was found there.
        builder.issue(f"Not covered by any testimony from {gap['from']}"
                      + (f" ({gap['seconds']} s)" if gap["seconds"] else "")
                      + f": \"{gap['starts']}\". Listen to confirm nothing was missed.",
                      builder.evidence(range(first, last + 1))[:3])
    listed = {b for unit in units for b in _parts(unit["bill"]["value"])}
    for bill, numbers in checks["spoken_agenda_bills_missing_from_draft"].items():
        if bill in listed:
            continue  # It joined a testimony's bill line as a companion.
        builder.issue(f"{bill} is mentioned in the transcript but has no testimony entry. Check whether it was discussed.",
                      builder.evidence(numbers[:3]))
    provenance = {"provider": "gemini", "multi_bill_testimonies_combined": combined, "model": model, "model_version": payload.get("modelVersion"),
                  "seconds": round(seconds, 1), "input_sha256": fingerprint(validated),
                  "prompt_tokens": usage.get("promptTokenCount"), "output_tokens": usage.get("candidatesTokenCount"),
                  "reused_saved_reply": reused, "agenda_url": agenda_url, "agenda_bills": len(bills or []),
                  "committee": committee,  # From the hearing page's heading; names the member list to check.
                  # The official numbers and titles, so typed bill corrections can be checked against them.
                  "agenda": [{"bill": b["bill"], "title": b["title"], "sponsor": b.get("sponsor")} for b in bills or []],
                  "quotes_found": stats["found"], "quotes": stats["quotes"],
                  "split_entries_rejoined": merged + rejoined, "bill_only_entries_attached": attached,
                  "mechanical_problems": len(checks["problems"]), "model_selected_final": False, "drafting": DRAFTING,
                  "redrafted": redrafted}
    if len(drafted) > 1:  # A rebuild drafts the same ranges, so it finds each saved reply.
        provenance["parts"] = [{"first_cue": part[0]["n"], "last_cue": part[-1]["n"], "reused_saved_reply": kept,
                                "seconds": round(took, 1)} for part, _, _, took, kept, _ in drafted]
    draft = {
        "schema_version": 1, "draft_format": FORMAT, "synthetic": validated["synthetic"],
        "provider": "gemini-draft-v1", "hearing": copy.deepcopy(validated["hearing"]),
        "segments": copy.deepcopy(segments), "units": units, "unassigned_questions": [],
        "issues": builder.issues, "coverage_ledger": [], "extraction_provenance": provenance, "review_log": [],
    }
    for name in ("source_format", "source_text", "source_text_filename", "source_text_sha256"):
        if name in validated:
            draft[name] = copy.deepcopy(validated[name])
    return draft


def add_issue(draft, message, evidence=()):
    """A hearing-wide check added after drafting, counted and copied like the drafter's own."""
    item = field(message, list(evidence), True)
    item.update(kind="gemini_check", source_target=None, source_quotes=[],
                flag="Hearing-wide check: confirm this before copying the note, then click Approve.")
    draft["issues"].append(item)


class ExtractionService:
    fetch_roster = staticmethod(roster.fetch)

    def __init__(self, store, cancel_event, keys=None, client=gemini):
        self.store = store
        self.cancel_event = cancel_event
        self.keys = keys or KeychainKeys()
        self.client = client

    def settings(self) -> dict:
        saved = self.store.extraction_settings()
        if saved.get("provider") == "gemini" and saved.get("model") in MODELS:
            return {"provider": "gemini", "model": saved["model"]}
        # Retired local-model settings fall back to the rule baseline.
        return {"provider": "rules", **({"retired": saved["provider"]} if saved.get("provider") not in (None, "rules") else {})}

    @property
    def background(self) -> bool:
        """Hosted drafting takes minutes, so it runs in the queue rather than in a request."""
        return self.settings()["provider"] == "gemini"

    def info(self) -> dict:
        return {**self.settings(), "key_saved": bool(self.keys.get()), "models": list(MODELS),
                "key_storage": "Mac Keychain" if self.keys.available() else "GEMINI_API_KEY environment variable"}

    def configure(self, provider, model=None, api_key=None) -> dict:
        if provider == "rules":
            settings = {"provider": "rules"}
        elif provider == "gemini":
            model = model or gemini.DEFAULT_MODEL
            if model not in MODELS:
                raise InputError("Choose a supported Gemini model.")
            pasted = api_key.strip() if isinstance(api_key, str) else ""
            if pasted and not re.fullmatch(r"[A-Za-z0-9._\-]{20,300}", pasted):
                raise InputError("That doesn't look like a Gemini API key. Copy it again from AI Studio.")
            key = pasted or self.keys.get()
            if not key:
                raise InputError("Paste the Gemini API key first.")
            try:
                self.client.check_key(key, model, context=tls_context())
            except gemini.GeminiError as error:
                raise InputError(str(error)) from None
            if pasted:
                self.keys.set(pasted)
            settings = {"provider": "gemini", "model": model}
        else:
            raise InputError("Choose the rule baseline or Gemini.")
        self.store.set_extraction(settings)
        return self.info()

    def forget_key(self) -> dict:
        self.keys.delete()
        if self.settings()["provider"] == "gemini":
            self.store.set_extraction({"provider": "rules"})
        return self.info()

    def __call__(self, document):
        settings = self.settings()
        if settings["provider"] != "gemini":
            return extract(document)
        key = self.keys.get()
        if not key:
            raise InputError("No Gemini key is saved. Open AI extraction, paste the key, then retry this hearing.")
        recording = document.get("recording_path")  # Kept out of the transcript and its fingerprint.
        document = {k: v for k, v in document.items() if k != "recording_path"}
        draft = gemini_draft(document, key, settings["model"], self.client,
                             cache_dir=self.store.directory / "extraction-output", wait=self._wait)
        return self.finish(draft, key, settings["model"], recording)

    def finish(self, draft, key=None, model=None, recording=None):
        """What runs on every new or rebuilt draft: the legislator name check, the audio check when the recording
        is attached, then the second check."""
        self.check_names(draft)
        if recording:
            self.check_audio(draft, recording, key, model)
        return self.second_check(draft, key, model)

    def check_audio(self, draft, recording, key=None, model=None):
        """The flag-only audio check (audio_check.py). The draft stands on its own if it can't run."""
        key, model = key or self.keys.get(), model or self.settings().get("model") or gemini.DEFAULT_MODEL
        if not key:
            return draft
        try:
            summary = audio_check.run(draft, recording, key, model, self.client,
                                      cache_dir=self.store.directory / "extraction-output", wait=self._wait)
        except InputError:
            raise InputError("Drafting stopped because the app is closing. Retry this hearing; the saved AI "
                             "replies are reused, so finished parts aren't charged again.") from None
        except Exception as error:  # noqa: BLE001 - an extra check; the draft is still usable.
            logging.exception("The audio check failed")
            draft["audio_check"] = {"model": model, "failed_testimonies": 1, "error": type(error).__name__}
            add_issue(draft, "The audio check didn't run, so names and figures weren't heard in the recording. "
                             "Listen to any point with a number, and check the names.")
            return draft
        # Whatever wasn't heard is said, so a quiet draft isn't taken for a checked one.
        if summary["items"] == 0 and summary["skipped"]:
            add_issue(draft, "The audio check couldn't cut clips from this recording (only MP3 and WAV can be "
                             "cut), so names and figures weren't heard. Listen to any point with a number.")
        elif summary["failed_testimonies"] or summary["skipped"] or summary["not_heard"]:
            add_issue(draft, "The audio check didn't hear every name and figure "
                             f"({summary['failed_testimonies'] + summary['skipped'] + summary['not_heard']} not "
                             "heard). Listen to any point with a number in the later testimonies, or click Rebuild "
                             "with the latest fixes before reviewing to try again.")
        return draft

    def check_names(self, draft):
        """Flags legislators' names not on the Legislature's member list, and keeps the committee's members for
        the review page. Only when the hearing page loaded, since it names the committee; skipped if the lists
        can't be loaded."""
        committee = (draft.get("extraction_provenance") or {}).get("committee")
        if not committee:
            return draft
        try:
            found = self.fetch_roster(committee, self.store.directory / "legislators.json")
        except InputError as error:
            logging.warning("The legislator name check didn't run: %s", error)
            return draft
        sponsors = [row.get("sponsor") for row in (draft.get("extraction_provenance") or {}).get("agenda") or []
                    if isinstance(row, dict)]
        draft["roster"] = {"committee": found["committee"], "legislators": len(found["legislators"]),
                           "fetched_utc": found["fetched_utc"], "flagged": roster.check(draft, found),
                           # Shadow mode: marked, not cleared, until her decisions show the rule is certain.
                           "would_clear": roster.shadow_confirm(draft, found["legislators"], sponsors)}
        return draft

    def second_check(self, draft, key=None, model=None):
        """The flag-only second check. The draft stands on its own if the check can't run."""
        key, model = key or self.keys.get(), model or self.settings().get("model") or gemini.DEFAULT_MODEL

        def not_run(reason):  # Recorded as failed for every testimony, so Rebuild can try again.
            draft["second_check"] = {"model": model, "units": len(draft["units"]), "flags": 0, "missed": [],
                                     "failed": [{"unit": unit["id"], "error": reason} for unit in draft["units"]]}
        if not key:
            not_run("no Gemini key is saved")
            add_issue(draft, "The second check didn't run because no Gemini key is saved. Read the arguments "
                             "with extra care.")
            return draft
        try:
            summary = second_check.run(draft, key, model, self.client,
                                       cache_dir=self.store.directory / "extraction-output", wait=self._wait)
        except InputError:
            raise InputError("Drafting stopped because the app is closing. Retry this hearing; the saved AI "
                             "replies are reused, so finished parts aren't charged again.") from None
        except Exception as error:  # noqa: BLE001 - the draft is still usable.
            logging.exception("The second check failed")
            not_run(type(error).__name__)
            add_issue(draft, f"The second check didn't run ({type(error).__name__}). Read the arguments with "
                             "extra care.")
            return draft
        if summary["failed"]:
            # Named by testifier with their times, which stay right if testimonies are moved or removed.
            units = {unit["id"]: unit for unit in draft["units"]}
            failed = [units[row["unit"]] for row in summary["failed"]]
            who = f"{len(failed)} testimonies" if len(failed) > 4 else "the testimony of " + "; ".join(
                (" and ".join(p["name"].get("value") or "an unnamed testifier" for p in unit["testifiers"])
                 or "an unnamed testifier") + f" on {unit['bill'].get('value') or 'an unknown bill'}" for unit in failed)
            add_issue(draft, f"The second check couldn't check {who} "
                             f"({summary['failed'][0]['error']}). Read those arguments with extra care, or "
                             "click Rebuild with the latest fixes before reviewing to try the check again.",
                      [unit["segment_ids"][0] for unit in failed if unit["segment_ids"]])
        return draft

    def _wait(self, seconds):
        if self.cancel_event.wait(seconds):
            raise InputError("Drafting stopped because the app is closing. Retry this hearing; "
                             "nothing was charged for the unfinished attempt.")
