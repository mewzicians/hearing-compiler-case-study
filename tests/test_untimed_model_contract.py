"""Untimed model packets preserve complete text provenance and absent voices."""
from __future__ import annotations

import copy
import json
import unittest

from hearing_compiler.model_contract import validate_model_draft
from hearing_compiler.model_contract.sources import SourceIndex, SourceError
from hearing_compiler.text_import import normalize_text
from review_helpers import add_missing_issues


def packet():
    document = normalize_text(
        "I am Nia Rowe. I support H.8179.\r\nThe notice must arrive twenty-one days before renewal.",
        {"title": "Fictional untimed model test", "date": "2026-10-06",
         "committee": "Example Committee", "source_file": "fictional.txt"},
        "fictional.txt", synthetic=True)
    context = {"hearing": document["hearing"], "source_profile": "plain-text-utf8",
               "source_document": document, "context_segments": [], "trusted_metadata": [],
               "coverage": {"span_id": "untimed-span-1", "scope": "excerpt",
                            "gaps": [], "complete_testimony_ranges": []}}
    def claim(value, sid=None):
        return {"value": value, "basis": "direct" if value else "missing",
                "missing_reason": None if value else "not_available_in_excerpt",
                "evidence": [{"segment_id": sid, "quote": next(s["text"] for s in document["segments"] if s["id"] == sid)}] if sid else [],
                "metadata_refs": []}
    output = {"contract": "hearing-extraction-draft/v1", "hearing_id": document["hearing"]["id"],
              "input_span_id": "untimed-span-1", "unassigned_questions": [], "review_issues": [],
              "coverage_ledger": [], "testimony_units": [{
                  "unit_id": "unit-txt0001-1", "segment_ids": ["txt0001", "txt0002"],
                  "testifiers": [{"speaker_labels": [], "name": claim("Nia Rowe", "txt0001"),
                                 "organization": claim(None)}], "joint_basis": [],
                  "bill_id": claim("H.8179", "txt0001"), "bill_title": claim(None),
                  "position": claim("Support", "txt0001"),
                  "arguments": [{"contributors": [], "text": claim("The notice must arrive twenty-one days before renewal.", "txt0002")}],
                  "questions": [], "question_status": "coverage_incomplete"}]}
    for sid in ("txt0001", "txt0002"):
        output["coverage_ledger"].append({"segment_id": sid, "categories": ["testimony"],
                                          "record_refs": ["/testimony_units/0"], "reason": None})
    add_missing_issues(output)
    for target in ("/testimony_units/0/testifiers/0", "/testimony_units/0/arguments/0"):
        output["review_issues"].append({"target": target, "kind": "contextual",
                                       "reason": "Text blocks do not establish attributable voices.", "evidence": []})
    return document, context, output


class UntimedModelContractTests(unittest.TestCase):
    def validate(self, document, context, output):
        return validate_model_draft(json.dumps(output), context, document["segments"])

    def test_valid_untimed_profile_needs_no_invented_clocks_or_speaker_turns(self):
        document, context, output = packet()
        before = copy.deepcopy((document, context, output))
        result = self.validate(document, context, output)
        self.assertEqual(result["output_status"], "VALID", result)
        self.assertEqual(result["semantic_status"], "NOT_EVALUATED")
        self.assertEqual(before, (document, context, output))
        self.assertEqual(SourceIndex(context, document["segments"]).speakers, set())

    def test_unavailable_block_labels_cannot_be_model_voice_labels(self):
        document, context, output = packet()
        output["testimony_units"][0]["testifiers"][0]["speaker_labels"] = [document["segments"][0]["speaker"]]
        result = self.validate(document, context, output)
        self.assertEqual(result["output_status"], "INVALID")
        self.assertTrue(any(d["rule_id"] == "V-IDENTITY" for d in result["diagnostics"]))

    def test_forged_time_hash_excerpt_or_text_cannot_enter_untimed_packet(self):
        for change in ("clock", "hash", "source", "subset"):
            document, context, output = packet()
            if change == "clock":
                document["segments"][0].update(start=0, end=1)
            elif change == "hash":
                document["source_text_sha256"] = "wrong"
            elif change == "source":
                document["segments"][0]["text"] = "Invented replacement."
            else:
                document["segments"] = document["segments"][1:]
            result = self.validate(document, context, output)
            self.assertEqual(result["packet_status"], "INVALID", (change, result))
            self.assertEqual(result["output_status"], "NOT_EVALUATED")

    def test_missing_explicit_profile_does_not_silently_accept_null_times(self):
        document, context, output = packet()
        context.pop("source_profile")
        self.assertEqual(self.validate(document, context, output)["packet_status"], "INVALID")

    def test_escaped_lone_surrogate_is_rejected_before_a_model_claim_can_be_saved(self):
        document, context, output = packet()
        output["testimony_units"][0]["testifiers"][0]["name"]["value"] = "\ud800"
        result = self.validate(document, context, output)
        self.assertEqual(result["output_status"], "INVALID")
        self.assertTrue(any(d["rule_id"] == "V-JSON" for d in result["diagnostics"]))

    def test_subset_uses_verified_full_original_and_id_based_declared_coverage(self):
        document, context, _ = packet()
        index = SourceIndex(context, [document["segments"][1]])
        self.assertEqual(index.current_ids, ["txt0002"])
        self.assertFalse(index.complete(["txt0002"]))
        context["coverage"]["complete_testimony_ranges"] = [{"start": "txt0001", "end": "txt0002"}]
        context["context_segments"] = [document["segments"][0]]
        index = SourceIndex(context, [document["segments"][1]])
        self.assertTrue(index.complete(["txt0002"]))
        context["coverage"]["complete_testimony_ranges"][0] = {"start": "txt0002", "end": "txt0001"}
        with self.assertRaises(SourceError):
            SourceIndex(context, document["segments"])
