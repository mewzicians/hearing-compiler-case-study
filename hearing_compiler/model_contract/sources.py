"""Strict JSON and canonical original-source identity."""
from __future__ import annotations

import hashlib
import json
import math


class SourceError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def fingerprint(value):
    try:
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    except (UnicodeError, ValueError, TypeError) as error:
        raise SourceError(f"Invalid canonical UTF-8 JSON: {error}") from error


def strict_json(raw):
    if not isinstance(raw, (str, bytes)):
        raise SourceError("Candidate must be raw JSON text or UTF-8 bytes.")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SourceError(f"Duplicate JSON member: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise SourceError(f"Non-JSON numeric constant: {value}")

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise SourceError("Nonfinite JSON number.")
        return result

    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        result = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=invalid_constant, parse_float=number)
        def unicode_strings(value):
            if isinstance(value, str):
                value.encode("utf-8")
            elif isinstance(value, dict):
                for key, item in value.items():
                    key.encode("utf-8")
                    unicode_strings(item)
            elif isinstance(value, list):
                for item in value:
                    unicode_strings(item)
        unicode_strings(result)
        return result
    except (ValueError, UnicodeError) as error:
        raise SourceError(str(error)) from error


def pointer(value, path):
    if path == "":
        return value
    if not isinstance(path, str) or not path.startswith("/"):
        raise SourceError("Invalid JSON pointer.")
    node = value
    for token in path[1:].split("/"):
        index = 0
        decoded = ""
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in "01":
                    raise SourceError("Invalid JSON pointer escape.")
                decoded += "~" if token[index + 1] == "0" else "/"
                index += 2
            else:
                decoded += token[index]
                index += 1
        if isinstance(node, list):
            if not decoded.isdigit() or (len(decoded) > 1 and decoded.startswith("0")):
                raise SourceError("Invalid array pointer.")
            position = int(decoded)
            if position >= len(node):
                raise SourceError("Array pointer out of range.")
            node = node[position]
        elif isinstance(node, dict) and decoded in node:
            node = node[decoded]
        else:
            raise SourceError("Dangling JSON pointer.")
    return node


def context_for_case(case):
    return {"hearing": case["document"]["hearing"], **case["context"]}


class SourceIndex:
    def __init__(self, context, current):
        if not isinstance(context, dict) or not isinstance(current, list) or not current:
            raise SourceError("Provide context and nonempty current segments.")
        hearing, coverage = context.get("hearing"), context.get("coverage")
        if not isinstance(hearing, dict) or not isinstance(hearing.get("id"), str) or not hearing["id"]:
            raise SourceError("Missing supplied hearing ID.")
        if not isinstance(coverage, dict) or not isinstance(coverage.get("span_id"), str) or not coverage["span_id"]:
            raise SourceError("Missing supplied span ID.")
        self.hearing_id, self.span_id = hearing["id"], coverage["span_id"]
        self.context, self.current = context, current
        self.profile = context.get("source_profile", "timed-segments")
        if self.profile not in {"timed-segments", "plain-text-utf8"}:
            raise SourceError("Unknown original-source profile.")
        originals = None
        if self.profile == "plain-text-utf8":
            from hearing_compiler.pipeline import validate, InputError
            try:
                document = validate(context.get("source_document"))
            except InputError as error:
                raise SourceError(f"Invalid retained untimed original: {error}") from error
            if document.get("source_format") != self.profile or document["hearing"] != hearing:
                raise SourceError("Untimed profile must retain matching original hearing/text.")
            originals = {s["id"]: s for s in document["segments"]}
            self.source_positions = {s["id"]: i for i, s in enumerate(document["segments"])}
        self.coverage = coverage
        if (coverage.get("scope", "excerpt") not in {"excerpt", "full_hearing"}
                or not isinstance(coverage.get("gaps", []), list)
                or not isinstance(coverage.get("complete_testimony_ranges", []), list)):
            raise SourceError("Malformed declared coverage.")
        self.records, self.current_ids, self.speakers = {}, [], set()
        for packet, segments in [("context", context.get("context_segments", [])), ("current", current)]:
            if not isinstance(segments, list):
                raise SourceError(f"{packet} segments must be an array.")
            seen, previous = set(), -1
            for segment in segments:
                if not isinstance(segment, dict):
                    raise SourceError("Original segment must be an object.")
                for key in ("id", "speaker", "text"):
                    if not isinstance(segment.get(key), str) or not segment[key]:
                        raise SourceError(f"Missing original {key}.")
                sid = segment["id"]
                if sid in seen:
                    raise SourceError(f"Duplicate ID within {packet}: {sid}")
                seen.add(sid)
                if originals is not None:
                    if originals.get(sid) != segment or self.source_positions[sid] <= previous:
                        raise SourceError("Untimed packet must preserve chronological original text blocks.")
                    previous = self.source_positions[sid]
                else:
                    for key in ("start", "end"):
                        number = segment.get(key)
                        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                            raise SourceError("Source times must be finite nonnegative numbers.")
                    if segment["start"] < previous or segment["end"] < segment["start"]:
                        raise SourceError("Source segments must be chronological with nonnegative duration.")
                    previous = segment["start"]
                if sid in self.records and self.records[sid] != segment:
                    raise SourceError(f"Conflicting original versions: {sid}")
                self.records[sid] = segment
                if not segment.get("speaker_unavailable"):
                    self.speakers.add(segment["speaker"])
                if packet == "current":
                    self.current_ids.append(sid)
        self.metadata = {}
        entries = context.get("trusted_metadata", [])
        if not isinstance(entries, list):
            raise SourceError("Trusted metadata must be an array.")
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not entry["id"] or entry["id"] in self.metadata
                    or entry.get("purpose") not in {"hearing_metadata", "bill_metadata"}
                    or not isinstance(entry.get("source_ref"), str) or not entry["source_ref"]):
                raise SourceError("Metadata needs unique registered IDs, allowed purpose and source reference.")
            self.metadata[entry["id"]] = entry
        for span in coverage.get("complete_testimony_ranges", []):
            if (not isinstance(span, dict) or span.get("start") not in self.records
                    or span.get("end") not in self.records
                    or (self.source_positions[span["start"]] > self.source_positions[span["end"]]
                        if originals is not None else
                        self.records[span["start"]]["start"] > self.records[span["end"]]["end"])):
                raise SourceError("Invalid declared complete range.")
        self.sha256 = fingerprint({"context": context, "current_segments": current})

    def evidence(self, item):
        if not isinstance(item, dict) or set(item) != {"segment_id", "quote"}:
            raise SourceError("Evidence requires segment_id and quote only.")
        original = self.records.get(item["segment_id"])
        quote = item["quote"]
        if original is None or not isinstance(quote, str) or not quote or quote not in original["text"]:
            raise SourceError("Evidence has a dangling ID or nonliteral quotation.")

    def complete(self, ids):
        if not ids or self.coverage.get("gaps"):
            return False
        originals = [self.records[sid] for sid in ids if sid in self.records]
        if len(originals) != len(ids):
            return False
        if self.profile == "plain-text-utf8":
            positions = [self.source_positions[s["id"]] for s in originals]
            return any(self.source_positions[r["start"]] <= min(positions)
                       and self.source_positions[r["end"]] >= max(positions)
                       for r in self.coverage.get("complete_testimony_ranges", []))
        start, end = min(s["start"] for s in originals), max(s["end"] for s in originals)
        return any(self.records[r["start"]]["start"] <= start and self.records[r["end"]]["end"] >= end
                   for r in self.coverage.get("complete_testimony_ranges", []))
