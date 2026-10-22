"""A flag-only audio check: Gemini listens to short clips of the recording for items she would approve unheard.

Sending short clips of public-hearing audio to Gemini was approved by the project owner. The transcript is
machine-made and can mishear names and numbers, and no text check can catch that. So for unflagged items with a
figure, and unflagged testifier names, the seconds they come from are cut from the recording (clips.py) and
Gemini is asked what is said there. Where it hears something different, the item gets a ⚠ quoting what it heard.
It never changes a value or approves anything, and finds nothing for a recording it can't cut (M4A, for now).
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import uuid

from . import clips, gemini
from .pipeline import InputError
from .rich_review import lead_flag
from .tls import context as tls_context

MAX_ITEMS = 150  # Clips per hearing: a 4-hour hearing has ~70 testimonies; 150 × 30 s of audio is a few cents.
WORKERS = 4
LEAD, TAIL = 1.5, 1.5  # Seconds around an item's lines, for words cut at a line's edge.
NEAR, SPAN = 3.0, 30.0  # A clip holds lines no more than 3 s apart, over at most 30 s.
REQUEST_BYTES = 10 * 1024 * 1024  # Audio per request: inline requests must stay under 20 MB once base64-encoded.
VERDICTS = {"same", "different", "cant_tell"}

RULES = """You check hearing notes against the original audio. The transcript was made by speech recognition
and can mishear names and numbers. For each numbered clip below, listen and compare what is SAID with the
draft item. Only names and numbers matter: a person's name, an organization, an amount, a percentage, a
count, a date or a bill number.

Answer "same" if the speaker says the same names and numbers as the draft item, "different" if you clearly
hear a different name or number, or "cant_tell" if the audio is unclear or the words aren't in the clip.
Never guess. For "different", copy the words you hear for that name or number in "heard".

Reply with JSON only:
{"items": [{"clip": <number>, "verdict": "same|different|cant_tell", "heard": "<the words you hear, or empty>"}]}"""


def is_check(prompt: str) -> bool:
    return prompt.startswith(RULES[:50])


def _value(item):
    return item.get("value") if isinstance(item, dict) else None


def candidates(draft, limit=MAX_ITEMS) -> list[tuple[str, dict, str]]:
    """(path, field, kind) for unflagged, unapproved items worth hearing: testifier names, and statements with
    a figure. A flagged item is already sent to the audio."""
    from .extraction import figures
    out = []
    for u, unit in enumerate(draft.get("units") or []):
        def add(path, item, kind):
            if isinstance(item, dict) and item.get("value") and not item.get("flag") and item.get("review") != "APPROVED" \
                    and item.get("evidence"):
                out.append((path, item, kind))
        for j, person in enumerate(unit.get("testifiers") or []):
            add(f"/units/{u}/testifiers/{j}/name", person.get("name"), "Testifier's name")
        for k, argument in enumerate(unit.get("arguments") or []):
            if figures(_value(argument.get("text")) or ""):
                add(f"/units/{u}/arguments/{k}/text", argument.get("text"), "Argument")
        for q, question in enumerate(unit.get("questions") or []):
            for r, response in enumerate(question.get("responses") or []):
                if figures(_value(response.get("text")) or ""):
                    add(f"/units/{u}/questions/{q}/responses/{r}/text", response.get("text"), "Answer")
    return out if limit is None else out[:limit]


def _window(draft, item, kind):
    """(start, end, transcript) of the clip for an item: the evidence line holding its figure or name, with the
    evidence lines right beside it. Evidence can cite lines minutes apart; a clip of the first 40 s would then
    hold the wrong words."""
    from .extraction import figures
    times = {s["id"]: s for s in draft["segments"]}
    lines = sorted((times[i] for i in item.get("evidence") or [] if i in times
                    and isinstance(times[i].get("start"), (int, float)) and isinstance(times[i].get("end"), (int, float))),
                   key=lambda s: s["start"])
    if not lines:
        return None
    if kind == "Testifier's name":
        surname = (str(item.get("value") or "").split() or [""])[-1].lower()
        anchor = next((n for n, s in enumerate(lines) if surname and surname in s["text"].lower()), 0)
    else:
        wanted = {f[1] for f in figures(str(item.get("value") or ""))}
        anchor = next((n for n, s in enumerate(lines) if wanted & {f[1] for f in figures(s["text"], small=True)}), 0)
    first = last = anchor
    while first > 0 and lines[first]["start"] - lines[first - 1]["end"] <= NEAR \
            and lines[last]["end"] - lines[first - 1]["start"] <= SPAN:
        first -= 1
    while last + 1 < len(lines) and lines[last + 1]["start"] - lines[last]["end"] <= NEAR \
            and lines[last + 1]["end"] - lines[first]["start"] <= SPAN:
        last += 1
    kept = lines[first:last + 1]
    return kept[0]["start"] - LEAD, kept[-1]["end"] + TAIL, " ".join(s["text"] for s in kept)


def _ask(client, parts, digest, model, key, cache_dir, wait):
    path = cache_dir / f"{digest}.json" if cache_dir is not None else None
    if path is not None and path.exists():
        try:
            return json.loads(gemini.response_text(json.loads(path.read_text(encoding="utf-8")))[0])
        except (OSError, ValueError, TypeError, AttributeError, LookupError):
            pass
    if wait is not None:
        wait(0)  # The app is closing: don't start another paid call.
    payload, _ = client.call_gemini(parts[0]["text"], model, key, max_output=4096, thinking="low", wait=wait,
                                    log=logging.info, context=tls_context(), parts=parts)
    reply = json.loads(gemini.response_text(payload)[0])
    if path is not None:
        temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            logging.exception("Could not save an audio-check reply; a retry will ask again")
        finally:
            if temporary.exists():
                temporary.unlink()
    return reply


def run(draft, recording, key, model, client=gemini, cache_dir=None, wait=None) -> dict:
    """Adds ⚠ flags where Gemini hears a different name or number, and draft["audio_check"]. Returns it."""
    wanted = candidates(draft, None)
    chosen = wanted[:MAX_ITEMS]
    grouped, skipped = {}, 0
    try:
        for path, item, kind in chosen:
            window = _window(draft, item, kind)
            cut = clips.clip(recording, window[0], window[1]) if window else None
            if cut is None:
                skipped += 1
                continue
            grouped.setdefault(path.split("/")[2], []).append((path, item, kind, window, cut))
    finally:
        clips.forget()
    batches = []  # One request per testimony, split if its clips are large (WAV is ~10x the size of MP3).
    for clipped in grouped.values():
        batch, size = [], 0
        for entry in clipped:
            if batch and size + len(entry[4][0]) > REQUEST_BYTES:
                batches.append(batch)
                batch, size = [], 0
            batch.append(entry)
            size += len(entry[4][0])
        batches.append(batch)

    def one(batch):
        # Each clip right after its own label, so which audio is which can't be mixed up.
        parts, digest = [{"text": RULES}], hashlib.sha256(f"{model}\naudio-check\n{RULES}".encode("utf-8"))
        for n, (path, item, kind, window, (audio, mime)) in enumerate(batch, 1):
            label = (f"Clip {n} ({kind}): the draft says \"{item['value']}\". The transcript of these seconds says: "
                     f"\"{window[2]}\".")
            parts += [{"text": label}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode("ascii")}}]
            digest.update(label.encode("utf-8") + hashlib.sha256(audio).digest())
        try:
            return batch, _ask(client, parts, digest.hexdigest(), model, key, cache_dir, wait)
        except InputError:
            raise  # The app is closing.
        except Exception as error:  # noqa: BLE001 - one testimony's failure leaves the rest checked.
            logging.warning("The audio check couldn't check one testimony: %s", error)
            return batch, None

    pool = ThreadPoolExecutor(WORKERS)
    try:
        replies = list(pool.map(one, batches))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    flagged = failed = 0
    for batch, reply in replies:
        if not isinstance(reply, dict) or not isinstance(reply.get("items"), list):
            failed += 1
            continue
        for found in reply["items"]:
            if not isinstance(found, dict) or found.get("verdict") != "different":
                continue
            number, heard = found.get("clip"), str(found.get("heard") or "").strip()[:120]
            if not isinstance(number, int) or not 1 <= number <= len(batch) or not heard:
                continue
            item = batch[number - 1][1]
            item["flag"] = lead_flag(f"Audio check: Gemini hears “{heard}” in the recording here. Listen to "
                                     "confirm.", item.get("flag"))
            flagged += 1
    summary = {"model": model, "items": len(chosen) - skipped, "skipped": skipped, "flags": flagged,
               "failed_testimonies": failed, "not_heard": len(wanted) - len(chosen)}
    draft["audio_check"] = summary
    return summary
