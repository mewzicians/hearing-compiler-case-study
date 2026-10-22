"""Plain UTF-8 transcripts preserve text evidence, without inventing audio times."""
from __future__ import annotations

import copy
import hashlib
import re

from .pipeline import InputError, fingerprint, validate

MAX_TEXT_BYTES = 4 * 1024 * 1024
PLAIN_TEXT_FORMAT = "plain-text-utf8"


def normalize_text(text: str, hearing: dict, filename: str, synthetic: bool = False) -> dict:
    if not isinstance(text, str) or not text.lstrip("\ufeff").strip():
        raise InputError("Choose a nonempty UTF-8 text transcript.")
    try:
        raw = text.encode("utf-8")
    except UnicodeError as error:
        raise InputError("The text contains invalid Unicode. Export it as UTF-8.") from error
    if len(raw) > MAX_TEXT_BYTES:
        raise InputError("Use a text transcript no larger than 4 MB.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise InputError("The transcript contains binary/control characters. Export plain UTF-8 text.")
    if not isinstance(filename, str) or not filename.lower().endswith(".txt") or len(filename) > 300:
        raise InputError("Choose a transcript file with a .txt extension.")
    if not isinstance(hearing, dict) or not isinstance(synthetic, bool):
        raise InputError("Provide hearing metadata and an explicit source scope.")
    metadata = copy.deepcopy(hearing)
    metadata.setdefault("source_file", filename)
    if not metadata.get("id"):
        metadata["id"] = "txt-" + fingerprint(
            {"text": text, "filename": filename, "hearing": metadata, "synthetic": synthetic})[:20]
    segments = []
    cursor = 1 if text.startswith("\ufeff") else 0
    for line_number, line in enumerate(re.split(r"(?<=\n)|(?<=\r)(?!\n)", text[cursor:]), 1):
        content = line.rstrip("\r\n")
        for begin in range(0, len(content), 50000):
            part = content[begin:begin + 50000]
            if not part.strip():
                continue
            if len(segments) >= 10000:
                raise InputError("The text has more than 10,000 blocks. Split it into smaller hearing excerpts.")
            ordinal = len(segments) + 1
            segments.append({
                "id": f"txt{ordinal:04}", "start": None, "end": None,
                "timing_unavailable": True, "speaker": f"Unattributed text block {ordinal}",
                "speaker_unavailable": True, "text": part, "source_line": line_number,
                "source_char_start": cursor + begin, "source_char_end": cursor + begin + len(part),
            })
        cursor += len(line)
    return validate({
        "schema_version": 1, "synthetic": synthetic,
        "source_scope": "synthetic" if synthetic else "public_hearing",
        "source_format": PLAIN_TEXT_FORMAT, "hearing": metadata,
        "source_text": text, "source_text_filename": filename,
        "source_text_sha256": hashlib.sha256(raw).hexdigest(), "segments": segments,
    })
