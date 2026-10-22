"""Structural saved-state checks shared by loading and full-backup restoration."""
from __future__ import annotations

import re

from .pipeline import InputError, REVIEW_STATES, fields, markdown, prepare_review, review_summary, validate

STAGES = {"Complete", "Failed", "Waiting", "Awaiting transcript", "Analyzing", "Transcribing"}


def _require(condition):
    if not condition:
        raise InputError("A saved hearing has malformed runtime metadata or review structure.")


def _person(person, speakers, organization=False):
    _require(isinstance(person, dict) and "name" in person
             and isinstance(person.get("speaker_labels"), list)
             and all(isinstance(s, str) and s in speakers for s in person["speaker_labels"]))
    if organization:
        _require("organization" in person)


def _rich(draft, ids, speakers):
    _require(all(isinstance(draft.get(key), list) for key in
                 ("units", "unassigned_questions", "issues", "coverage_ledger", "review_log")))
    unit_ids, question_ids = set(), set()
    questions = list(draft["unassigned_questions"])
    for unit in draft["units"]:
        _require(isinstance(unit, dict) and isinstance(unit.get("id"), str) and unit["id"]
                 and unit["id"] not in unit_ids
                 and isinstance(unit.get("segment_ids"), list)
                 and all(isinstance(s, str) and s in ids for s in unit["segment_ids"])
                 and all(isinstance(unit.get(k), list) for k in ("testifiers", "arguments", "questions", "joint_basis"))
                 and all(k in unit for k in ("bill", "bill_title", "position", "question_status")))
        unit_ids.add(unit["id"])
        _require(unit["question_status"] in {"present", "none_detected_in_supplied_material", "coverage_incomplete"})
        for person in unit["testifiers"]:
            _person(person, speakers, True)
        for argument in unit["arguments"]:
            _require(isinstance(argument, dict) and "text" in argument
                     and isinstance(argument.get("contributors"), list)
                     and all(isinstance(s, str) and s in speakers for s in argument["contributors"]))
        questions.extend(unit["questions"])
    for question in questions:
        _require(isinstance(question, dict) and isinstance(question.get("id"), str)
                 and question["id"] and question["id"] not in question_ids
                 and all(k in question for k in ("asker", "asker_role", "question", "bill"))
                 and isinstance(question.get("responses"), list)
                 and question.get("response_status") in {"answered", "no_response_detected", "not_available_in_excerpt", "unclear"})
        question_ids.add(question["id"])
        _person(question["asker"], speakers)
        for response in question["responses"]:
            _require(isinstance(response, dict) and "text" in response and "respondent" in response)
            _person(response["respondent"], speakers)


def validate_saved_state(state):
    """Validate usability and source identity, never certify semantic accuracy."""
    try:
        _require(isinstance(state, dict) and type(state.get("schema_version")) is int
                 and state["schema_version"] == 1 and isinstance(state.get("jobs"), dict))
        for job_id, job in state["jobs"].items():
            _require(isinstance(job_id, str) and job_id and isinstance(job, dict)
                     and job.get("id") == job_id and type(job.get("revision")) is int
                     and job["revision"] >= 0 and job.get("processing") in STAGES
                     and isinstance(job.get("hearing"), dict)
                     and all(isinstance(job["hearing"].get(k), str) for k in
                             ("id", "title", "date", "committee", "source_file"))
                     and isinstance(job.get("fingerprint"), str)
                     and re.fullmatch(r"[0-9a-f]{64}", job["fingerprint"])
                     and isinstance(job.get("updated_at"), str)
                     and "input" in job and (job["input"] is None or isinstance(job["input"], dict))
                     and "draft" in job and (job["draft"] is None or isinstance(job["draft"], dict))
                     and "error" in job and (job["error"] is None or isinstance(job["error"], str)))
            if job.get("audio") is not None:
                asset = job["audio"]
                _require(isinstance(asset, dict) and isinstance(asset.get("path"), str)
                         and re.fullmatch(r"audio/[0-9a-f]{64}\.(?:mp3|wav|m4a)", asset["path"])
                         and type(asset.get("bytes")) is int and asset["bytes"] > 0
                         and isinstance(asset.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
                         and asset["path"].split("/")[1].split(".")[0] == asset["sha256"]
                         and isinstance(asset.get("filename"), str) and isinstance(asset.get("content_type"), str))
            if job["processing"] == "Complete" or job["draft"] is not None:
                _require(job["draft"] is not None)
                original = validate(job["input"])
                draft = prepare_review(job["draft"])
                # Attaching audio intentionally updates the displayed source
                # filename while preserving the original imported document.
                valid_filenames = {original["hearing"]["source_file"]}
                if job.get("audio"):
                    valid_filenames.add(job["audio"]["filename"])
                _require(job["hearing"] == draft["hearing"]
                         and all(job["hearing"][k] == original["hearing"][k] for k in ("id", "title", "date", "committee"))
                         and job["hearing"]["source_file"] in valid_filenames
                         and original["hearing"]["id"] == job_id
                         and draft["segments"] == original["segments"]
                         and isinstance(draft.get("synthetic"), bool))
                ids = {s["id"] for s in original["segments"]}
                speakers = {s["speaker"] for s in original["segments"] if not s.get("speaker_unavailable")}
                if draft.get("draft_format") == "hearing-review-units/v1":
                    _rich(draft, ids, speakers)
                else:
                    _require(isinstance(draft.get("testimonies"), list) and isinstance(draft.get("questions"), list))
                for _, item in fields(draft):
                    _require(isinstance(item.get("value"), str) or item.get("value") is None)
                    _require("original_value" in item and (isinstance(item["original_value"], str) or item["original_value"] is None)
                             and item.get("review") in REVIEW_STATES
                             and isinstance(item.get("evidence"), list)
                             and all(isinstance(s, str) and s in ids for s in item["evidence"]))
                review_summary(draft)
                markdown(draft)  # Check all required rendering fields before promotion.
        return state
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError("A saved hearing has malformed runtime metadata or review structure.") from error
