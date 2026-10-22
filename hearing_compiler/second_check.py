"""A flag-only second Gemini check of a draft, one testimony at a time. Public hearing text only.

It never rewrites, approves or deletes anything. An item that says more, less or something other than the
transcript gets a ⚠ flag that quotes what was actually said, and points the draft may have left out are listed
beside their testimony. Code checks that every quote really is in the transcript lines; unquoted findings are
dropped. It reads the same transcript, so it can't catch transcription mistakes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import uuid

from . import gemini
from .pipeline import InputError
from .rich_review import agenda_titles, lead_flag
from .tls import context as tls_context

BEFORE, AFTER = 20, 6  # Transcript lines around a testimony: the chair's introduction comes before it.
WORKERS = 4
VERDICTS = {"overstated", "not_said", "wrong_attribution", "changed", "cant_tell"}

RULES = """You check a draft of hearing notes against the transcript lines it came from. You never rewrite
the notes. You only report items that need a person to listen again.

For each draft item, compare it with what was actually said in the transcript lines below. Report an
item ONLY if it is one of these:
- "overstated": stronger or more general than what was said. Examples: a hedge dropped ("may", "can",
  "as many as", "likely"), one case or story turned into a pattern ("cases", "frequently", "has caused"),
  an attempt or risk turned into a completed act ("attempted to harm" -> "harm", "risk of having to
  register" -> "mandatory registration"), a possibility turned into a certainty, "progress" -> "fully
  recover", "sent to jail" -> "lifetime incarceration".
- "not_said": it states a fact, number, name, place or claim that these lines don't contain.
- "wrong_attribution": it gives a point to the wrong person, or to the wrong bill.
- "changed": it changes a number, amount, date or meaning in another way ("over a million" ->
  "hundreds of thousands").
- "cant_tell": the transcript lines are too garbled or broken to check this item. Never treat a garbled
  passage as support.
Don't report wording, style, shortening or reasonable summary. Don't report items that are fine.
Names and spellings: the transcript is machine-made and misspells names; report a name only if the
lines clearly give a different person. Bill numbers: the draft's numbers come from the official agenda,
so don't report a bill number that differs from a misheard number in the transcript.

Then list at most 3 material points this testimony made that NO draft item covers: a specific figure,
a personal account the speaker used as evidence, or a specific proposal or amendment. Skip greetings,
thanks and procedure.

For every reported item and omission, copy a quote of 5 to 25 words EXACTLY as it appears in the
transcript lines, showing what was actually said. Copy the words only, without the line number or time.

Reply with JSON only:
{"items": [{"id": "<draft item id>", "verdict": "overstated|not_said|wrong_attribution|changed|cant_tell",
            "reason": "<one short sentence>", "quote": "<exact transcript words>"}],
 "omissions": [{"point": "<one short sentence>", "quote": "<exact transcript words>"}]}
Use empty lists when nothing needs reporting."""


def is_check(prompt: str) -> bool:
    return prompt.startswith(RULES[:60])


def _clock(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "--:--:--"
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def _value(item):
    return item.get("value") if isinstance(item, dict) else None


def items(unit, u) -> list[tuple[str, str, str]]:
    """(field path, kind, text) for each statement in one testimony; the path doubles as the model's id."""
    out = [(f"/units/{u}/position", "Position", _value(unit.get("position")))]
    for j, person in enumerate(unit.get("testifiers") or []):
        name, org = _value(person.get("name")), _value(person.get("organization"))
        out.append((f"/units/{u}/testifiers/{j}/name", "Testifier", f"{name} ({org})" if org else name))
    for k, argument in enumerate(unit.get("arguments") or []):
        out.append((f"/units/{u}/arguments/{k}/text", "Argument", _value(argument.get("text"))))
    for q, question in enumerate(unit.get("questions") or []):
        out.append((f"/units/{u}/questions/{q}/question", "Question",
                    f"{_value(question['asker'].get('name'))}: {_value(question.get('question'))}"))
        for r, response in enumerate(question.get("responses") or []):
            out.append((f"/units/{u}/questions/{q}/responses/{r}/text", "Answer",
                        f"{_value(response['respondent'].get('name'))}: {_value(response.get('text'))}"))
    return [(path, kind, text) for path, kind, text in out if text]


def window(draft, unit) -> list[dict]:
    order = {s["id"]: n for n, s in enumerate(draft["segments"])}
    spots = [order[s] for s in unit.get("segment_ids") or [] if s in order]
    if not spots:
        return []
    return draft["segments"][max(0, min(spots) - BEFORE):min(len(draft["segments"]), max(spots) + AFTER + 1)]


def prompt(draft, unit, u) -> str:
    lines = "\n".join(f"{n} [{_clock(s.get('start'))}] {s['text']}" for n, s in enumerate(window(draft, unit), 1))
    drafted = "\n".join(f"{path} | {kind} | {text}" for path, kind, text in items(unit, u))
    return (f"{RULES}\n\nTestimony on {_value(unit.get('bill')) or 'an unknown bill'}.\n\n"
            f"DRAFT ITEMS (id | kind | text):\n{drafted}\n\nTRANSCRIPT LINES:\n{lines}\n")


def locate(quote, lines) -> list[str]:
    """The segment where an exact (normalized) quote starts, or nothing if it isn't in these lines."""
    wanted = gemini._norm(quote or "")
    if len(wanted.split()) < 3:
        return []
    texts = [gemini._norm(s["text"]) for s in lines]
    joined, starts = "", []
    for text in texts:
        starts.append(len(joined))
        joined += text + " "
    at = f" {joined}".find(f" {wanted}")
    if at < 0:
        return []
    return [lines[max(n for n, start in enumerate(starts) if start <= at)]["id"]]


def _clean(reply):
    """The reply's findings as lists of string-valued rows, or None when it isn't that shape at all."""
    if not isinstance(reply, dict):
        return None
    found, gaps = reply.get("items", []), reply.get("omissions", [])
    if not isinstance(found, list) or not isinstance(gaps, list):
        return None

    def rows(listed, keys):
        return [{k: row[k] for k in keys} for row in listed
                if isinstance(row, dict) and all(isinstance(row.get(k), str) for k in keys)]
    return {"items": rows(found, ("id", "verdict", "reason", "quote")), "omissions": rows(gaps, ("point", "quote"))}


def _reply(payload):
    try:
        return _clean(json.loads(gemini.response_text(payload)[0]))
    except (ValueError, TypeError, AttributeError, LookupError):
        return None


def _saved(path):
    try:
        return _reply(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _ask(client, text, model, key, cache_dir, wait):
    path = cache_dir / (hashlib.sha256(f"{model}\nsecond-check\n{text}".encode("utf-8")).hexdigest() + ".json") \
        if cache_dir is not None else None
    reply = _saved(path) if path is not None else None
    if reply is not None:
        return reply
    payload, _ = client.call_gemini(text, model, key, max_output=8192, thinking="low", wait=wait,
                                    log=logging.info, context=tls_context())
    reply = _reply(payload)
    if reply is None:  # Not saved, so a retry asks again.
        raise ValueError("its reply wasn't in the expected form")
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:  # Saved like drafting replies, so a rebuild or retry doesn't pay again.
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            logging.exception("Could not save a second-check reply; a retry will ask again")
        finally:
            if temporary.exists():
                temporary.unlink()
    return reply


def _misread_bill(item_text, quote, agenda) -> bool:
    """A flag that only trades an agenda bill number for a misheard one would make the note wrong.

    item_text includes the testimony's bill, so a Position or Testifier flag is covered too."""
    if not agenda:
        return False
    said = set(gemini.mentioned_bills([{"n": 1, "text": quote}], None))
    drafted = set(gemini.mentioned_bills([{"n": 1, "text": item_text}], None))
    return bool(said - set(agenda)) and bool(drafted & set(agenda))


def run(draft, key, model, client=gemini, cache_dir=None, wait=None) -> dict:
    """Adds ⚠ flags and draft["second_check"]; returns that summary. Values and reviews never change.

    wait is the app's cancellable sleep: it raises InputError once the app is closing, so no further
    testimony is sent."""
    from .pipeline import fields
    available = dict(fields(draft))
    agenda = agenda_titles(draft)

    def one(u):
        if wait is not None:
            wait(0)  # Closing: stop before paying for this testimony.
        unit = draft["units"][u]
        if not window(draft, unit):
            return u, None, "it has no transcript lines"  # Nothing a quote could be checked against.
        try:
            return u, _ask(client, prompt(draft, unit, u), model, key, cache_dir, wait), None
        except InputError:
            raise
        except (gemini.GeminiError, ValueError, TypeError) as error:
            logging.warning("The second check couldn't check testimony %s: %s", u + 1, error)
            return u, None, str(error)[:200]

    pool = ThreadPoolExecutor(WORKERS)
    try:
        replies = list(pool.map(one, range(len(draft["units"]))))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)  # After a cancel, queued testimonies are never sent.
    flagged, missed, failed = 0, [], []
    for u, reply, error in replies:
        unit = draft["units"][u]
        if reply is None:
            failed.append({"unit": unit["id"], "error": error})
            continue
        lines = window(draft, unit)
        bill = _value(unit.get("bill")) or ""
        texts = {path: text for path, _, text in items(unit, u)}
        for found in reply["items"]:
            quote, reason = found["quote"][:300], found["reason"].strip()[:200]
            item = available.get(found["id"])
            if (found["verdict"] not in VERDICTS or found["id"] not in texts or item is None or not reason
                    or not locate(quote, lines) or _misread_bill(f"{texts[found['id']]} {bill}", quote, agenda)):
                continue
            item["flag"] = lead_flag(f"Second check: {reason} Transcript: “{quote}”", item.get("flag"))
            flagged += 1
        for gap in reply["omissions"][:3]:
            quote, point = gap["quote"][:300], gap["point"].strip()[:200]
            evidence = locate(quote, lines)
            if point and evidence:
                # Keyed by the testimony's id, which stays put if testimonies are added or removed.
                missed.append({"unit": unit["id"], "point": point, "quote": quote, "evidence": evidence})
    summary = {"model": model, "units": len(draft["units"]), "flags": flagged, "missed": missed, "failed": failed}
    draft["second_check"] = summary
    return summary
