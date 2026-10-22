"""Strict, source-preserving SRT/WebVTT intake. Cue labels are not identities."""
from __future__ import annotations

import copy
import hashlib
import re

from .pipeline import InputError, fingerprint, validate
from .text_import import MAX_TEXT_BYTES, normalize_text

SUBTITLE_FORMATS = {"srt-utf8": ".srt", "webvtt-utf8": ".vtt"}


def _clock(value: str, kind: str) -> float:
    pattern = (r"(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})" if kind == "srt"
               else r"(?:(\d{2,}):)?([0-5]\d):([0-5]\d)\.(\d{3})")
    match = re.fullmatch(pattern, value)
    if not match:
        raise InputError(f"Invalid {kind.upper()} timestamp: {value}.")
    hours, minutes, seconds, milliseconds = match.groups()
    return (int(hours or 0) * 3600000 + int(minutes) * 60000
            + int(seconds) * 1000 + int(milliseconds)) / 1000


def parse_cues(text: str, kind: str) -> list[dict]:
    """Reparse retained originals to detect forged clocks, omitted cues or edits."""
    if kind not in ("srt", "vtt") or not isinstance(text, str) or not text.lstrip("\ufeff").strip():
        raise InputError("Choose a nonempty UTF-8 SRT or WebVTT transcript.")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError as error:
        raise InputError("Export subtitles as valid UTF-8.") from error
    if size > MAX_TEXT_BYTES:
        raise InputError("Use a subtitle transcript no larger than 4 MB.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise InputError("The subtitle transcript contains binary/control characters.")
    # Offsets refer to the exact original, including BOM and CRLF.
    lines = []
    cursor = 1 if text.startswith("\ufeff") else 0
    for line in text[cursor:].splitlines(keepends=True):
        body = line.rstrip("\r\n")
        lines.append((body, cursor, cursor + len(body)))
        cursor += len(line)
    index = 0
    if kind == "vtt":
        if not lines or not re.fullmatch(r"WEBVTT(?:[ \t][^\r\n]*)?", lines[0][0]):
            raise InputError("WebVTT must begin with WEBVTT.")
        index = 1
        while index < len(lines) and lines[index][0].strip():
            if "-->" in lines[index][0] or "X-TIMESTAMP-MAP" in lines[index][0].upper():
                raise InputError("Unsupported WebVTT header or external timestamp mapping.")
            index += 1
    segments = []
    previous = -1
    while index < len(lines):
        if not lines[index][0].strip():
            index += 1
            continue
        block_start = index
        while index < len(lines) and lines[index][0].strip():
            index += 1
        block = lines[block_start:index]
        if kind == "vtt" and re.match(r"^(?:NOTE(?:[ \t]|$)|STYLE$|REGION$)", block[0][0]):
            if any("-->" in line[0] for line in block):
                raise InputError("Cue timing cannot be hidden inside WebVTT metadata.")
            continue
        identifier = None
        timing_index = 0
        if "-->" not in block[0][0]:
            identifier = block[0][0]
            if kind == "srt" and not re.fullmatch(r"\d+", identifier):
                raise InputError("SRT cue identifiers must be numeric.")
            timing_index = 1
        if timing_index >= len(block):
            raise InputError("A subtitle cue is missing its timing line.")
        timing = block[timing_index][0]
        match = re.fullmatch(r"(\S+)[ \t]+-->[ \t]+(\S+)(?:[ \t]+(.+))?", timing)
        if not match or (kind == "srt" and match.group(3)):
            raise InputError("Use a valid subtitle timing line; SRT cue settings are unsupported.")
        start, end = _clock(match.group(1), kind), _clock(match.group(2), kind)
        if end < start or start < previous:
            raise InputError("Subtitle cues need nonnegative durations and chronological start times.")
        previous = start
        payload = block[timing_index + 1:]
        if any(re.match(r"^\d[\d:.,]*[ \t]+-->[ \t]+\d", line[0]) for line in payload):
            raise InputError("A subtitle timing line appears inside a cue body. Check for a missing blank cue separator.")
        # Whisper can emit blank cues. They carry no text and remain in source_text.
        if not payload or not text[payload[0][1]:payload[-1][2]].strip():
            continue
        begin, finish = payload[0][1], payload[-1][2]
        content = text[begin:finish]
        if len(content) > 50000 or len(segments) >= 10000:
            raise InputError("Use 1–10,000 nonempty cues with at most 50,000 characters each.")
        ordinal = len(segments) + 1
        segments.append({
            "id": f"{kind}{ordinal:04}", "start": start, "end": end,
            "text": content, "speaker": f"Unattributed subtitle cue {ordinal}",
            "speaker_unavailable": True, "source_cue_id": identifier,
            "source_timing": timing, "source_line": block_start + timing_index + 2,
            "source_char_start": begin, "source_char_end": finish,
        })
    if not segments:
        raise InputError("The subtitle file contains no transcript cues.")
    return segments


def validate_subtitle_source(document: dict):
    kind = "srt" if document["source_format"] == "srt-utf8" else "vtt"
    text = document.get("source_text")
    expected = parse_cues(text, kind)
    filename = document.get("source_text_filename")
    if (not isinstance(filename, str) or not filename.lower().endswith("." + kind)
            or document.get("source_text_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()
            or document.get("segments") != expected):
        raise InputError("Subtitle evidence must match every original cue, clock, text span and source hash.")


def normalize_subtitles(text: str, hearing: dict, filename: str, synthetic: bool = False) -> dict:
    if not isinstance(filename, str) or len(filename) > 300 or not re.search(r"\.(srt|vtt)$", filename, re.I):
        raise InputError("Choose a transcript with a .srt or .vtt extension.")
    if not isinstance(hearing, dict) or not isinstance(synthetic, bool):
        raise InputError("Provide hearing metadata and an explicit source scope.")
    kind = filename.rsplit(".", 1)[-1].lower()
    segments = parse_cues(text, kind)
    metadata = copy.deepcopy(hearing)
    metadata.setdefault("source_file", filename)
    if not metadata.get("id"):
        metadata["id"] = kind + "-" + fingerprint(
            {"text": text, "filename": filename, "hearing": metadata, "synthetic": synthetic})[:20]
    return validate({
        "schema_version": 1, "synthetic": synthetic,
        "source_scope": "synthetic" if synthetic else "public_hearing",
        "source_format": "srt-utf8" if kind == "srt" else "webvtt-utf8",
        "hearing": metadata, "segments": segments, "source_text": text,
        "source_text_filename": filename,
        "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    })


def normalize_transcript_file(text: str, hearing: dict, filename: str, synthetic: bool = False) -> dict:
    if isinstance(filename, str) and filename.lower().endswith(".txt"):
        return normalize_text(text, hearing, filename, synthetic)
    return normalize_subtitles(text, hearing, filename, synthetic)
