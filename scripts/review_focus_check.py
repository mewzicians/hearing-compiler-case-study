"""Focused review navigation checks in a disposable synthetic workspace."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from scripts.audit_reporting import audit_main, browser_setup


def body(run, argv):
    audit = browser_setup(run, argv)
    from playwright.sync_api import expect, sync_playwright
    from hearing_compiler.server import make_server
    from hearing_compiler.pipeline import extract
    from hearing_compiler.rich_review import from_packets
    from hearing_compiler.text_import import normalize_text
    from test_rich_review import joint_packet
    document, packet = joint_packet()
    # Independently retained source, with one uncited material point and one
    # uncited procedural point: both are candidates, neither is auto-classified.
    document["segments"] += [
        {"id": "attention-example", "start": 40, "end": 44, "speaker": "Voice A",
         "text": "An overlooked example: one family returned twice for a corrected notice."},
        {"id": "attention-procedure", "start": 45, "end": 49, "speaker": "Voice C",
         "text": "Please move the microphone closer."},
    ]
    packet["context"]["coverage"]["scope"] = "excerpt"
    packet["current"] = copy.deepcopy(document["segments"])
    packet["output"]["coverage_ledger"] += [
        {"segment_id": "attention-example", "categories": ["unclear"], "record_refs": [],
         "reason": "Source retained for review; its draft extraction is missing."},
        {"segment_id": "attention-procedure", "categories": ["procedural"], "record_refs": [],
         "reason": "Microphone procedure, retained original."},
    ]
    original = copy.deepcopy(document)
    rich = from_packets(document, [packet], {})
    rich["issues"].append({"value": "Check the notice example.", "evidence": ["attention-example"],
                           "review": "NEEDS_REVIEW", "original_value": "Check the notice example.",
                           "corrected": False})
    rich["units"][0]["bill_title"]["value"] = "Uncited draft title"
    rich["units"][0]["bill_title"]["review"] = "NEEDS_REVIEW"
    def extractor(source):
        return copy.deepcopy(rich) if source["hearing"]["id"] == document["hearing"]["id"] else extract(source)
    server = make_server(audit / "state", 0, extraction_provider=extractor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def stop():
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()
    run.cleanup(stop)
    imported = server.store.import_one(document)
    initial_saved = server.store.get(document["hearing"]["id"])["draft"]
    errors = []
    run.data["browser_errors"] = errors
    for name in ("web/review_focus.js", "web/app.js", "web/rich.js"):
        run.identity("inputs", name, ROOT / name)
    run.data["limits"] = "Synthetic UI behavior only; source review remains human-controlled. No model accuracy or recipient review-time claim."
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("HEARING_TEST_BROWSER"), headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.locator(f'[data-open="{document["hearing"]["id"]}"]').click()
        expect(page.locator(".review-focus")).to_be_visible()
        inventory = page.evaluate("reviewFocusInventory(state.job.draft)")
        assert "attention-example" in inventory["unreferenced"], inventory
        assert "attention-procedure" in inventory["unreferenced"], inventory
        assert any(c["path"] == "/units/0/bill_title" for c in inventory["candidates"]), inventory
        assert any(c["path"].startswith("/issues/") for c in inventory["candidates"]), inventory
        before_review = server.store.get(document["hearing"]["id"])["draft"]
        assert before_review == initial_saved, "Read-only attention view changed saved draft."
        run.data["checks"].append("Specific issues and uncited fields visible; issue/coverage pointers don't hide uncited material or procedure; no state mutation")
        page.locator("#transcript-filter").select_option("unreferenced")
        expect(page.locator('[data-segment="attention-example"]')).to_be_visible()
        expect(page.locator('[data-segment="attention-procedure"]')).to_be_visible()
        expect(page.locator('[data-segment="joint1"]')).to_be_hidden()
        page.locator('[data-evidence="joint1"]').first.click()
        expect(page.locator("#transcript-filter")).to_have_value("all")
        expect(page.locator('[data-segment="joint1"]')).to_be_visible()
        run.data["checks"].append("Unreferenced filter preserves original order; evidence navigation reveals referenced passages")
        page.locator(".review-focus summary").click()
        page.locator('[data-focus-field="/units/0/bill_title"]').click()
        assert page.locator('[data-edit="/units/0/bill_title"]').evaluate("node => node === document.activeElement")
        page.locator('[data-edit="/units/0/bill_title"]').click()
        expect(page.locator("#edit-dialog")).to_be_visible()
        page.locator("#cancel-edit").click()
        page.locator('[data-focus-field="/units/0/bill_title"]').focus()
        updated = server.store.get(document["hearing"]["id"])
        server.store.review(updated["id"], updated["revision"], changes=[{
            "path": "/units/0/bill_title", "value": "Externally saved title", "review": "NEEDS_REVIEW"}])
        expect(page.locator('[data-field="/units/0/bill_title"]')).to_contain_text("Externally saved title")
        assert page.locator(".review-focus details").evaluate("node => node.open")
        assert page.locator('[data-focus-field="/units/0/bill_title"]').evaluate("node => node === document.activeElement")
        page.locator("#transcript-filter").select_option("unreferenced")
        page.locator("#transcript-filter").focus()
        updated = server.store.get(document["hearing"]["id"])
        server.store.review(updated["id"], updated["revision"], changes=[{
            "path": "/units/0/bill_title", "value": "Second external title", "review": "NEEDS_REVIEW"}])
        expect(page.locator('[data-field="/units/0/bill_title"]')).to_contain_text("Second external title")
        assert page.locator("#transcript-filter").evaluate("node => node === document.activeElement")
        expect(page.locator("#transcript-filter")).to_have_value("unreferenced")
        page.locator("#transcript-filter").select_option("all")
        run.data["checks"].append("Focus links reach existing correction controls by keyboard-compatible button navigation")
        run.data["checks"].append("Actual revision polling preserves expanded attention and candidate/filter keyboard focus")
        page.get_by_role("button", name="Add argument", exact=True).first.click()
        page.locator("#rich-value").fill("A family returned twice for a corrected notice.")
        page.locator("#rich-labels").fill("Voice A")
        page.locator('[data-evidence-choice="rich-evidence"][value="attention-example"]').check()
        page.locator("#save-structure").click()
        expect(page.locator("#structure-dialog")).not_to_be_visible()
        inventory = page.evaluate("reviewFocusInventory(state.job.draft)")
        assert "attention-example" not in inventory["unreferenced"]
        assert "attention-procedure" in inventory["unreferenced"]
        saved = server.store.get(document["hearing"]["id"])
        assert saved["input"] == original
        assert saved["review"]["status"] != "APPROVED"
        page.locator('[data-view="output"]').click()
        expect(page.locator(".document-preview")).to_contain_text("A family returned twice")
        page.goto(f"http://127.0.0.1:{server.server_port}/#hearings")
        page.locator(f'[data-open="{document["hearing"]["id"]}"]').click()
        assert "attention-example" not in page.evaluate("reviewFocusInventory(state.job.draft).unreferenced")
        assert server.store.get(document["hearing"]["id"])["draft"] == saved["draft"]
        run.data["checks"].append("Source-backed omitted-argument repair updates attention, survives reload and reaches output without source change or auto-approval")
        # Pure inventory corruptions: original historic evidence and broad unit
        # segment lists must not suppress attention.
        result = page.evaluate("""() => {
          const draft = structuredClone(state.job.draft);
          draft.review_log = [{before: {value: "old", evidence: ["attention-procedure"], review: "APPROVED"}}];
          draft.units[0].segment_ids.push("attention-procedure");
          draft.coverage_ledger.push({entries:[{segment_id:"attention-procedure",categories:["testimony"],record_refs:["/units/0"]}]});
          return reviewFocusInventory(draft);
        }""")
        assert "attention-procedure" in result["unreferenced"]
        run.data["checks"].append("Historic evidence, broad unit ranges and coverage ledgers do not certify substantive source use")
        updated = server.store.get(document["hearing"]["id"])
        server.store.review(updated["id"], updated["revision"], operations=[{
            "op": "rich_remove", "path": "/units/0/questions/0"}])
        expect(page.locator('[data-focus-target="/units/0/questions_none"]')).to_be_visible()
        if not page.locator(".review-focus details").evaluate("node => node.open"):
            page.locator(".review-focus summary").click()
        page.locator('[data-focus-field="/units/0/questions_none"]').click()
        expect(page.locator('[data-focus-target="/units/0/questions_none"]')).to_be_in_viewport()
        # The testimony's one-click No questions control replaced the old confirmation dialog.
        page.locator('#review-view [data-no-questions="0"]').click()
        expect(page.locator('[data-focus-target="/units/0/questions_none"]')).to_contain_text("No questions confirmed")
        absence = server.store.get(document["hearing"]["id"])["draft"]["units"][0]["questions_none"]
        assert absence["value"] == "None noted during this testimony." and absence["review"] == "APPROVED", absence
        run.data["checks"].append("Rich question-absence attention scrolls to the testimony, whose one-click No questions "
                                  "control records a reviewed absence")
        text = normalize_text(
            "I am Ada Field with Fictional Civic Office. I support H.9009.\n\nA separate plain-text paragraph may contain missed material.",
            {"id": "attention-txt", "title": "Fictional untimed attention", "date": "2026-10-09",
             "committee": "Fictional Civic Committee"}, "fictional.txt", synthetic=True)
        server.store.import_one(text)
        page.goto(f"http://127.0.0.1:{server.server_port}/#hearings")
        page.locator('[data-open="attention-txt"]').click()
        expect(page.locator(".review-focus")).to_be_visible()
        page.locator(".review-focus summary").click()
        page.locator('[data-focus-field="/testimonies/0/statements/0/questions_none"]').click()
        assert page.locator('[data-focus-target="/testimonies/0/statements/0/questions_none"] button[data-repair]').evaluate("node => node === document.activeElement")
        page.keyboard.press("Enter")
        expect(page.locator("#structure-dialog")).to_be_visible()
        page.locator("#close-structure").click()
        page.locator("#transcript-filter").select_option("unreferenced")
        page.locator("#transcript-filter").select_option("all")
        assert all(s["start"] is None and s["end"] is None for s in server.store.get("attention-txt")["draft"]["segments"])
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        page.screenshot(path=str(audit / "review-focus-narrow.png"), full_page=True)
        run.data["checks"].append("Legacy untimed TXT retains honest original locations; narrow review has no overflow or browser errors")
        browser.close()


def main(argv=None):
    return audit_main("review-focus-browser", body, argv,
                      latest=ROOT / ".scratch/review-focus-browser/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
