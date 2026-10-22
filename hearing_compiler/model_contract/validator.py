"""Shared offline structural validator. VALID never certifies semantic truth."""
from __future__ import annotations

import re
from .sources import SourceError, SourceIndex, strict_json, pointer

ROOT_KEYS = {"contract", "hearing_id", "input_span_id", "testimony_units", "unassigned_questions", "review_issues", "coverage_ledger"}
CLAIM_KEYS = {"value", "basis", "missing_reason", "evidence", "metadata_refs"}
UNIT_KEYS = {"unit_id", "segment_ids", "testifiers", "joint_basis", "bill_id", "bill_title", "position", "arguments", "questions", "question_status"}
QUESTION_KEYS = {"asker", "asker_role", "question", "bill_id", "responses", "response_status"}
REASONS = {"not_stated", "unclear", "not_available_in_excerpt"}


def validate_model_draft(candidate_json, hearing_context, current_segments):
    try:
        return _validate(candidate_json, hearing_context, current_segments)
    except (TypeError, KeyError, AttributeError, IndexError) as error:
        return {"packet_status": "VALID", "output_status": "INVALID",
                "semantic_status": "NOT_EVALUATED", "diagnostics": [{
                    "rule_id": "V-JSON", "path": "", "source_ids": [],
                    "message": f"Malformed nested field: {type(error).__name__}"}]}


def _validate(candidate_json, hearing_context, current_segments):
    diagnostics = []
    result = {"packet_status": "VALID", "output_status": "INVALID",
              "semantic_status": "NOT_EVALUATED", "diagnostics": diagnostics}

    def fail(rule, path, message, ids=()):
        diagnostics.append({"rule_id": rule, "path": path, "source_ids": list(ids), "message": message})

    try:
        source = SourceIndex(hearing_context, current_segments)
    except (SourceError, TypeError, KeyError) as error:
        fail("V-SOURCE", "", str(error))
        result.update(packet_status="INVALID", output_status="NOT_EVALUATED")
        return result
    try:
        output = strict_json(candidate_json)
    except SourceError as error:
        fail("V-JSON", "", str(error))
        return result

    def shape(value, keys, path):
        if not isinstance(value, dict) or set(value) != keys:
            fail("V-JSON", path, f"Required exact record keys: {sorted(keys)}")
            return False
        return True

    def array(value, path):
        if not isinstance(value, list):
            fail("V-JSON", path, "Expected array.")
            return []
        return value

    def evidence(entries, path):
        for i, item in enumerate(array(entries, path)):
            try:
                source.evidence(item)
            except (SourceError, TypeError, KeyError) as error:
                fail("V-EVIDENCE", f"{path}/{i}", str(error))

    def labels(entries, path, allow_empty=False):
        values = array(entries, path)
        if any(not isinstance(v, str) or v not in source.speakers for v in values) or len(set(v for v in values if isinstance(v, str))) != len(values):
            fail("V-IDENTITY", path, "Labels must be unique original speaker labels.")
        if not values and not allow_empty:
            fail("V-IDENTITY", path, "Missing attributable speaker label.")
        return values

    if not shape(output, ROOT_KEYS, ""):
        return result
    if output["contract"] != "hearing-extraction-draft/v1":
        fail("V-JSON", "/contract", "Wrong contract.")
    if output["hearing_id"] != source.hearing_id or output["input_span_id"] != source.span_id:
        fail("V-IDS", "", "Supplied hearing/span IDs must be copied.")
    issues, issue_targets = array(output["review_issues"], "/review_issues"), set()
    for i, issue in enumerate(issues):
        path = f"/review_issues/{i}"
        if not shape(issue, {"target", "kind", "reason", "evidence"}, path):
            continue
        if issue["kind"] not in {"contextual", "missing", "conflict", "transcription", "coverage"}:
            fail("V-REVIEW", path + "/kind", "Unknown issue kind.")
        if not isinstance(issue["reason"], str) or not issue["reason"]:
            fail("V-REVIEW", path + "/reason", "Issue needs a brief source-based reason.")
        try:
            pointer(output, issue["target"])
            issue_targets.add(issue["target"])
        except (SourceError, TypeError):
            fail("V-POINTER", path + "/target", "Issue pointer does not resolve.")
        evidence(issue["evidence"], path + "/evidence")

    def needs_issue(path, reason):
        if path not in issue_targets:
            fail("V-REVIEW", path, reason)

    def claim(value, path, metadata=False, allowed_values=None):
        if not shape(value, CLAIM_KEYS, path):
            return
        text, basis, missing = value["value"], value["basis"], value["missing_reason"]
        if text is not None and (not isinstance(text, str) or not text):
            fail("V-CLAIM", path + "/value", "Value must be nonempty text or null.")
        if allowed_values is not None and text is not None and text not in allowed_values:
            fail("V-CLAIM", path + "/value", "Unknown field enum.")
        if basis not in {"direct", "contextual", "missing"}:
            fail("V-CLAIM", path + "/basis", "Unknown claim basis.")
        position = path.endswith("/position")
        if basis == "missing":
            if text is not None and not (position and text == "Unclear"):
                fail("V-CLAIM", path, "Missing claim needs null, except position Unclear.")
            if missing not in REASONS:
                fail("V-CLAIM", path + "/missing_reason", "Missing claim needs a defined reason.")
        elif text is None or missing is not None:
            fail("V-CLAIM", path, "Supported claim needs value and null missing reason.")
        evidence(value["evidence"], path + "/evidence")
        refs = array(value["metadata_refs"], path + "/metadata_refs")
        for ref in refs:
            registered = source.metadata.get(ref) if isinstance(ref, str) else None
            if not metadata or registered is None or registered["purpose"] != "bill_metadata":
                fail("V-METADATA", path, "Metadata reference not allowed in this field.")
        if basis != "missing" and not value["evidence"] and not refs:
            fail("V-EVIDENCE", path, "Supported claim needs original evidence or permitted metadata.")
        if basis in {"missing", "contextual"} or position and text == "Unclear":
            needs_issue(path, "Every missing/contextual/Unclear claim needs its own issue.")

    def identity(value, path, testifier=False):
        keys = {"speaker_labels", "name"} | ({"organization"} if testifier else set())
        if not shape(value, keys, path):
            return
        mapping = labels(value["speaker_labels"], path + "/speaker_labels", allow_empty=True)
        claim(value["name"], path + "/name")
        if not mapping:
            needs_issue(path, "Named member without attributable voice needs a mapping issue.")
        if testifier:
            claim(value["organization"], path + "/organization")

    def question(value, path, unit_ids):
        if not shape(value, QUESTION_KEYS, path):
            return
        identity(value["asker"], path + "/asker")
        claim(value["asker_role"], path + "/asker_role", allowed_values={"legislator", "other"})
        claim(value["question"], path + "/question")
        claim(value["bill_id"], path + "/bill_id", metadata=True)
        responses = array(value["responses"], path + "/responses")
        for j, response in enumerate(responses):
            rp = f"{path}/responses/{j}"
            if shape(response, {"respondent", "text"}, rp):
                identity(response["respondent"], rp + "/respondent")
                claim(response["text"], rp + "/text")
        status = value["response_status"]
        if status not in {"answered", "no_response_detected", "unclear", "not_available_in_excerpt"}:
            fail("V-QA", path + "/response_status", "Unknown response status.")
        if status == "answered" and not responses:
            fail("V-QA", path, "Answered requires actual response records.")
        if status in {"no_response_detected", "not_available_in_excerpt"} and responses:
            fail("V-QA", path, "Unanswered/truncated exchange cannot contain a fabricated response.")
        qids = [e.get("segment_id") for e in value["question"].get("evidence", []) if isinstance(e, dict)] if isinstance(value["question"], dict) else []
        if status == "no_response_detected" and not source.complete(unit_ids or qids):
            fail("V-QA", path, "No-response detection requires relevant complete declared coverage.")

    units, unit_ids = array(output["testimony_units"], "/testimony_units"), set()
    current_positions = {sid: i for i, sid in enumerate(source.current_ids)}
    for i, unit in enumerate(units):
        path = f"/testimony_units/{i}"
        if not shape(unit, UNIT_KEYS, path):
            continue
        uid = unit["unit_id"]
        if not isinstance(uid, str) or uid in unit_ids:
            fail("V-IDS", path + "/unit_id", "Unit IDs must be unique strings.")
        if isinstance(uid, str):
            unit_ids.add(uid)
        ids = array(unit["segment_ids"], path + "/segment_ids")
        valid_ids = all(isinstance(s, str) and s in current_positions for s in ids)
        if not ids or not valid_ids or len(set(s for s in ids if isinstance(s, str))) != len(ids):
            fail("V-IDS", path + "/segment_ids", "Unit needs unique current source IDs.")
        elif [current_positions[s] for s in ids] != sorted(current_positions[s] for s in ids):
            fail("V-IDS", path + "/segment_ids", "Unit IDs must retain source order.")
        if ids and isinstance(uid, str) and (not uid.startswith(f"unit-{ids[0]}-") or not re.fullmatch(r"[1-9][0-9]*", uid[len(f"unit-{ids[0]}-"):])):
            fail("V-IDS", path + "/unit_id", "Unit must use first current ID and positive occurrence.")
        testifiers = array(unit["testifiers"], path + "/testifiers")
        if not testifiers:
            fail("V-IDENTITY", path + "/testifiers", "Testimony needs a testifier representation.")
        for j, person in enumerate(testifiers):
            identity(person, f"{path}/testifiers/{j}", True)
        evidence(unit["joint_basis"], path + "/joint_basis")
        if len(testifiers) > 1 and not unit["joint_basis"]:
            fail("V-IDENTITY", path + "/joint_basis", "Joint members require source presentation evidence.")
        claim(unit["bill_id"], path + "/bill_id", metadata=True)
        claim(unit["bill_title"], path + "/bill_title", metadata=True)
        claim(unit["position"], path + "/position", allowed_values={"Support", "Oppose", "Unclear"})
        for j, argument in enumerate(array(unit["arguments"], path + "/arguments")):
            ap = f"{path}/arguments/{j}"
            if shape(argument, {"contributors", "text"}, ap):
                if not labels(argument["contributors"], ap + "/contributors", allow_empty=True):
                    needs_issue(ap, "Empty contributor mapping needs a targeted issue.")
                claim(argument["text"], ap + "/text")
        questions = array(unit["questions"], path + "/questions")
        for j, item in enumerate(questions):
            question(item, f"{path}/questions/{j}", ids if valid_ids else [])
        status = unit["question_status"]
        if status not in {"present", "none_detected_in_supplied_material", "coverage_incomplete"}:
            fail("V-QA", path + "/question_status", "Unknown question detection status.")
        if bool(questions) != (status == "present"):
            fail("V-QA", path, "Question presence and detection status contradict.")
        if status == "none_detected_in_supplied_material" and not source.complete(ids if valid_ids else []):
            fail("V-COVERAGE", path, "Detection without questions needs a complete gap-free range.")
    for i, item in enumerate(array(output["unassigned_questions"], "/unassigned_questions")):
        question(item, f"/unassigned_questions/{i}", [])
    ledger = array(output["coverage_ledger"], "/coverage_ledger")
    def cited_ids(record):
        if isinstance(record, dict):
            if set(record) == {"segment_id", "quote"}:
                return {record["segment_id"]}
            return set().union(*(cited_ids(v) for v in record.values())) if record else set()
        if isinstance(record, list):
            return set().union(*(cited_ids(v) for v in record)) if record else set()
        return set()
    seen = []
    for i, entry in enumerate(ledger):
        path = f"/coverage_ledger/{i}"
        if not shape(entry, {"segment_id", "categories", "record_refs", "reason"}, path):
            continue
        seen.append(entry["segment_id"])
        categories = array(entry["categories"], path + "/categories")
        if not categories or any(v not in {"testimony", "question", "response", "procedural", "unclear"} for v in categories):
            fail("V-COVERAGE", path, "Unknown/missing coverage category.")
        if any(v in {"testimony", "question", "response"} for v in categories) and not entry["record_refs"]:
            fail("V-COVERAGE", path, "Substantive coverage needs an extraction record reference.")
        if {"procedural", "unclear"} & set(v for v in categories if isinstance(v, str)) and (not isinstance(entry["reason"], str) or not entry["reason"]):
            fail("V-COVERAGE", path + "/reason", "Exclusions and uncertainty need a reason.")
        linked_ids = set()
        for j, ref in enumerate(array(entry["record_refs"], path + "/record_refs")):
            try:
                resolved = pointer(output, ref)
                if not isinstance(ref, str) or not ref.startswith(("/testimony_units/", "/unassigned_questions/")) or not isinstance(resolved, dict):
                    raise SourceError("Coverage reference is not an extraction record/claim.")
                linked_ids.update(cited_ids(resolved))
            except (SourceError, TypeError):
                fail("V-POINTER", f"{path}/record_refs/{j}", "Invalid extraction record reference.")
        if (any(v in {"testimony", "question", "response"} for v in categories)
                and entry["segment_id"] not in linked_ids):
            fail("V-COVERAGE", path, "Referenced records do not cite this original segment.")
    if len(seen) != len(source.current_ids) or set(s for s in seen if isinstance(s, str)) != set(source.current_ids):
        fail("V-COVERAGE", "/coverage_ledger", "Exactly one entry per current original ID is required.")
    result["output_status"] = "INVALID" if diagnostics else "VALID"
    return result
