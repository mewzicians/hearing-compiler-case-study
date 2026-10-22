"""Generation shape constraints; offline validation and source review still apply."""
from __future__ import annotations


def draft_schema(source):
    absence_possible = bool(source.coverage.get("complete_testimony_ranges")) and not source.coverage.get("gaps")
    def record(properties):
        return {"type": "object", "properties": properties, "required": list(properties),
                "additionalProperties": False}
    def array(items):
        return {"type": "array", "items": items}
    text = {"type": "string"}
    nullable = {"type": ["string", "null"]}
    evidence = record({"segment_id": {"type": "string", "enum": list(source.records)}, "quote": text})
    labels = array({"type": "string", "enum": sorted(source.speakers)}) if source.speakers else {"type": "array", "maxItems": 0}
    claim = record({"value": nullable, "basis": {"enum": ["direct", "contextual", "missing"]},
                    "missing_reason": {"enum": [None, "not_stated", "unclear", "not_available_in_excerpt"]},
                    "evidence": array(evidence), "metadata_refs": array(text)})
    identity = record({"speaker_labels": labels, "name": claim})
    testifier = record({**identity["properties"], "organization": claim})
    question = record({
        "asker": identity, "asker_role": {**claim, "properties": {**claim["properties"], "value": {"enum": ["legislator", "other", None]}}},
        "question": claim, "bill_id": claim,
        "responses": array(record({"respondent": identity, "text": claim})),
        "response_status": {"enum": ["answered", "unclear", "not_available_in_excerpt"] +
                            (["no_response_detected"] if absence_possible else [])}})
    unit = record({
        "unit_id": text, "segment_ids": array({"type": "string", "enum": source.current_ids}),
        "testifiers": array(testifier), "joint_basis": array(evidence),
        "bill_id": claim, "bill_title": claim,
        "position": {**claim, "properties": {**claim["properties"], "value": {"enum": ["Support", "Oppose", "Unclear"]}}},
        "arguments": array(record({"contributors": labels, "text": claim})), "questions": array(question),
        "question_status": {"enum": ["present", "coverage_incomplete"] +
                            (["none_detected_in_supplied_material"] if absence_possible else [])}})
    return record({
        "contract": {"const": "hearing-extraction-draft/v1"},
        "hearing_id": {"const": source.hearing_id}, "input_span_id": {"const": source.span_id},
        "testimony_units": array(unit), "unassigned_questions": array(question),
        "review_issues": array(record({
            "target": text, "kind": {"enum": ["contextual", "missing", "conflict", "transcription", "coverage"]},
            "reason": text, "evidence": array(evidence)})),
        "coverage_ledger": array(record({
            "segment_id": {"type": "string", "enum": source.current_ids},
            "categories": array({"enum": ["testimony", "question", "response", "procedural", "unclear"]}),
            "record_refs": array(text), "reason": nullable}))})
