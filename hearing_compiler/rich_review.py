"""Loss-aware application review for validated model units, not a second extractor."""
from __future__ import annotations

import copy
import re
import uuid

from . import gemini
from .pipeline import InputError, REVIEW_STATES, field, fields, timestamp
from .model_contract.sources import pointer, SourceError
from .model_contract.validator import validate_model_draft
import json

FORMAT = "hearing-review-units/v1"
NO_RESPONSE = "No response; comment only."  # What the review page's No response (comment only) button saves.
NOT_ON_AGENDA = "Not on this hearing's agenda; check the number against the audio."
AGENDA_TITLE = "Title filled from the official agenda; confirm it matches."
TITLE_CHANGED = "The bill number changed; confirm this title still matches it."


def is_rich(draft):
    return draft.get("draft_format") == FORMAT


def agenda_titles(draft) -> dict:
    """Official bill titles saved with a Gemini draft; empty for older drafts and hearings without an agenda."""
    provenance = draft.get("extraction_provenance")
    rows = provenance.get("agenda") if isinstance(provenance, dict) else None
    return {row["bill"]: row["title"] for row in rows if isinstance(row, dict)
            and isinstance(row.get("bill"), str) and isinstance(row.get("title"), str)} if isinstance(rows, list) else {}


def typed_bills(value) -> list | None:
    """"H. 1975", "h1975" or "House Bill 1975" as H.1975, for a value made only of bill numbers."""
    parts = [p for p in re.split(r"\s*(?:/|,|&|\band\b)\s*", value, flags=re.I) if p.strip()]
    found = [gemini.BILL_MENTION.fullmatch(p.strip()) for p in parts]
    if not parts or not all(found):
        return None
    return list(dict.fromkeys(f"{m.group(1).upper()}.{int(m.group(2))}" for m in found))


def claim_field(claim):
    result = field(claim["value"], [e["segment_id"] for e in claim["evidence"]], True)
    result.update(basis=claim["basis"], missing_reason=claim["missing_reason"],
                  metadata_refs=copy.deepcopy(claim["metadata_refs"]),
                  source_quotes=copy.deepcopy(claim["evidence"]),
                  original_source_quotes=copy.deepcopy(claim["evidence"]),
                  original_basis=claim["basis"], original_missing_reason=claim["missing_reason"])
    return result


def identity(person, testifier=False):
    result = {"speaker_labels": copy.deepcopy(person["speaker_labels"]), "name": claim_field(person["name"])}
    if testifier:
        result["organization"] = claim_field(person["organization"])
    return result


def question(record):
    return {"id": "question-" + uuid.uuid4().hex, "asker": identity(record["asker"]),
            "asker_role": claim_field(record["asker_role"]), "question": claim_field(record["question"]),
            "bill": claim_field(record["bill_id"]),
            "responses": [{"respondent": identity(response["respondent"]),
                           "text": claim_field(response["text"])} for response in record["responses"]],
            "response_status": record["response_status"]}


def from_packets(document, packets, provenance):
    """Each unit retains its real contributions and all response records."""
    units, unassigned, issues, ledger = [], [], [], []
    for span, packet in enumerate(packets):
        output = packet["output"]
        validation = validate_model_draft(json.dumps(output), packet["context"], packet["current"])
        if validation["output_status"] != "VALID":
            raise InputError("An invalid model packet cannot become an application draft.")
        for record in output["testimony_units"]:
            units.append({
                "id": f"span-{span + 1}-{record['unit_id']}", "source_unit_id": record["unit_id"],
                "segment_ids": copy.deepcopy(record["segment_ids"]),
                "testifiers": [identity(person, True) for person in record["testifiers"]],
                "joint_basis": copy.deepcopy(record["joint_basis"]),
                "joint_confirmation": field("Source indicates joint presentation." if len(record["testifiers"]) > 1 else None,
                                            [e["segment_id"] for e in record["joint_basis"]], True)
                                      if len(record["testifiers"]) > 1 else None,
                "bill": claim_field(record["bill_id"]), "bill_title": claim_field(record["bill_title"]),
                "position": claim_field(record["position"]),
                "arguments": [{"contributors": copy.deepcopy(argument["contributors"]),
                               "text": claim_field(argument["text"])} for argument in record["arguments"]],
                "questions": [question(q) for q in record["questions"]],
                "question_status": record["question_status"],
                "questions_none": None if record["questions"] else field(None, [], True),
            })
        unassigned += [question(q) for q in output["unassigned_questions"]]
        for issue in output["review_issues"]:
            item = field(issue["reason"], [e["segment_id"] for e in issue["evidence"]], True)
            item.update(source_span=output["input_span_id"], source_target=issue["target"], kind=issue["kind"],
                        source_quotes=copy.deepcopy(issue["evidence"]))
            issues.append(item)
        ledger.append({"span_id": output["input_span_id"], "entries": copy.deepcopy(output["coverage_ledger"])})
    if len(packets) > 1:
        issues.append(field("This hearing spans several extraction packets. Review testimony boundaries, repeated claims and cross-span Q&A before use.",
                            [s["id"] for s in document["segments"]], True))
    draft = {
        "schema_version": 1, "draft_format": FORMAT, "synthetic": document["synthetic"],
        "provider": "local-model-validated-draft-v1", "hearing": copy.deepcopy(document["hearing"]),
        "segments": copy.deepcopy(document["segments"]), "units": units,
        "unassigned_questions": unassigned, "issues": issues, "coverage_ledger": ledger,
        "extraction_provenance": copy.deepcopy(provenance), "review_log": [],
    }
    for key in ("source_format", "source_text", "source_text_filename", "source_text_sha256"):
        if key in document:
            draft[key] = copy.deepcopy(document[key])
    return draft


def _invalidate(node):
    for _, item in fields(node):
        item["review"] = "NEEDS_REVIEW" if item["value"] or item.get("not_applicable") else "UNRESOLVED"
        item["source_quotes"] = []


def _unassigned_matches(draft, unit):
    bill = unit["bill"]["value"]
    owners = {label for p in unit["testifiers"] for label in p["speaker_labels"]}
    for question in draft["unassigned_questions"]:
        other_bill = question["bill"]["value"]
        if bill and other_bill and bill != other_bill:
            continue
        respondents = {label for r in question["responses"] for label in r["respondent"]["speaker_labels"]}
        source_ids = {sid for _, item in fields(question) for sid in item["evidence"]}
        if (bill and other_bill and owners and respondents and owners.isdisjoint(respondents)
                and source_ids and set(unit["segment_ids"]).isdisjoint(source_ids)):
            continue
        return True
    return False


def prepare(draft, hide_ambiguous_absence=True):
    """hide_ambiguous_absence only changes the view: a "no questions" decision is shown as open while an
    unassigned exchange might belong to that testimony. Saving keeps it, so it returns once that's resolved."""
    result = copy.deepcopy(draft)
    for unit in result["units"]:
        if unit["questions"]:
            unit["questions_none"] = None
        elif unit.get("questions_none") is None:
            unit["questions_none"] = field(None, [], True)
        elif hide_ambiguous_absence and _unassigned_matches(result, unit):
            unit["questions_none"] = field(None, [], True)
    for question in result["unassigned_questions"] + [q for u in result["units"] for q in u["questions"]]:
        if question["responses"]:
            question["response_resolution"] = None
        else:
            question.setdefault("response_resolution", field(None, [], True))
            if question["response_status"] == "answered":
                question["response_status"] = "unclear"
    return result


def summary(draft):
    draft = prepare(draft)
    values = [value for _, value in fields(draft)]
    unresolved = sum(not item["value"] and not item.get("not_applicable") or item["review"] == "UNRESOLVED" for item in values)
    missing = int(not draft["units"]) + sum(not u["testifiers"] or not u["arguments"] for u in draft["units"])
    approved = sum(item["review"] == "APPROVED" for item in values)
    return {"status": "APPROVED" if values and approved == len(values) and not missing else
            "UNRESOLVED" if unresolved or missing else "NEEDS_REVIEW",
            "fields": len(values), "approved": approved, "unresolved": unresolved + missing,
            "flags": sum(1 for item in values if item.get("flag") and item["review"] != "APPROVED")}


def _text(value, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 50000:
        raise InputError("Use nonempty text, or null for an unknown value.")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise InputError("Use valid UTF-8 correction text.") from error
    return value.strip()


def _evidence(draft, ids, required=False):
    known = {segment["id"] for segment in draft["segments"]}
    if not isinstance(ids, list) or any(not isinstance(sid, str) or sid not in known for sid in ids):
        raise InputError("Choose original transcript segment IDs for evidence.")
    if required and not ids:
        raise InputError("Source-backed repairs need transcript evidence.")
    return list(dict.fromkeys(ids))


def _labels(draft, labels):
    known = {s["speaker"] for s in draft["segments"] if not s.get("speaker_unavailable")}
    if not isinstance(labels, list) or any(label not in known for label in labels):
        raise InputError("Use attributable original speaker labels, or an empty mapping when unavailable.")
    return list(dict.fromkeys(labels))


def _added(draft, value, ids):
    return field(_text(value), _evidence(draft, ids), True)


def lead_flag(sentence, rest):
    """A code-made sentence goes before any other flag text, once, so the length cut never hides it."""
    if not sentence or rest and sentence in rest:
        return rest
    return f"{sentence} {rest}" if rest else sentence


def correct(draft, changes=None, operations=None):
    result = prepare(draft, hide_ambiguous_absence=False)
    changes, operations = changes or [], operations or []
    if not isinstance(changes, list) or not isinstance(operations, list) or not changes and not operations:
        raise InputError("Provide corrections or structural repairs.")
    if len(changes) + len(operations) > 2000:
        raise InputError("Use at most 2,000 corrections per save.")
    available = dict(fields(result))
    typed = []  # Unit bills changed in this save.
    for change in changes:
        if (not isinstance(change, dict) or change.get("path") not in available
                or set(change) - {"path", "value", "review", "not_applicable"}):
            raise InputError("Choose an existing extracted field.")
        path = change["path"]
        item = available[path]
        before = copy.deepcopy(item)
        if "value" in change:
            value = _text(change["value"])
            if path.endswith("/position") and value not in {"Support", "Oppose", "Unclear"}:
                raise InputError("Position must be Support, Oppose or Unclear.")
            if path.endswith("/asker_role") and value not in {None, "legislator", "other"}:
                raise InputError("Question role must be legislator, other or unknown.")
            if re.fullmatch(r"/units/\d+/bill", path) and value:
                value = " / ".join(typed_bills(value) or [value])
                if value != " / ".join(typed_bills(before["value"] or "") or [before["value"] or ""]):
                    typed.append(path)  # Another number, not just "H. 1975" tidied to H.1975.
            item["value"] = value
            item["corrected"] = value != item["original_value"]
            if value != before["value"]:
                item["review"] = "NEEDS_REVIEW" if value else "UNRESOLVED"
                item["basis"] = "human_correction"
                item["source_quotes"] = []  # Original produced quotes remain immutable provenance.
        if "not_applicable" in change:
            if not path.endswith("/organization") or not isinstance(change["not_applicable"], bool):
                raise InputError("Not applicable is available only for optional organizations.")
            item["not_applicable"] = change["not_applicable"]
            if item["not_applicable"] and item["value"]:
                raise InputError("A not-applicable organization must be empty.")
        if path.endswith("/questions_none"):
            raise InputError("Use the explicit reviewed no-questions operation.")
        status = change.get("review", item["review"])
        if status not in REVIEW_STATES:
            raise InputError("Choose an existing review state.")
        if status == "APPROVED" and not item["value"] and not item.get("not_applicable"):
            raise InputError("Unknown values cannot be approved as facts.")
        item["review"] = status
        result["review_log"].append({"op": "correct_field", "path": path, "before": before, "after": copy.deepcopy(item)})
    titles = agenda_titles(result)
    retitled = {c["path"] for c in changes if "value" in c}
    for path in typed:
        item, numbers = available[path], typed_bills(available[path]["value"]) or []
        title = available.get(path + "_title")
        listed = bool(numbers) and all(n in titles for n in numbers)
        if titles and not listed:
            # An off-agenda number keeps its ⚠ even if it was approved in the same save.
            before = copy.deepcopy(item)
            if not (item.get("flag") or "").startswith(NOT_ON_AGENDA):
                item["flag"] = lead_flag(NOT_ON_AGENDA, item.get("flag"))
            item["review"] = "NEEDS_REVIEW" if item["review"] == "APPROVED" else item["review"]
            result["review_log"].append({"op": "agenda_check", "path": path, "before": before,
                                         "after": copy.deepcopy(item)})
        if not listed:
            if title is not None and path + "_title" not in retitled and title["value"]:
                before = copy.deepcopy(title)  # The old title may belong to the old number.
                title.update(review="NEEDS_REVIEW", flag=lead_flag(TITLE_CHANGED, title.get("flag")))
                result["review_log"].append({"op": "bill_changed", "path": path + "_title", "before": before,
                                             "after": copy.deepcopy(title)})
            continue
        if (item.get("flag") or "").startswith(NOT_ON_AGENDA):  # About the number this replaced.
            item["flag"] = item["flag"][len(NOT_ON_AGENDA):].strip()
            if not item["flag"]:
                del item["flag"]
        official = " / ".join(dict.fromkeys(titles[n] for n in numbers))
        if title is not None and path + "_title" not in retitled and title["value"] != official:
            before = copy.deepcopy(title)  # A typed bill takes its official title, flagged until confirmed.
            title.update(value=official, corrected=official != title["original_value"], review="NEEDS_REVIEW",
                         basis="official_agenda", source_quotes=[], flag=AGENDA_TITLE)
            result["review_log"].append({"op": "agenda_title", "path": path + "_title", "before": before,
                                         "after": copy.deepcopy(title)})
    for op in operations:
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise InputError("Use a structured review operation.")
        name, path = op["op"], op.get("path", "")
        try:
            target = pointer(result, path)
        except SourceError as error:
            raise InputError("The repair target no longer exists. Reload the hearing.") from error
        before = copy.deepcopy(target)
        if name == "set_evidence":
            if path not in dict(fields(result)):
                raise InputError("Choose an extracted field for evidence repair.")
            target["evidence"] = _evidence(result, op.get("evidence"), True)
            target["source_quotes"] = []
            target["review"] = "NEEDS_REVIEW" if target["value"] else "UNRESOLVED"
        elif name in {"rich_set_mapping", "rich_set_contributors"}:
            key = "speaker_labels" if name == "rich_set_mapping" else "contributors"
            valid_path = (re.fullmatch(r"/units/\d+/arguments/\d+", path) if name == "rich_set_contributors" else
                          re.fullmatch(r"/(?:units/\d+/testifiers/\d+|(?:units/\d+/questions/\d+|unassigned_questions/\d+)/(?:asker|responses/\d+/respondent))", path))
            if not valid_path or not isinstance(target, dict) or key not in target:
                raise InputError("Choose an identity or argument for attribution repair.")
            target[key] = _labels(result, op.get("labels"))
            if name == "rich_set_mapping" and path.endswith("/asker"):
                exchange = pointer(result, path.rsplit("/", 1)[0])
                _invalidate({k: exchange[k] for k in ("asker", "asker_role", "question", "bill")})
            elif name == "rich_set_mapping" and path.endswith("/respondent"):
                _invalidate(pointer(result, path.rsplit("/", 1)[0]))
            elif name == "rich_set_mapping" and "/testifiers/" in path:
                _invalidate(pointer(result, path.split("/testifiers/", 1)[0]))
            else:
                _invalidate(target)
        elif name == "rich_remove":
            if not re.fullmatch(r"/(?:units/\d+(?:/(?:testifiers|arguments|questions)/\d+(?:/responses/\d+)?)?|unassigned_questions/\d+(?:/responses/\d+)?)", path):
                raise InputError("Choose a testimony, testifier, argument, question or response to remove.")
            parent_path, raw_index = path.rsplit("/", 1)
            parent = pointer(result, parent_path)
            parent.pop(int(raw_index))
            if "/responses/" in path:
                exchange = pointer(result, path.split("/responses/", 1)[0])
                exchange["response_status"] = "unclear"
                exchange["response_resolution"] = field(None, [], True) if not exchange["responses"] else None
                _invalidate(exchange)
            elif "/testifiers/" in path:
                _invalidate(pointer(result, path.split("/testifiers/", 1)[0]))
        elif name == "rich_add_argument":
            if not re.fullmatch(r"/units/\d+/arguments", path):
                raise InputError("Choose a unit's argument list.")
            target.append({"contributors": _labels(result, op.get("labels", [])),
                           "text": _added(result, _text(op.get("value"), nullable=False), _evidence(result, op.get("evidence"), True))})
        elif name == "rich_add_testifier":
            if not re.fullmatch(r"/units/\d+/testifiers", path):
                raise InputError("Choose a unit's testifier list.")
            ids = _evidence(result, op.get("evidence"), True)
            target.append({"speaker_labels": _labels(result, op.get("labels", [])),
                           "name": _added(result, op.get("name"), ids),
                           "organization": _added(result, op.get("organization"), ids)})
            unit = pointer(result, path.rsplit("/", 1)[0])
            if len(target) > 1:
                unit["joint_confirmation"] = field(None, ids, True)
        elif name == "rich_add_unit":
            if path != "/units":
                raise InputError("Add testimony to the hearing's unit list.")
            if op.get("position", "Unclear") not in {"Support", "Oppose", "Unclear"}:
                raise InputError("Position must be Support, Oppose or Unclear.")
            ids = _evidence(result, op.get("evidence"), True)
            target.append({"id": "human-unit-" + uuid.uuid4().hex, "segment_ids": ids,
                           "testifiers": [{"speaker_labels": _labels(result, op.get("labels", [])),
                                          "name": _added(result, op.get("name"), ids),
                                          "organization": _added(result, op.get("organization"), ids)}],
                           "bill": _added(result, op.get("bill"), ids),
                           "bill_title": field(None, [], True), "position": _added(result, op.get("position", "Unclear"), ids),
                           "arguments": [], "questions": [], "question_status": "coverage_incomplete",
                           "questions_none": field(None, [], True), "joint_basis": [], "joint_confirmation": None})
        elif name in {"rich_add_question", "rich_add_response"}:
            ids = _evidence(result, op.get("evidence"), True)
            person = {"speaker_labels": _labels(result, op.get("labels", [])),
                      "name": _added(result, op.get("name"), ids)}
            if name == "rich_add_response":
                if not re.fullmatch(r"/(?:units/\d+/questions/\d+|unassigned_questions/\d+)/responses", path):
                    raise InputError("Choose an exchange's response list.")
                target.append({"respondent": person, "text": _added(result, _text(op.get("value"), nullable=False), ids)})
                exchange = pointer(result, path.rsplit("/", 1)[0])
                exchange["response_status"] = "unclear"
                exchange["response_resolution"] = None
                _invalidate(exchange)
            else:
                if not re.fullmatch(r"/(?:units/\d+/questions|unassigned_questions)", path):
                    raise InputError("Choose a question list.")
                if op.get("role") not in {None, "legislator", "other"}:
                    raise InputError("Question role must be legislator, other or unknown.")
                target.append({"id": "human-question-" + uuid.uuid4().hex, "asker": person,
                               "asker_role": _added(result, op.get("role"), ids),
                               "question": _added(result, _text(op.get("value"), nullable=False), ids),
                               "bill": _added(result, op.get("bill"), ids),
                               "responses": [], "response_status": "unclear"})
        elif name == "rich_move_question":
            if not re.fullmatch(r"/(?:units/\d+/questions|unassigned_questions)/\d+", path):
                raise InputError("Choose a question to reassign.")
            destination = op.get("destination")
            if not isinstance(destination, str) or not re.fullmatch(r"/(?:units/\d+/questions|unassigned_questions)", destination):
                raise InputError("Choose a testimony or the unassigned exchange list.")
            parent, index = path.rsplit("/", 1)
            if destination == parent:
                continue  # Already there: nothing moves, so nothing needs reviewing again.
            copied = copy.deepcopy(target)
            for _, item in fields(copied):
                item["review"] = "NEEDS_REVIEW" if item["value"] else "UNRESOLVED"
            destination_list = pointer(result, destination)
            pointer(result, parent).pop(int(index))
            destination_list.append(copied)
        elif name == "rich_confirm_no_questions":
            if not re.fullmatch(r"/units/\d+", path) or target["questions"]:
                raise InputError("Review no-question absence only for a unit without retained exchanges.")
            ids = _evidence(result, op.get("evidence"), True)
            if not set(ids) & set(target["segment_ids"]):
                raise InputError("Cite this testimony's original source coverage.")
            confirmed = op.get("confirmed")
            if not isinstance(confirmed, bool):
                raise InputError("Explicitly confirm or withdraw the absence decision.")
            if confirmed and _unassigned_matches(result, target):
                raise InputError("Resolve matching or ambiguous unassigned exchanges before confirming no questions.")
            target["questions_none"] = field("None noted during this testimony." if confirmed else None, ids, True)
            target["questions_none"]["review"] = "APPROVED" if confirmed else "UNRESOLVED"
        elif name == "rich_confirm_joint":
            if not re.fullmatch(r"/units/\d+", path) or len(target["testifiers"]) < 2:
                raise InputError("Joint presentation needs at least two retained testifiers.")
            ids = _evidence(result, op.get("evidence"), True)
            if not set(ids) & set(target["segment_ids"]):
                raise InputError("Cite the joint presentation's original source.")
            confirmed = op.get("confirmed")
            if not isinstance(confirmed, bool):
                raise InputError("Explicitly confirm or withdraw joint presentation.")
            target["joint_confirmation"] = field("Reviewed as joint testimony." if confirmed else None, ids, True)
            target["joint_confirmation"]["review"] = "APPROVED" if confirmed else "UNRESOLVED"
        else:
            raise InputError("Unknown rich review operation.")
        result["review_log"].append({"op": name, "path": path, "before": before, "after": copy.deepcopy(target)})
    return prepare(result, hide_ambiguous_absence=False)


def markdown(draft):
    draft = prepare(draft)
    def escape(value):
        return re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", str(value))
    def value(item):
        text = "Not applicable" if item.get("not_applicable") else escape(item["value"]) if item["value"] else "Unknown"
        return text + (" [human corrected]" if item["review"] == "APPROVED" and item.get("corrected") else
                       "" if item["review"] == "APPROVED" else
                       " [unresolved]" if not item["value"] or item["review"] == "UNRESOLVED" else " [draft; review needed]")
    lookup = {s["id"]: s for s in draft["segments"]}
    def evidence(items):
        ids = list(dict.fromkeys(sid for item in items for sid in item["evidence"]))
        return "; ".join(
            (f"Text line {lookup[sid]['source_line']} - Time unavailable" if lookup[sid].get("timing_unavailable")
             else f"{timestamp(lookup[sid]['start'])}-{timestamp(lookup[sid]['end'])}")
            + f" - {escape(sid)}" for sid in ids if sid in lookup) or "Source mapping requires review."
    metadata = draft["hearing"]
    lines = [f"# {escape(metadata['title'])}", "", f"{escape(metadata['date'])} - {escape(metadata['committee'])}",
             "", f"Tags: Hearing Notes; {escape(metadata['committee'])}", "",
             "**Synthetic test hearing.**" if draft["synthetic"] else "**Public hearing draft - verify against original audio.**",
             "", f"Review: {summary(draft)['status']}", f"Source reference: {escape(metadata['source_file'])}", ""]
    if "export_revision" in draft:
        lines += [f"Saved review revision: {escape(draft['export_revision'])}"]
    if draft.get("export_saved_at"):
        lines += [f"Saved at: {escape(draft['export_saved_at'])}"]
    if "export_revision" in draft or draft.get("export_saved_at"):
        lines += [""]
    def exchanges(items):
        rendered = []
        for q in items:
            rendered += [f"Question asker: {value(q['asker']['name'])}", f"Question role: {value(q['asker_role'])}",
                         f"Question: {value(q['question'])}", f"Bill association: {value(q['bill'])}"]
            for response in q["responses"]:
                rendered += [f"Respondent: {value(response['respondent']['name'])}", f"Response: {value(response['text'])}",
                             f"Evidence: {evidence([response['respondent']['name'], response['text']])}"]
            if not q["responses"]:
                rendered += [f"Response unavailable/unclear: {escape(q['response_status'])}",
                             f"Response source review: {value(q['response_resolution'])}"]
            rendered += [f"Evidence: {evidence([q['asker']['name'], q['asker_role'], q['question'], q['bill']])}", ""]
        return rendered
    for unit in draft["units"]:
        lines += [f"## Bill: {value(unit['bill'])}", f"Bill title: {value(unit['bill_title'])}", "",
                  "Testifiers: " + "; ".join(value(person["name"]) for person in unit["testifiers"])]
        for person in unit["testifiers"]:
            lines += [f"Organization ({value(person['name'])}): {value(person['organization'])}",
                      f"Testifier evidence: {evidence([person['name'], person['organization']])}"]
        if unit.get("joint_confirmation"):
            lines += [f"Joint testimony: {value(unit['joint_confirmation'])}"]
        lines += [f"Position: {value(unit['position'])}",
                  f"Bill evidence: {evidence([unit['bill'], unit['bill_title']])}",
                  f"Position evidence: {evidence([unit['position']])}", "", "Main Arguments:", ""]
        for argument in unit["arguments"]:
            labels = ", ".join(argument["contributors"]) or "Attribution unresolved"
            lines += [f"- {value(argument['text'])} ({escape(labels)})",
                      f"Evidence: {evidence([argument['text']])}"]
        if not unit["arguments"]:
            lines += ["- No retained argument; check for omissions."]
        lines += ["", "### Legislator Questions", ""]
        legislators = [q for q in unit["questions"] if q["asker_role"]["value"] == "legislator"]
        lines += exchanges(legislators)
        if not legislators:
            lines += [value(unit["questions_none"]) if unit.get("questions_none") and unit["questions_none"]["review"] == "APPROVED"
                      else "None detected in supplied material; coverage and source review still required.", ""]
        others = [q for q in unit["questions"] if q not in legislators]
        if others:
            lines += ["### Other or unresolved question roles", ""] + exchanges(others)
        lines += ["---", ""]
    if draft["unassigned_questions"]:
        lines += ["## Unassigned exchanges - association requires review", ""] + exchanges(draft["unassigned_questions"])
    if draft["issues"]:
        lines += ["## Review issues", ""] + ["- " + value(issue) for issue in draft["issues"]]
    return "\n".join(lines).rstrip() + "\n"


def open_flags(draft) -> int:
    """AI flags not yet resolved by the reviewer."""
    return sum(1 for _, item in fields(draft) if item.get("flag") and item["review"] != "APPROVED")


def notion(draft):
    """Paste-ready note in the reviewer's format. Only unresolved items carry a ⚠ marker;
    evidence, statuses and internal codes stay in the app and the review export."""
    draft = prepare(draft)

    def text(item, fallback="Unknown"):
        if item is None:
            return fallback + " ⚠"
        if item.get("not_applicable"):
            return ""
        value = item["value"] or fallback
        attention = not item["value"] or item["review"] == "UNRESOLVED" or (item.get("flag") and item["review"] != "APPROVED")
        return value + (" ⚠" if attention else "")

    metadata = draft["hearing"]
    lines = []
    previous_bill = None
    for unit in draft["units"]:
        bill = text(unit["bill"])
        title = text(unit["bill_title"]) if unit["bill_title"] and unit["bill_title"]["value"] else None
        if bill != previous_bill:
            lines += [f"Bill: {bill}" + (f" — {title}" if title else ""), ""]
            previous_bill = bill
        people = []
        for person in unit["testifiers"]:
            organization = text(person["organization"], "")
            people.append(text(person["name"]) + (f" ({organization})" if organization.strip(" ⚠") else
                                                  " ⚠" if organization else ""))
        lines += [f"Testifiers: {' and '.join(people) if len(people) == 2 else '; '.join(people) or 'Unknown ⚠'}", "",
                  f"Position: {text(unit['position'])}", "", "Main Arguments:", ""]
        lines += [f"- {text(argument['text'])}" for argument in unit["arguments"]] or ["- None recorded ⚠"]
        lines += ["", "Legislator Questions:", ""]
        if unit["questions"]:
            for q in unit["questions"]:
                answer = "; ".join(f"{text(r['respondent']['name'])}: {text(r['text'])}" for r in q["responses"])
                resolution = q.get("response_resolution") or {}
                # Approving the No response button's text means the reviewer checked that nothing was answered.
                # A reply typed into that field instead is part of the note, marked ⚠ until approved.
                checked = resolution.get("review") == "APPROVED"
                typed = resolution.get("value") if resolution.get("value") != NO_RESPONSE else None
                if answer:
                    ending = f" — {answer}"
                elif typed:
                    ending = f" — {typed}" + ("" if checked else " ⚠")
                else:
                    ending = "" if checked else " — No response noted ⚠"
                lines.append(f"- {text(q['asker']['name'])}: {text(q['question'])}{ending}")
        else:
            confirmed = unit.get("questions_none") and unit["questions_none"]["review"] == "APPROVED"
            noted = unit.get("questions_none") and unit["questions_none"]["value"]
            lines.append("- None noted during this testimony." + ("" if confirmed or noted else " ⚠"))
        lines += ["", "---", ""]
    for question in draft["unassigned_questions"]:
        lines += [f"- Unassigned question ⚠: {text(question['question'])}"]
    # Hearing-wide checks (possibly missed testimony or bills) stay in the copy until approved.
    checks = [issue["value"] for issue in draft["issues"] if issue["value"] and issue["review"] != "APPROVED"]
    if checks:
        lines += ([""] if lines and lines[-1] else []) + ["⚠ Still to check:", ""] + [f"- {value} ⚠" for value in checks]
    header = [metadata["title"], "", f"{metadata['date']} · {metadata['committee']}",
              f"Tags: Hearing Notes; {metadata['committee']}", ""]
    if any("⚠" in line for line in lines):
        header += ["⚠ = still to check against the audio.", ""]
    return "\n".join(header + lines).rstrip("-\n ") + "\n"
