"""Gemini whole-hearing drafting with code-checked citations.

Shared by the app and the Colab trial script, so it uses only the standard
library and no package-relative imports. The model must quote transcript words
verbatim; code, not the model, turns those quotes into timestamps. Every
uncertain or unlocatable item stays visibly flagged for human review.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}"
ENDPOINT = API + ":generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
POSITIONS = ("Support", "Oppose", "Unclear")
RETRY_WAITS = (15, 30, 60, 120, 240)  # Busy/rate-limit responses are usually temporary.
MAX_RETRY_SECONDS = 1800  # Give up after 30 minutes of retrying rather than stall the queue.
MAX_WORDS = 30000  # About 3.5 hours of speech in one call; a longer transcript is drafted in parts.


class GeminiError(Exception):
    pass


INSTRUCTIONS = """You are drafting notes on a public Massachusetts legislative committee hearing for a
legislative staffer. Her goal is to save listening time; she must be able to trust anything
you do not flag. Work only from the transcript{agenda_scope} below.

The transcript came from automatic speech recognition (Whisper). It has no reliable speaker labels
and can mishear names, numbers and words. Each line is: cue number, [start time or text line], text.

{agenda_rules}

Produce one entry per bill per testimony, in chronological order. A testimony is one person or a
panel presenting together. If a testimony covers several bills, make one entry per bill and keep
each argument with the bill it was about. Legislators who testify are testifiers. Questions and
comments from committee members after a testimony belong to that entry. Chair procedure (opening
remarks, calling witnesses, breaks, closing) goes in "procedural".

Rules:
1. Every field and list item has "quote": 5 to 20 consecutive words copied EXACTLY from the
   transcript that support it (keep the transcript's spelling, even if misheard). The quote is how
   the reader finds the audio, so it must be verbatim. Also give your best "cues" numbers.
2. If anything is uncertain, put a short reason in "check"; otherwise "check" is null. Flag:
   possibly misheard names, organizations or numbers (most spoken names are uncertain unless spelled
   out or repeated); unclear speaker; inferred position; bill not stated{agenda_flag}; garbled
   passages. Never present a guess as confirmed, and do not flag clear statements.
3. Names come only from the transcript. {name_rule}If the transcript only gives a surname, use the
   surname.
4. Be complete. One bullet per distinct argument or material point: purpose, mechanism, legal
   conditions, case names, examples, numbers, who is affected, and requests to the committee. Do not
   merge separate points. Paraphrase faithfully and concisely. Do not invent anything.
5. "questions" lists every question or comment from committee members to the testifier, with the
   response. If the chair asked for questions and there were none, leave "questions" empty and say
   so in "questions_note".
6. position is one of Support, Oppose, Unclear (use Unclear for neutral, mixed or unstated).
7. Keep each entry as ONE object containing all of its fields. Never split an entry in two.

Return only JSON with exactly this shape:
{{
  "entries": [
    {{
      "start_cue": int, "end_cue": int,
      "bill": "H.1911" or null, "bill_title": string or null,
      "bill_quote": string, "bill_cues": [int], "bill_check": string or null,
      "testifiers": [{{"name": string, "role": string or null, "organization": string or null,
                      "quote": string, "cues": [int], "check": string or null}}],
      "position": "Support", "position_quote": string, "position_cues": [int], "position_check": string or null,
      "arguments": [{{"text": string, "quote": string, "cues": [int], "check": string or null}}],
      "questions": [{{"asker": string, "question": string, "respondent": string, "response": string or null,
                     "quote": string, "cues": [int], "check": string or null}}],
      "questions_note": string or null
    }}
  ],
  "procedural": [{{"start_cue": int, "end_cue": int, "summary": string}}]
}}
"""

AGENDA_RULES = """The official agenda lists bills that could be heard, with exact numbers and titles. Use it to
normalize spoken bill numbers ("House Bill 1885", "H-1885", "House 1885" -> "H.1885"). The agenda
does NOT show who testified, their positions, or which bills were actually discussed; never infer
those from sponsorship."""

NO_AGENDA_RULES = """No official agenda is supplied. Write spoken bill numbers as H.#### or S.#### ("House Bill 1885",
"H-1885", "House 1885" -> "H.1885"), give a title only if it is spoken, and flag any number you are
not sure you heard correctly."""


def clock(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "--:--:--"
    whole = int(seconds)
    return f"{whole // 3600:02}:{whole // 60 % 60:02}:{whole % 60:02}"


def build_prompt(cues: list[dict], hearing: dict, bills: list[dict] | None = None, of: int | None = None) -> str:
    """bills: optional official agenda rows with bill, title and sponsor. of: the whole transcript's line count
    when cues are only part of it (a long hearing drafted in parts); a whole transcript's prompt is unchanged."""
    if bills:
        rules = INSTRUCTIONS.format(
            agenda_scope=" and agenda", agenda_rules=AGENDA_RULES, agenda_flag=" or not on the agenda",
            name_rule="Never fill in a name, title or district from the agenda's sponsor list. ")
        agenda = "Official agenda:\n" + "\n".join(
            f"{b['bill']} | {b['title']} | sponsor: {b['sponsor']}" for b in bills) + "\n\n"
    else:
        rules = INSTRUCTIONS.format(agenda_scope="", agenda_rules=NO_AGENDA_RULES, agenda_flag="", name_rule="")
        agenda = ""
    lines = "\n".join(
        f"{c['n']} [{clock(c['start']) if c.get('start') is not None else 'line ' + str(c.get('line') or '?')}] {c['text']}"
        for c in cues)
    part = "" if of is None else (
        f"This is only lines {cues[0]['n']} to {cues[-1]['n']} of a {of}-line transcript; the rest is drafted "
        "separately. A testimony may have begun before the first line or go on after the last one. Draft only "
        "what these lines contain, and leave out a testifier's name these lines don't give.\n\n")
    return (f"{rules}\nHearing: {hearing['committee']}, {hearing['date']}, {hearing['title']}\n\n"
            f"{agenda}{part}Transcript:\n{lines}\n")


def _post(url: str, body: bytes | None, key: str, timeout: int, context=None) -> dict:
    request = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
    # The packaged app passes an SSL context with the Mac's trusted roots.
    with urllib.request.urlopen(request, timeout=timeout, **({"context": context} if context else {})) as response:
        return json.loads(response.read().decode("utf-8"))


def check_key(key: str, model: str = DEFAULT_MODEL, context=None) -> dict:
    """A tokenless request that proves the key can use the model."""
    try:
        return _post(API.format(model=model), None, key, 20, context)
    except urllib.error.HTTPError as error:
        raise GeminiError(f"Gemini rejected the key or model (HTTP {error.code}). "
                          "Check that the key is copied completely and is enabled for the Gemini API.") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise GeminiError(f"Could not reach Gemini to check the key: {error}") from None


def _permanent(error) -> bool:
    """Certificate and DNS failures won't fix themselves by retrying."""
    reason = getattr(error, "reason", error)
    return isinstance(reason, (ssl.SSLCertVerificationError, socket.gaierror))


def call_gemini(prompt: str, model: str, key: str, max_output: int = 65536,
                thinking: str | None = "low", wait=None, log=None, context=None, parts=None) -> tuple[dict, float]:
    """wait(seconds) may raise to cancel; transient failures retry with backoff up to MAX_RETRY_SECONDS.
    parts: the request's parts (text and audio clips) instead of the prompt alone."""
    # Thinking tokens count against the output limit, so a long note needs a low level.
    wait, log = wait or time.sleep, log or (lambda message: print(message, flush=True))
    config = {"responseMimeType": "application/json", "maxOutputTokens": max_output}
    if thinking:
        config["thinkingConfig"] = {"thinkingLevel": thinking}
    body = json.dumps({"contents": [{"role": "user", "parts": parts or [{"text": prompt}]}],
                       "generationConfig": config}).encode("utf-8")
    deadline = time.monotonic() + MAX_RETRY_SECONDS
    for attempt, pause in enumerate(RETRY_WAITS + (None,), start=1):
        started = time.monotonic()
        try:
            return _post(ENDPOINT.format(model=model), body, key, 900, context), time.monotonic() - started
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:2000]
            if error.code == 400 and thinking and "think" in detail.lower():
                log("The model rejected the thinking setting; retrying with its default.")
                return call_gemini(prompt, model, key, max_output, None, wait, log, context, parts)
            if error.code == 400 and "output" in detail.lower() and "token" in detail.lower() and max_output > 32768:
                log("The model rejected the output limit; retrying with 32768 tokens.")
                return call_gemini(prompt, model, key, 32768, thinking, wait, log, context, parts)
            # Spend-cap and prepaid-credit stops don't clear by waiting, so they fail at once.
            if error.code == 402 or error.code == 429 and any(p in detail.lower() for p in (
                    "spending cap", "spend cap", "prepayment credits", "credits are depleted")):
                raise GeminiError("Gemini refused this key because its project reached its monthly spending cap "
                                  "or ran out of prepaid credit. Whoever manages the key can raise the cap or add "
                                  "credit in AI Studio; then retry.") from None
            if error.code == 429 and "quota" in detail.lower() and ("day" in detail.lower() or "billing" in detail.lower()):
                raise GeminiError("The Gemini key has used up its quota or spend cap. Raise the cap in AI Studio, "
                                  "or wait until it resets, then retry.") from None
            if error.code not in (429, 500, 502, 503, 504) or pause is None:
                raise GeminiError(f"Gemini returned HTTP {error.code}: {detail}") from None
            reason = f"HTTP {error.code} (busy or rate-limited)"
        except urllib.error.URLError as error:
            if _permanent(error):
                raise GeminiError(f"Could not connect securely to Gemini ({error.reason}). Check the internet "
                                  "connection; on a work network, IT may need to allow it.") from None
            if pause is None:
                raise GeminiError(f"Could not reach Gemini: {error.reason}") from None
            reason = f"connection problem ({error.reason})"
        except (OSError, http.client.HTTPException, ValueError) as error:
            # Dropped or truncated responses arrive as raw socket/HTTP errors, or as unreadable JSON.
            if pause is None:
                raise GeminiError(f"Gemini's connection kept failing: {type(error).__name__}") from None
            reason = f"connection problem ({type(error).__name__})"
        if time.monotonic() + pause > deadline:
            raise GeminiError(f"Gemini stayed unavailable for {MAX_RETRY_SECONDS // 60} minutes ({reason}). Retry later.")
        log(f"Attempt {attempt}: {reason}; waiting {pause} s before retrying.")
        wait(pause)


def response_text(payload: dict) -> tuple[str, str | None]:
    candidate = (payload.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought")), candidate.get("finishReason")


# No word boundary inside run-together numbers such as "H1852S1164", so letters and digits are checked instead.
BILL_MENTION = re.compile(
    r"(?<![A-Za-z])(H|S)(?:ouse|enate)?\.?\s*(?:Bill\s*)?(?:(?:No\.?|Number|#)\s*)?[-.]?\s*(\d{1,4})(?!\d)", re.I)


def mentioned_bills(cues: list[dict], agenda_numbers: set[str] | None = None) -> dict[str, list[int]]:
    """Bills spoken in the transcript in common forms; agenda bills only when an agenda is known."""
    found: dict[str, list[int]] = {}
    for cue in cues:
        for chamber, number in BILL_MENTION.findall(cue["text"]):
            bill = f"{chamber.upper()}.{int(number)}"
            if agenda_numbers is None and int(number) >= 10 or agenda_numbers and bill in agenda_numbers:
                found.setdefault(bill, []).append(cue["n"])
    return found


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def citable(entry: dict):
    """(label, holder, quote key, cues key, check key) for every citable part of an entry."""
    yield "bill", entry, "bill_quote", "bill_cues", "bill_check"
    for person in entry.get("testifiers") or []:
        yield "testifier", person, "quote", "cues", "check"
    yield "position", entry, "position_quote", "position_cues", "position_check"
    for argument in entry.get("arguments") or []:
        yield "argument", argument, "quote", "cues", "check"
    for question in entry.get("questions") or []:
        yield "question", question, "quote", "cues", "check"


def items(entry: dict):
    """(label, item) views with cues and check, for checking and rendering."""
    for label, holder, _, cues_key, check_key in citable(entry):
        yield label, {**holder, "cues": holder.get(cues_key) or [], "check": holder.get(check_key)}


STITCH = re.compile(r"\.{3,}|…")
PIECE_GAP = 2500  # Characters (about 400 words, a few minutes) a stitched quote's next piece may follow the last.
PART_FOUND = "part of the supporting quote wasn't found in the transcript; check it by listening"


def _first_cue(spans, position):
    return next(n for start, end, n in spans if end > position)


def _covering(spans, start, end):
    return [n for a, b, n in spans if a < end and b > start]


def _stitched(quote, text, spans, guess) -> list[int]:
    """Gemini sometimes joins nearby passages with "...". Each piece must be verbatim and follow the one before
    within a few minutes; the cues are those of every piece, so ▶ starts at the first."""
    pieces = [p for p in (_norm(piece) for piece in STITCH.split(quote)) if p]
    if sum(len(p.split()) for p in pieces) < 4:
        return []

    def whole_words(piece, start, stop):
        at = text.find(piece, start, stop)
        while at >= 0 and not ((at == 0 or text[at - 1] == " ")
                               and (at + len(piece) == len(text) or text[at + len(piece)] == " ")):
            at = text.find(piece, at + 1, stop)
        return at
    anchors, at = [], whole_words(pieces[0], 0, len(text))
    while at >= 0:
        anchors.append(at)
        at = whole_words(pieces[0], at + 1, len(text))
    for start in sorted(anchors, key=lambda h: abs(_first_cue(spans, h) - guess)):
        found, end = _covering(spans, start, start + len(pieces[0])), start + len(pieces[0])
        for piece in pieces[1:]:
            at = whole_words(piece, end, min(len(text), end + PIECE_GAP + len(piece)))
            if at < 0:
                break
            found, end = found + _covering(spans, at, at + len(piece)), at + len(piece)
        else:
            return sorted(set(found))
    return []


def locate(quote: str, text: str, spans: list[tuple[int, int, int]], near: list[int]) -> list[int]:
    """Cue numbers containing an exact (normalized) quote, nearest the model's own guess."""
    guess = near[0] if near else 0
    if STITCH.search(quote or "") and len(STITCH.split(quote)) > 1:
        return _stitched(quote, text, spans, guess)
    wanted = _norm(quote or "")
    if len(wanted.split()) < 4:
        return []
    hits = [m.start() for m in re.finditer(re.escape(wanted), text)]
    if not hits:
        return []
    start = min(hits, key=lambda h: abs(_first_cue(spans, h) - guess))
    return _covering(spans, start, start + len(wanted))


def locate_part(quote: str, text: str, spans: list[tuple[int, int, int]], near: list[int]) -> list[int]:
    """For a stitched quote that wasn't wholly found: where its first verbatim piece of 4 or more words is,
    so ▶ still starts in the right place."""
    for piece in STITCH.split(quote or "") if STITCH.search(quote or "") else []:
        found = locate(piece, text, spans, near)
        if found:
            return found
    return []


def _as_list(value) -> list:
    return value if isinstance(value, list) else [] if value is None else [value]


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _cue_list(value) -> list[int]:
    return [n for n in (_number(v) for v in _as_list(value)) if n is not None]


def _scalar(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


ITEM_TEXT = {"testifiers": "name", "arguments": "text", "questions": "question"}


def normalize(raw) -> dict:
    """Coerce model JSON into the documented shape so later steps never crash on odd output.
    Keys that are present stay present (fragment rejoining depends on which keys exist)."""
    if isinstance(raw, list):
        raw = {"entries": raw}
    if not isinstance(raw, dict):
        return {"entries": [], "procedural": []}
    entries = []
    for entry in _as_list(raw.get("entries")):
        if not isinstance(entry, dict):
            continue
        clean = {}
        for key, value in entry.items():
            if key in ITEM_TEXT:
                items = []
                for item in _as_list(value):
                    if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                        item = {ITEM_TEXT[key]: str(item)}
                    if isinstance(item, dict):
                        items.append({k: _cue_list(v) if k.endswith("cues") else _scalar(v) for k, v in item.items()})
                clean[key] = items
            elif key.endswith("_cues"):
                clean[key] = _cue_list(value)
            elif key in ("start_cue", "end_cue"):
                clean[key] = _number(value)
            else:
                clean[key] = _scalar(value)
        entries.append(clean)
    procedural = []
    for part in _as_list(raw.get("procedural")):
        if isinstance(part, dict):
            procedural.append({"start_cue": _number(part.get("start_cue")), "end_cue": _number(part.get("end_cue")),
                               "summary": _scalar(part.get("summary"))})
    return {"entries": entries, "procedural": procedural}


def merge_fragments(draft: dict) -> int:
    """Rejoin entries the model split in two: a bill-only half followed by a testimony half,
    or an arguments-only continuation of the previous entry. Content is never dropped."""
    merged, out = 0, []
    for entry in draft.get("entries") or []:
        previous = out[-1] if out else None
        if (previous is not None and "bill" not in entry and "testifiers" in entry
                and "testifiers" not in previous and "bill" in previous):
            previous.update(entry)
            merged += 1
        elif previous is not None and "bill" not in entry and "testifiers" not in entry:
            for key in ("arguments", "questions"):
                previous.setdefault(key, []).extend(entry.get(key) or [])
            if entry.get("questions_note") and not previous.get("questions_note"):
                previous["questions_note"] = entry["questions_note"]
            merged += 1
        else:
            out.append(entry)
    draft["entries"] = out
    return merged


def resolve(draft: dict, cues: list[dict]) -> dict:
    """Replace model cue numbers with cues found from verbatim quotes; flag unfound quotes."""
    parts, spans, position = [], [], 0
    for cue in cues:
        normalized = _norm(cue["text"])
        spans.append((position, position + len(normalized), cue["n"]))
        parts.append(normalized)
        position += len(normalized) + 1
    text = " ".join(parts)
    stats = {"quotes": 0, "found": 0, "moved": 0}
    for entry in draft.get("entries") or []:
        offsets = []
        for label, holder, quote_key, cues_key, check_key in citable(entry):
            if label == "bill" and not entry.get("bill") and not entry.get("bill_quote"):
                continue
            model = [n for n in holder.get(cues_key) or [] if isinstance(n, int) and not isinstance(n, bool)]
            stats["quotes"] += 1
            found = locate(holder.get(quote_key), text, spans, model)
            holder["model_" + cues_key] = model
            if found:
                stats["found"] += 1
                stats["moved"] += set(found) != set(model)
                if model:
                    offsets.append(found[0] - model[0])
                holder[cues_key] = found
            elif locate_part(holder.get(quote_key), text, spans, model):
                holder[cues_key] = locate_part(holder.get(quote_key), text, spans, model)
                holder[check_key] = f"{holder[check_key]}; {PART_FOUND}" if holder.get(check_key) else PART_FOUND
            else:
                note = "supporting quote not found in the transcript; locate it by listening"
                holder[check_key] = f"{holder[check_key]}; {note}" if holder.get(check_key) else note
        if offsets and all(isinstance(entry.get(k), int) for k in ("start_cue", "end_cue")):
            shift = sorted(offsets)[len(offsets) // 2]
            entry["model_range"] = [entry["start_cue"], entry["end_cue"]]
            # Within these cues, which needn't start at line 1 (a part or a redraft keeps the hearing's numbers).
            entry["start_cue"] = max(cues[0]["n"], entry["start_cue"] + shift)
            entry["end_cue"] = min(cues[-1]["n"], entry["end_cue"] + shift)
    return stats


def check(draft: dict, cues: list[dict], agenda_bills: list[str] | None = None) -> dict:
    """Mechanical checks only. Passing them does not make a claim true."""
    total = len(cues)
    by_number = {c["n"]: c for c in cues}
    agenda_numbers = set(agenda_bills) if agenda_bills else None
    entries = draft.get("entries") or []
    problems, flags, covered = [], 0, set()
    for index, entry in enumerate(entries):
        where = f"entry {index + 1} ({entry.get('bill') or 'no bill'})"
        start, end = entry.get("start_cue"), entry.get("end_cue")
        if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= total):
            problems.append(f"{where}: invalid cue range {start}-{end}")
            start = end = None
        else:
            covered.update(range(start, end + 1))
        if entry.get("position") not in POSITIONS:
            problems.append(f"{where}: position {entry.get('position')!r} is not one of {POSITIONS}")
        if (agenda_numbers and entry.get("bill") and entry["bill"] not in agenda_numbers
                and not entry.get("bill_check")):
            problems.append(f"{where}: bill {entry['bill']} is not on the agenda and is not flagged")
        if not entry.get("bill") and not entry.get("bill_check"):
            problems.append(f"{where}: no bill and no check flag")
        for label, item in items(entry):
            if item.get("check"):
                flags += 1
            cited = item.get("cues") or []
            if not cited and not item.get("check") and not (label == "bill" and not entry.get("bill")):
                problems.append(f"{where}: {label} has no cited cues and no check flag")
            bad = [n for n in cited if not isinstance(n, int) or n not in by_number]
            if bad:
                problems.append(f"{where}: {label} cites missing cues {bad}")
            elif start is not None and any(n < start or n > end for n in cited):
                problems.append(f"{where}: {label} cites cues outside its range {start}-{end}")
            text = " ".join(str(item.get(k) or "") for k in ("text", "question", "response"))
            source = _norm(" ".join(by_number[n]["text"] for n in cited if n in by_number))
            for quote in re.findall(r"[\"“]([^\"”]{12,})[\"”]", text):
                if _norm(quote) not in source:
                    problems.append(f"{where}: {label} quote not found in its cited cues: {quote[:80]!r}")
    for part in draft.get("procedural") or []:
        start, end = part.get("start_cue"), part.get("end_cue")
        if isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= total:
            covered.update(range(start, end + 1))
    gaps, run = [], []
    for n in range(1, total + 1):
        if n not in covered:
            run.append(n)
        if run and (n in covered or n == total):
            first, last = by_number[run[0]], by_number[run[-1]]
            timed = isinstance(first.get("start"), (int, float)) and isinstance(last.get("end"), (int, float))
            seconds = last["end"] - first["start"] if timed else None
            if (seconds is not None and seconds >= 20) or (seconds is None and len(run) >= 8):
                gaps.append({"cues": f"{run[0]}-{run[-1]}", "from": clock(first.get("start")),
                             "seconds": round(seconds) if seconds is not None else None,
                             "starts": first["text"][:100]})
            run = []
    spoken = mentioned_bills(cues, agenda_numbers)
    drafted = {e.get("bill") for e in entries}
    return {
        "entries": len(entries),
        "bills_in_draft": sorted(b for b in drafted if b),
        "entries_without_bill": sum(1 for e in entries if not e.get("bill")),
        "spoken_agenda_bills_missing_from_draft": {b: n[:5] for b, n in sorted(spoken.items()) if b not in drafted},
        "testifiers": sum(len(e.get("testifiers") or []) for e in entries),
        "arguments": sum(len(e.get("arguments") or []) for e in entries),
        "questions": sum(len(e.get("questions") or []) for e in entries),
        "flags": flags,
        "uncovered_spans_20s_or_more": gaps,
        "problems": problems,
        "limits": "Mechanical citation/coverage checks; meaning still needs human review against the audio.",
    }
