"""Conservative, extractive baseline. This is not a general-purpose AI extractor."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from typing import Any

REVIEW_STATES = {"READY", "NEEDS_REVIEW", "UNRESOLVED", "APPROVED"}
BILL = re.compile(r"\b([HS])\.\s*(\d{1,5})\b", re.I)
INTRO = re.compile(
    r"(?:I'm|I am|My name is)\s+(.+?)(?:\s+(?:with|from|representing)\s+(.+?))?[.!](?:\s|$)",
    re.I,
)
LEGISLATOR = re.compile(
    r"(?:I'm|I am|This is)\s+((?:Representative|Rep\.|Senator|Sen\.)\s+[\w '-]+)",
    re.I,
)
GAP = re.compile(r"\[(?:inaudible|unclear|overlap|missing)[^\]]*\]", re.I)
POSITION = re.compile(r"\b(support|oppose|opposition)\b", re.I)


def introduction(text: str):
    match = INTRO.search(text)
    if match and (GAP.search(match.group(1)) or
                  re.fullmatch(r"[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){1,4}", match.group(1))):
        return match
    return None


class InputError(ValueError):
    pass


def field(value: Any, evidence: list[str], uncertain: bool = False) -> dict:
    return {
        "value": value,
        "evidence": list(dict.fromkeys(evidence)),
        "review": "UNRESOLVED" if value is None else "NEEDS_REVIEW" if uncertain else "READY",
        "original_value": copy.deepcopy(value),
        "corrected": False,
    }


def validate(document: dict) -> dict:
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise InputError("Use a transcript object with schema_version: 1.")
    if document.get("synthetic") is not True and not (
        document.get("synthetic") is False and document.get("source_scope") == "public_hearing"
    ):
        raise InputError("Label the transcript synthetic, or explicitly declare source_scope: public_hearing. Private inputs are outside this workflow.")
    hearing = document.get("hearing")
    if not isinstance(hearing, dict):
        raise InputError("Missing hearing metadata.")
    for key in ("id", "title", "date", "committee", "source_file"):
        if not isinstance(hearing.get(key), str) or not hearing[key].strip():
            raise InputError(f"hearing.{key} must be nonempty text.")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", hearing["id"]):
        raise InputError("Use letters, numbers, underscores or hyphens for the hearing id.")
    segments = document.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 10000:
        raise InputError("Provide between 1 and 10,000 chronological transcript segments.")
    seen: set[str] = set()
    previous = -1.0
    plain_text = document.get("source_format") == "plain-text-utf8"
    if document.get("source_format") in ("srt-utf8", "webvtt-utf8"):
        from .subtitle_import import validate_subtitle_source
        validate_subtitle_source(document)
    original_text = document.get("source_text")
    if plain_text and (not isinstance(original_text, str) or not original_text.lstrip("\ufeff").strip()):
        raise InputError("Plain-text imports must retain their original source text.")
    if plain_text:
        try:
            original_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
        except UnicodeError as error:
            raise InputError("Plain-text source must be valid Unicode.") from error
        if (document.get("source_text_sha256") != original_hash
                or not isinstance(document.get("source_text_filename"), str)
                or not document["source_text_filename"].lower().endswith(".txt")):
            raise InputError("Plain-text source filename and hash must match the retained original text.")
    previous_char = 0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise InputError(f"Segment {index + 1} must be an object.")
        for key in ("id", "speaker", "text"):
            if not isinstance(segment.get(key), str) or not segment[key].strip():
                raise InputError(f"Segment {index + 1}: {key} must be nonempty text.")
        if segment["id"] in seen:
            raise InputError(f"Duplicate segment id: {segment['id']}.")
        seen.add(segment["id"])
        if plain_text:
            if (segment.get("start") is not None or segment.get("end") is not None
                    or segment.get("timing_unavailable") is not True
                    or segment.get("speaker_unavailable") is not True):
                raise InputError("Plain-text imports have unavailable timestamps and speaker attribution.")
            begin, end = segment.get("source_char_start"), segment.get("source_char_end")
            if (isinstance(begin, bool) or isinstance(end, bool)
                    or not isinstance(begin, int) or not isinstance(end, int)
                    or not previous_char <= begin < end <= len(original_text)
                    or original_text[begin:end] != segment["text"]
                    or original_text[previous_char:begin].lstrip("\ufeff").strip()
                    or isinstance(segment.get("source_line"), bool)
                    or segment.get("source_line") != 1 + len(re.findall(r"\r\n|\r|\n", original_text[:begin]))):
                raise InputError("Text evidence must preserve complete, ordered original character spans and line references.")
            previous_char = end
            if len(segment["text"]) > 50000:
                raise InputError(f"{segment['id']}: segment text is too long.")
            continue
        for key in ("start", "end"):
            value = segment.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InputError(f"{segment['id']}: {key} must be seconds as a number.")
            if not math.isfinite(value) or value < 0:
                raise InputError(f"{segment['id']}: {key} must be finite and nonnegative.")
        # Whisper can emit zero-length segments; keep their original clocks.
        if segment["end"] < segment["start"]:
            raise InputError(f"{segment['id']}: end must not precede start.")
        if segment["start"] < previous:
            raise InputError(f"{segment['id']}: segments are out of chronological order.")
        previous = segment["start"]
        if len(segment["text"]) > 50000:
            raise InputError(f"{segment['id']}: segment text is too long.")
    if plain_text and original_text[previous_char:].strip():
        raise InputError("Text evidence must cover the complete source text.")
    result = copy.deepcopy(document)
    # Drafts and saved jobs carry only known metadata. Extra hearing fields
    # remain in the original input instead of mismatching the saved job.
    result["hearing"] = {key: hearing[key] for key in ("id", "title", "date", "committee", "source_file")}
    return result


def fingerprint(document: dict) -> str:
    payload = json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bills(text: str) -> list[str]:
    return list(dict.fromkeys(f"{a.upper()}.{b}" for a, b in BILL.findall(text)))


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[!?;])\s+|(?<=\.)\s+(?=[A-Z])", text) if s.strip()]


def stance(text: str, bill: str | None) -> str:
    matches = list(POSITION.finditer(text))
    mentioned = list(BILL.finditer(text))
    if bill and len(mentioned) > 1:
        location = next((m.start() for m in mentioned if bills(m.group()) == [bill]), 0)
        preceding = [m for m in matches if m.start() < location]
        matches = preceding[-1:]
    values = set()
    for match in matches:
        prefix = text[max(0, match.start() - 40):match.start()].lower()
        suffix = text[match.end():match.end() + 30].lower()
        if re.search(r"\b(?:not|don't|cannot|can't|may|might|could)\b", prefix):
            values.add("Unclear")
        elif re.match(r"\s+(?:parts?|some|only)\b", suffix):
            values.add("Unclear")
        else:
            values.add("Support" if match.group().lower() == "support" else "Oppose")
    return next(iter(values)) if len(values) == 1 else "Unclear"


def extract(document: dict) -> dict:
    document = validate(document)
    segments = document["segments"]
    identities: dict[str, dict] = {}
    roles: dict[str, str] = {}
    for segment in segments:
        text, speaker = segment["text"], segment["speaker"]
        legislator = LEGISLATOR.search(text)
        intro = introduction(text)
        if legislator:
            identities[speaker] = {
                "name": field(legislator.group(1).strip().rstrip("."), [segment["id"]]),
                "organization": field(None, []),
            }
            roles[speaker] = "legislator"
        elif intro:
            name, organization = intro.groups()
            identities[speaker] = {
                "name": field(None if GAP.search(name) else name.strip(), [segment["id"]]),
                "organization": field(
                    None if not organization or GAP.search(organization) else organization.strip(),
                    [segment["id"]],
                ),
            }
            roles[speaker] = "witness"
        elif POSITION.search(text) and "?" not in text and re.search(r"\b(?:I|we)\b", text) and speaker not in roles:
            roles[speaker] = "witness"
    for segment in segments:
        if re.search(r"\b(?:I am here to testify|I am testifying|we are testifying|I will testify)\b",
                     segment["text"], re.I):
            roles[segment["speaker"]] = "witness"

    testimony_by_speaker: dict[str, dict] = {}
    testimonies: list[dict] = []
    questions: list[dict] = []
    issues: list[dict] = []
    context: list[str] = []
    context_evidence: list[str] = []
    active_witness: str | None = None
    pending: dict | None = None
    responding: dict | None = None

    def identity(speaker: str | None, key: str = "name") -> dict:
        return copy.deepcopy(identities.get(speaker, {}).get(key, field(None, [])))

    def testimony(speaker: str) -> dict:
        if speaker not in testimony_by_speaker:
            record = {
                "speaker": speaker,
                "name": identity(speaker),
                "organization": identity(speaker, "organization"),
                "statements": [],
            }
            testimony_by_speaker[speaker] = record
            testimonies.append(record)
        return testimony_by_speaker[speaker]

    def statement(record: dict, bill: str | None, evidence: str) -> dict:
        existing = next((s for s in record["statements"] if s["bill"]["value"] == bill), None)
        if existing:
            if evidence not in existing["bill"]["evidence"]:
                existing["bill"]["evidence"].append(evidence)
            return existing
        item = {
            "bill": field(bill, [evidence], bill is None),
            "position": field("Unclear", [evidence], True),
            "arguments": [],
        }
        record["statements"].append(item)
        return item

    previous_end = 0
    for segment in segments:
        sid, speaker, text = segment["id"], segment["speaker"], segment["text"]
        if GAP.search(text):
            issues.append(field(f"Transcript contains uncertain or missing speech in {sid}.", [sid], True))
        if segment["start"] is not None and segment["start"] < previous_end:
            issues.append(field(f"Overlapping time spans at {sid}; inspect turn attribution.", [sid], True))
        if segment["end"] is not None:
            previous_end = max(previous_end, segment["end"])
        explicit = bills(text)
        is_witness = roles.get(speaker) == "witness"

        if not is_witness:
            if re.search(r"\b(?:next witness|testimony is concluded|hearing is adjourned)\b", text, re.I):
                pending = None
                responding = None
                active_witness = None
                context = []
                context_evidence = []
            if "?" in text:
                if pending and pending["asker_speaker"] == speaker:
                    pending["question"]["value"] += "\n" + text
                    pending["question"]["original_value"] = pending["question"]["value"]
                    pending["question"]["evidence"].append(sid)
                    if explicit and explicit != [pending["bill"]["value"]]:
                        pending["bill"] = field(None, pending["bill"]["evidence"] + [sid], True)
                else:
                    relevant = explicit or context
                    asker = identity(speaker)
                    if not asker["evidence"]:
                        asker["evidence"] = [sid]
                    pending = {
                        "asker_speaker": speaker,
                        "respondent_speaker": active_witness,
                        "asker": asker,
                        "respondent": identity(active_witness),
                        "bill": field(relevant[0] if len(relevant) == 1 else None,
                                      [sid] if explicit else context_evidence + [sid], len(relevant) != 1),
                        "question": field(text, [sid]),
                        "response": field(None, []),
                    }
                    questions.append(pending)
                responding = None
            elif explicit:
                context = explicit
                context_evidence = [sid]
            continue

        # A new witness must never answer the preceding witness's pending question.
        if active_witness is not None and active_witness != speaker:
            pending = None
            responding = None
            context = []
            context_evidence = []
        active_witness = speaker
        record = testimony(speaker)
        if pending and pending["respondent_speaker"] == speaker:
            pending["respondent_speaker"] = speaker
            pending["respondent"] = identity(speaker)
            pending["response"] = field(text, [sid], bool(GAP.search(text)))
            responding, pending = pending, None
            continue
        if responding and responding["respondent_speaker"] == speaker and not explicit:
            response = responding["response"]
            response["value"] += "\n" + text
            response["original_value"] = response["value"]
            response["evidence"].append(sid)
            if GAP.search(text):
                response["review"] = "NEEDS_REVIEW"
            continue
        responding = None
        if explicit:
            context = explicit
            context_evidence = [sid]
        for sentence in sentences(text):
            introduced = introduction(sentence) or LEGISLATOR.search(sentence)
            sentence_bills = bills(sentence)
            if introduced and not sentence_bills and not POSITION.search(sentence):
                continue
            relevant = sentence_bills or context
            targets = relevant if sentence_bills or len(relevant) == 1 else [None]
            if not targets:
                targets = [None]
            for bill in targets:
                item = statement(record, bill, sid)
                if POSITION.search(sentence):
                    position = stance(sentence, bill)
                    prior = item["position"]
                    if prior["value"] != "Unclear" and prior["value"] != position:
                        position = "Unclear"
                    evidence = list(dict.fromkeys(prior["evidence"] + [sid]))
                    item["position"] = field(position, evidence, position == "Unclear")
                argument = re.sub(
                    r"^I\s+(?:support|oppose)\s+[HS]\.\s*\d+(?:\s+because\s+)?",
                    "", sentence, flags=re.I,
                ).strip(" .")
                if argument and (not POSITION.search(sentence) or len(argument.split()) >= 5):
                    item["arguments"].append(field(sentence, [sid], bool(GAP.search(sentence))))
    for record in testimonies:
        if not record["statements"]:
            evidence = [s["id"] for s in segments if s["speaker"] == record["speaker"]]
            statement(record, None, evidence[0])
    if not testimonies:
        issues.append(field("No witness testimony was detected. Review the transcript before copying.", [], True))
    for question in questions:
        if question["response"]["value"] is None:
            issues.append(field("A question has no detected response; no answer has been invented.",
                                question["question"]["evidence"], True))
    draft = {
        "schema_version": 1,
        "synthetic": document["synthetic"],
        "provider": "conservative-extractive-baseline-v1",
        "hearing": document["hearing"],
        "segments": segments,
        "testimonies": testimonies,
        "questions": questions,
        "issues": issues,
    }
    if any(segment.get("speaker_unavailable") for segment in segments):
        draft["issues"].append(field("Speaker labels were missing from the export. Turn and Q&A attribution cannot be established reliably.",
                                     [s["id"] for s in segments if s.get("speaker_unavailable")], True))
    if document.get("source_format") in ("plain-text-utf8", "srt-utf8", "webvtt-utf8"):
        for key in ("source_format", "source_text", "source_text_filename", "source_text_sha256"):
            draft[key] = copy.deepcopy(document[key])
        draft["issues"].append(field(
            ("Plain text has no validated timestamps or speaker turns. Evidence uses source lines; audio must be reviewed manually."
             if document["source_format"] == "plain-text-utf8" else
             "Subtitle clocks come from the imported cues. Speaker turns are unavailable; verify timing against the original recording."),
            [s["id"] for s in segments], True))
        for _, item in fields(draft):
            if item["value"] is not None:
                item["review"] = "NEEDS_REVIEW"
    return prepare_review(draft)


def fields(node: Any, path: str = ""):
    if isinstance(node, dict):
        if {"value", "evidence", "review"} <= node.keys():
            yield path, node
        else:
            for key, value in node.items():
                if key not in ("segments", "hearing", "review_log"):
                    yield from fields(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from fields(value, f"{path}/{index}")


def _matching_questions(draft: dict, testimony: dict, statement: dict, ambiguous: bool = False) -> list:
    speaker, bill = testimony["speaker"], statement["bill"]["value"]
    return [q for q in draft["questions"]
            if (q["respondent_speaker"] == speaker or ambiguous and q["respondent_speaker"] is None)
            and (q["bill"]["value"] == bill and bill is not None
                 or ambiguous and (q["bill"]["value"] is None or bill is None))]


def prepare_review(draft: dict) -> dict:
    """Pure view normalization; existing saved work is changed only by an explicit save."""
    if draft.get("draft_format") == "hearing-review-units/v1":
        from .rich_review import prepare
        return prepare(draft)
    result = copy.deepcopy(draft)
    result.setdefault("testimony_groups", [])
    known = {t["id"] for t in result["testimonies"] if "id" in t}
    for index, testimony in enumerate(result["testimonies"]):
        if "id" not in testimony:
            candidate = f"testimony-{index + 1}"
            while candidate in known:
                candidate += "-legacy"
            testimony["id"] = candidate
            known.add(candidate)
        for statement in testimony["statements"]:
            if _matching_questions(result, testimony, statement):
                statement.pop("questions_none", None)
            else:
                statement.setdefault("questions_none", field(None, [], True))
                if _matching_questions(result, testimony, statement, ambiguous=True):
                    statement["questions_none"] = field(None, [], True)
    return result


def _text(value: Any, label: str, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 50000:
        raise InputError(f"{label} must be nonempty text" + (" or null." if nullable else "."))
    return value.strip()


def _evidence(draft: dict, value: Any, required: bool = False) -> list[str]:
    known = {s["id"] for s in draft["segments"]}
    if (not isinstance(value, list) or len(value) > 10000
            or any(not isinstance(i, str) or i not in known for i in value)):
        raise InputError("Evidence must be a list of existing transcript segment ids.")
    result = list(dict.fromkeys(value))
    if required and not result:
        raise InputError("Select transcript evidence for the added material or explicit confirmation.")
    return result


def _speaker(draft: dict, value: Any, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in {s["speaker"] for s in draft["segments"]}:
        raise InputError("Speaker must be an existing transcript speaker label.")
    return value


def _item(items: list, value: Any, label: str):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < len(items):
        raise InputError(f"{label} index is out of range.")
    return items[value]


def _manual(value: str | None, evidence: list[str]) -> dict:
    item = field(None, evidence, True)
    item.update(value=value, corrected=value is not None,
                review="NEEDS_REVIEW" if value else "UNRESOLVED")
    return item


def _reset(item: dict) -> None:
    item["review"] = "NEEDS_REVIEW" if item["value"] or item.get("not_applicable") else "UNRESOLVED"


def _audit(draft: dict, operation: dict, before: Any, after: Any) -> None:
    log = draft.setdefault("review_log", [])
    log.append({"sequence": len(log) + 1, "operation": copy.deepcopy(operation),
                "before": copy.deepcopy(before), "after": copy.deepcopy(after)})


OPERATION_KEYS = {
    "add_testimony": {"speaker", "name", "organization", "evidence"},
    "remove_testimony": {"testimony"},
    "add_statement": {"testimony", "bill", "position", "evidence"},
    "remove_statement": {"testimony", "statement"},
    "add_argument": {"testimony", "statement", "value", "evidence"},
    "remove_argument": {"testimony", "statement", "argument"},
    "add_question": {"asker_speaker", "respondent_speaker", "asker", "respondent", "bill",
                     "question", "response", "evidence", "response_evidence"},
    "remove_question": {"question"},
    "set_evidence": {"path", "evidence"},
    "set_question_speakers": {"question", "asker_speaker", "respondent_speaker"},
    "group_testimonies": {"testimonies"},
    "ungroup_testimonies": {"group_id"},
    "confirm_no_questions": {"testimony", "statement", "confirmed", "evidence"},
}


def _operation(draft: dict, op: dict) -> None:
    if (not isinstance(op, dict) or not isinstance(op.get("op"), str)
            or op["op"] not in OPERATION_KEYS
            or set(op) - OPERATION_KEYS[op["op"]] - {"op"}):
        raise InputError("Use a supported review operation with only its allowed fields.")
    name = op["op"]
    before, after = None, None
    if name == "add_testimony":
        if len(draft["testimonies"]) >= 10000:
            raise InputError("Too many testimony records.")
        evidence = _evidence(draft, op.get("evidence"), True)
        after = {"id": "testimony-" + uuid.uuid4().hex, "speaker": _speaker(draft, op.get("speaker")),
                 "name": _manual(_text(op.get("name"), "Testifier name"), evidence),
                 "organization": _manual(_text(op.get("organization"), "Organization"), evidence),
                 "statements": [{"bill": _manual(None, evidence),
                                 "position": _manual("Unclear", evidence), "arguments": [],
                                 "questions_none": field(None, [], True)}]}
        draft["testimonies"].append(after)
    elif name in {"remove_testimony", "add_statement", "remove_statement", "add_argument",
                  "remove_argument", "confirm_no_questions"}:
        testimony = _item(draft["testimonies"], op.get("testimony"), "Testimony")
        if name == "remove_testimony":
            before = copy.deepcopy(testimony)
            draft["testimonies"].pop(op["testimony"])
            retained = []
            for group in draft["testimony_groups"]:
                if testimony["id"] in group["members"]:
                    group["members"].remove(testimony["id"])
                    _reset(group["confirmation"])
                if len(group["members"]) >= 2:
                    retained.append(group)
            draft["testimony_groups"] = retained
        elif name == "add_statement":
            if len(testimony["statements"]) >= 10000:
                raise InputError("Too many bill statements.")
            evidence = _evidence(draft, op.get("evidence"), True)
            position = op.get("position", "Unclear")
            if position not in ("Support", "Oppose", "Unclear"):
                raise InputError("Position must be Support, Oppose, or Unclear.")
            after = {"bill": _manual(_text(op.get("bill"), "Bill"), evidence),
                     "position": _manual(position, evidence), "arguments": [],
                     "questions_none": field(None, [], True)}
            testimony["statements"].append(after)
        else:
            statement = _item(testimony["statements"], op.get("statement"), "Statement")
            if name == "remove_statement":
                before = copy.deepcopy(statement)
                testimony["statements"].pop(op["statement"])
            elif name == "add_argument":
                if len(statement["arguments"]) >= 10000:
                    raise InputError("Too many arguments.")
                after = _manual(_text(op.get("value"), "Argument", False),
                                _evidence(draft, op.get("evidence"), True))
                statement["arguments"].append(after)
            elif name == "remove_argument":
                before = copy.deepcopy(_item(statement["arguments"], op.get("argument"), "Argument"))
                statement["arguments"].pop(op["argument"])
            else:
                if not isinstance(op.get("confirmed"), bool):
                    raise InputError("Confirming no questions requires an explicit true or false.")
                before = copy.deepcopy(statement.get("questions_none"))
                if op["confirmed"] and _matching_questions(draft, testimony, statement, ambiguous=True):
                    raise InputError("Resolve or remove matching/ambiguous questions before confirming none noted.")
                evidence = _evidence(draft, op.get("evidence", []), op["confirmed"])
                if op["confirmed"] and not any(s["id"] in evidence and s["speaker"] == testimony["speaker"]
                                                for s in draft["segments"]):
                    raise InputError("No-questions confirmation must cite this testifier's transcript evidence.")
                after = _manual("None noted during this testimony." if op["confirmed"] else None, evidence)
                if op["confirmed"]:
                    after["review"] = "APPROVED"
                statement["questions_none"] = after
    elif name == "add_question":
        if len(draft["questions"]) >= 10000:
            raise InputError("Too many questions.")
        evidence = _evidence(draft, op.get("evidence"), True)
        response = _text(op.get("response"), "Response")
        response_evidence = _evidence(draft, op.get("response_evidence", []), bool(response))
        after = {"asker_speaker": _speaker(draft, op.get("asker_speaker")),
                 "respondent_speaker": _speaker(draft, op.get("respondent_speaker"), True),
                 "asker": _manual(_text(op.get("asker"), "Question asker"), evidence),
                 "respondent": _manual(_text(op.get("respondent"), "Respondent"), response_evidence),
                 "bill": _manual(_text(op.get("bill"), "Bill"), evidence),
                 "question": _manual(_text(op.get("question"), "Question", False), evidence),
                 "response": _manual(response, response_evidence)}
        draft["questions"].append(after)
    elif name in {"remove_question", "set_question_speakers"}:
        question = _item(draft["questions"], op.get("question"), "Question")
        before = copy.deepcopy(question)
        if name == "remove_question":
            draft["questions"].pop(op["question"])
        else:
            for label, key in (("asker_speaker", "asker"), ("respondent_speaker", "respondent")):
                speaker = _speaker(draft, op.get(label), label == "respondent_speaker")
                if question[label] != speaker:
                    question[label] = speaker
                    # An old identity cannot silently move to a different generic speaker.
                    question[key]["value"] = None
                    question[key]["corrected"] = question[key]["original_value"] is not None
                _reset(question[key])
            for key in ("question", "response", "bill"):
                _reset(question[key])
            after = question
    elif name == "set_evidence":
        available = dict(fields(draft))
        if not isinstance(op.get("path"), str) or op["path"] not in available:
            raise InputError("Evidence path does not identify an extracted field.")
        target = available[op["path"]]
        before = copy.deepcopy(target)
        target["evidence"] = _evidence(draft, op.get("evidence"))
        _reset(target)
        after = target
    elif name == "group_testimonies":
        indexes = op.get("testimonies")
        if (not isinstance(indexes, list) or not 2 <= len(indexes) <= 10000
                or any(isinstance(i, bool) or not isinstance(i, int) for i in indexes)
                or len(set(indexes)) != len(indexes)):
            raise InputError("Choose two or more distinct testimony indexes.")
        members = [_item(draft["testimonies"], i, "Testimony") for i in indexes]
        ids = [t["id"] for t in members]
        if any(set(ids) & set(g["members"]) for g in draft["testimony_groups"]):
            raise InputError("Ungroup testimony before assigning it to a different shared entry.")
        evidence = list(dict.fromkeys(i for t in members for _, f in fields(t) for i in f["evidence"]))
        after = {"id": "group-" + uuid.uuid4().hex, "members": ids,
                 "confirmation": _manual("Shared testimony", evidence)}
        draft["testimony_groups"].append(after)
    else:
        group_id = op.get("group_id")
        if not isinstance(group_id, str):
            raise InputError("Choose an existing testimony group.")
        found = next((g for g in draft["testimony_groups"] if g["id"] == group_id), None)
        if found is None:
            raise InputError("Choose an existing testimony group.")
        before = copy.deepcopy(found)
        draft["testimony_groups"].remove(found)
    _audit(draft, op, before, after)


def correct(draft: dict, changes: list[dict] | None = None, operations: list[dict] | None = None) -> dict:
    result = _apply_corrections(draft, changes, operations)
    # Re-pointing evidence resets review; an approval requested in the same save still applies.
    approved = {c["path"] for c in changes or [] if isinstance(c, dict) and c.get("review") == "APPROVED"}
    repointed = {o.get("path") for o in operations or [] if isinstance(o, dict) and o.get("op") == "set_evidence"}
    if approved & repointed:
        available = dict(fields(result))
        for path in approved & repointed:
            item = available.get(path)
            if item and (item["value"] or item.get("not_applicable")):
                item["review"] = "APPROVED"
    return result


def _apply_corrections(draft: dict, changes: list[dict] | None = None, operations: list[dict] | None = None) -> dict:
    if draft.get("draft_format") == "hearing-review-units/v1":
        from .rich_review import correct as rich_correct
        return rich_correct(draft, changes, operations)
    changes = [] if changes is None else changes
    operations = [] if operations is None else operations
    if (not isinstance(changes, list) or not isinstance(operations, list)
            or not changes and not operations or len(changes) + len(operations) > 2000):
        raise InputError("Provide a nonempty list of at most 2,000 corrections and review operations.")
    result = prepare_review(draft)
    available = dict(fields(result))
    for change in changes:
        if (not isinstance(change, dict) or not isinstance(change.get("path"), str)
                or change["path"] not in available
                or set(change) - {"path", "value", "review", "not_applicable"}):
            raise InputError("Correction path does not identify an extracted field.")
        target = available[change["path"]]
        before = copy.deepcopy(target)
        status = change.get("review", target["review"])
        if not isinstance(status, str) or status not in REVIEW_STATES:
            raise InputError("Invalid review state.")
        if "not_applicable" in change:
            if not isinstance(change["not_applicable"], bool) or not re.fullmatch(r"/testimonies/\d+/organization", change["path"]):
                raise InputError("Not applicable is supported only for an optional organization.")
            target["not_applicable"] = change["not_applicable"]
        if "value" in change:
            value = change["value"]
            if value is not None and (not isinstance(value, str) or len(value) > 50000):
                raise InputError("Corrected values must be text or null.")
            if isinstance(value, str):
                try:
                    value.encode("utf-8")
                except UnicodeError as error:
                    raise InputError("Corrected values must contain valid UTF-8 text.") from error
            if change["path"].endswith("/position") and value not in ("Support", "Oppose", "Unclear"):
                raise InputError("Position must be Support, Oppose, or Unclear.")
            target["value"] = value.strip() if isinstance(value, str) else value
            target["corrected"] = target["value"] != target["original_value"]
            if target["value"]:
                target.pop("not_applicable", None)
            if "review" not in change and target["value"] != before["value"]:
                status = "NEEDS_REVIEW" if target["value"] else "UNRESOLVED"
            if target["value"] != before["value"] and re.fullmatch(r"/testimonies/\d+/statements/\d+/bill", change["path"]):
                pieces = change["path"].split("/")
                statement = result["testimonies"][int(pieces[2])]["statements"][int(pieces[4])]
                statement["questions_none"] = field(None, [], True)
        if target.get("not_applicable") and target["value"]:
            raise InputError("A not-applicable organization must have a null or empty value.")
        if change["path"].endswith("/questions_none") and "value" in change:
            raise InputError("Use the explicit no-questions confirmation control.")
        if change["path"].endswith("/questions_none") and status == "APPROVED":
            raise InputError("Use the explicit no-questions confirmation control to approve its evidence and context.")
        if status == "APPROVED" and not target["value"] and not target.get("not_applicable"):
            raise InputError("An empty or unknown field cannot be approved. Resolve it or keep it unresolved.")
        target["review"] = status
        _audit(result, {"op": "correct_field", **change}, before, target)
    result = prepare_review(result)
    for operation in operations:
        _operation(result, operation)
        result = prepare_review(result)
    return result


def review_summary(draft: dict) -> dict:
    if draft.get("draft_format") == "hearing-review-units/v1":
        from .rich_review import summary
        return summary(draft)
    draft = prepare_review(draft)
    values = [f for _, f in fields(draft)]
    unresolved = sum(f["review"] == "UNRESOLVED" or not f["value"] and not f.get("not_applicable") for f in values)
    missing = (int(not draft["testimonies"])
               + sum(not t["statements"] for t in draft["testimonies"])
               + sum(not s["arguments"] for t in draft["testimonies"] for s in t["statements"]))
    unresolved += missing
    approved = sum(f["review"] == "APPROVED" for f in values)
    return {
        "status": "APPROVED" if values and approved == len(values) and not missing else "UNRESOLVED" if unresolved else "NEEDS_REVIEW",
        "fields": len(values),
        "approved": approved,
        "unresolved": unresolved,
    }


def timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "Time unavailable"
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def markdown(draft: dict) -> str:
    if draft.get("draft_format") == "hearing-review-units/v1":
        from .rich_review import markdown as rich_markdown
        return rich_markdown(draft)
    draft = prepare_review(draft)
    metadata = draft["hearing"]
    lookup = {s["id"]: s for s in draft["segments"]}

    def escape(text: Any) -> str:
        return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", str(text))

    def value(item: dict) -> str:
        text = "Not applicable" if item.get("not_applicable") else escape(item["value"]) if item["value"] else "Unknown"
        if item["review"] != "APPROVED":
            text += " [unresolved]" if not item["value"] and not item.get("not_applicable") or item["review"] == "UNRESOLVED" else " [draft; review needed]"
        elif item["corrected"]:
            text += " [human corrected]"
        return text

    def evidence(items: list[dict]) -> str:
        ids = list(dict.fromkeys(i for item in items for i in item["evidence"]))
        return "; ".join(
            (f"Text line {lookup[i]['source_line']} · Time unavailable"
             if lookup[i].get("timing_unavailable")
             else f"{timestamp(lookup[i]['start'])}–{timestamp(lookup[i]['end'])}")
            + f" · {escape(lookup[i]['speaker'])} · {escape(i)}"
            for i in ids if i in lookup
        ) or "No transcript evidence; manual confirmation required."

    lines = [
        f"# {escape(metadata['title'])}", "",
        f"{escape(metadata['date'])} · {escape(metadata['committee'])}", "",
        f"Tags: Hearing Notes; {escape(metadata['committee'])}", "",
        "**Synthetic test hearing — not a real legislative record.**" if draft["synthetic"] else
        "**Public hearing draft — verify against the recording before use.**", "",
        f"Review: {review_summary(draft)['status'].replace('_', ' ').title()}",
        f"Source reference: {escape(metadata['source_file'])}", "",
    ]
    if "export_revision" in draft:
        lines += [f"Saved review revision: {escape(draft['export_revision'])}"]
    if draft.get("source_format") == "plain-text-utf8":
        lines += ["Plain-text evidence: timestamps and speaker turns unavailable; review source audio manually.",
                  f"Transcript file: {escape(draft['source_text_filename'])}", ""]
    if draft.get("export_saved_at"):
        lines += [f"Saved at: {escape(draft['export_saved_at'])}"]
    if "export_revision" in draft or draft.get("export_saved_at"):
        lines.append("")
    used_questions = set()

    def question_lines(question: dict) -> list[str]:
        return [
            f"Question asker: {value(question['asker'])} · {escape(question['asker_speaker'])}",
            f"Question: {value(question['question'])}",
            f"Respondent: {value(question['respondent'])} · {escape(question['respondent_speaker'] or 'Unknown speaker')}",
            f"Response: {value(question['response'])}", "",
            f"Evidence: {evidence([question[key] for key in ('asker', 'respondent', 'bill', 'question', 'response')])}", "",
        ]

    grouped = {member: group for group in draft["testimony_groups"] for member in group["members"]}
    emitted = set()
    if not draft["testimonies"]:
        lines += ["## Testimony requiring review", "", "No testimony entries remain. Inspect the transcript for omitted testimony.", ""]
    for ti, testimony in enumerate(draft["testimonies"]):
        if not testimony["statements"]:
            lines += ["## Bill: Unknown [unresolved]", "", f"Testifiers: {value(testimony['name'])}",
                      f"Transcript speaker: {escape(testimony['speaker'])}",
                      "No bill statements remain for this testifier; inspect the transcript.", ""]
        for si, statement in enumerate(testimony["statements"]):
            if (ti, si) in emitted:
                continue
            entries = [(ti, si, testimony, statement)]
            group = grouped.get(testimony["id"])
            # Shared output never merges differing or unknown bill/position values.
            if group and statement["bill"]["value"] and statement["position"]["value"] != "Unclear":
                for oi, other in enumerate(draft["testimonies"]):
                    if oi == ti or other["id"] not in group["members"]:
                        continue
                    for sj, candidate in enumerate(other["statements"]):
                        if ((oi, sj) not in emitted and candidate["bill"]["value"] == statement["bill"]["value"]
                                and candidate["position"]["value"] == statement["position"]["value"]):
                            entries.append((oi, sj, other, candidate))
            emitted.update((a, b) for a, b, _, _ in entries)
            shared_bill = copy.deepcopy(statement["bill"])
            member_bills = [s["bill"] for _, _, _, s in entries]
            if any(item["review"] != "APPROVED" for item in member_bills):
                shared_bill["review"] = ("UNRESOLVED" if any(item["review"] == "UNRESOLVED" for item in member_bills)
                                         else "NEEDS_REVIEW")
            shared_bill["corrected"] = any(item.get("corrected") for item in member_bills)
            lines += [f"## Bill: {value(shared_bill)}", "",
                      "Testifiers: " + "; ".join(value(t["name"]) for _, _, t, _ in entries)]
            if group:
                lines += [f"Shared testimony: {value(group['confirmation'])} (individual speaker records retained)"]
                if len(entries) == 1:
                    lines += ["Shared testifier positions or bills differ or remain unclear; entries are shown separately."]
            for _, _, t, s in entries:
                suffix = f" ({escape(t['speaker'])})" if len(entries) > 1 else ""
                lines += [f"Organization{suffix}: {value(t['organization'])}",
                          f"Transcript speaker: {escape(t['speaker'])}", f"Position{suffix}: {value(s['position'])}"]
            lines += ["", "Main Arguments:", ""]
            arguments = [(t, argument) for _, _, t, s in entries for argument in s["arguments"]]
            lines += [f"- {value(argument)}" + (f" ({escape(t['speaker'])})" if len(entries) > 1 else "")
                      for t, argument in arguments] or ["- No main argument detected; inspect the transcript."]
            cited = [f for _, _, t, s in entries for f in
                     [t["name"], t["organization"], s["bill"], s["position"]] + s["arguments"]]
            lines += ["", f"Evidence: {evidence(cited)}", ""]
            lines += ["### Legislator Questions", ""]
            matched = [
                (index, q) for index, q in enumerate(draft["questions"])
                if q["respondent_speaker"] in {t["speaker"] for _, _, t, _ in entries}
                and statement["bill"]["value"] is not None
                and q["bill"]["value"] == statement["bill"]["value"]
            ]
            for index, question in matched:
                used_questions.add(index)
                lines += question_lines(question)
            if not matched:
                unclear = any(_matching_questions(draft, t, s, ambiguous=True) for _, _, t, s in entries)
                confirmed = [s.get("questions_none") for _, _, _, s in entries]
                if not unclear and all(item and item["review"] == "APPROVED" and item["value"] for item in confirmed):
                    lines += ["- None noted during this testimony. [human confirmed]",
                              f"Evidence: {evidence(confirmed)}", ""]
                else:
                    lines += [("- Questions have unclear attribution; see the unassigned questions below." if unclear else
                               "- None detected for this testimony by the baseline; verify before recording 'None noted'."), ""]
    if len(used_questions) < len(draft["questions"]):
        lines += ["## Questions needing testimony or bill attribution", ""]
        for index, question in enumerate(draft["questions"]):
            if index not in used_questions:
                lines += [f"Bill: {value(question['bill'])}", ""] + question_lines(question)
    if draft["issues"]:
        lines += ["## Review notes", ""]
        lines += [f"- {value(issue)} Evidence: {evidence([issue])}" for issue in draft["issues"]]
        lines.append("")
    lines += ["---", "Compare with existing hearing notes and add missing material without replacing existing work.", ""]
    return "\n".join(lines)
