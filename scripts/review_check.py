"""Optional structural-review browser audit; uses only disposable synthetic workspaces.

Run with the optional QA requirements installed and HEARING_TEST_BROWSER pointing
to an installed Chromium browser. Application production code has no QA dependency.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.audit_reporting import audit_main, browser_setup


def launch(workspace):
    server = make_server(workspace, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive(), "Local test service did not stop."


def repair(page, op, **values):
    buttons = page.locator("#review-view [data-repair]")
    for index in range(buttons.count()):
        item = json.loads(buttons.nth(index).get_attribute("data-repair"))
        if item["op"] == op and all(item.get(key) == value for key, value in values.items()):
            buttons.nth(index).click()
            expect(page.locator("#structure-dialog")).to_be_visible()
            return
    raise AssertionError(f"No visible repair control for {op}: {values}")


def choose_evidence(page, scope, ids):
    choices = page.locator(f'[data-evidence-choice="{scope}"]')
    for index in range(choices.count()):
        choice = choices.nth(index)
        choice.set_checked(choice.get_attribute("value") in ids)


def save_repair(page):
    page.locator("#save-structure").click()
    try:
        page.locator("#structure-dialog").wait_for(state="hidden")
    except Exception as error:
        page.screenshot(path=str(AUDIT_FOLDER / "failure.png"), full_page=True)
        raise AssertionError(page.locator("#structure-error").inner_text() or str(error)) from error
    expect(page.locator("#global-error")).to_be_hidden()


def edit(page, path, value=None, *, approved=True, evidence=None, not_applicable=False):
    page.locator(f'[data-edit="{path}"]').click()
    if not_applicable:
        page.locator("#organization-na").check()
    elif path.endswith("/position"):
        page.locator("#position-value").select_option(value)
    elif value is not None:
        page.locator("#edit-value").fill(value)
    if not not_applicable:
        page.locator("#edit-review").select_option("APPROVED" if approved else "NEEDS_REVIEW")
    if evidence is not None:
        picker = page.locator(".evidence-picker").filter(has=page.locator("#edit-evidence"))
        if not picker.evaluate("node => node.open"):  # The dialog keeps the picker open from an earlier edit.
            picker.locator("summary").click()
        choose_evidence(page, "edit-evidence", evidence)
    page.locator("#save-edit").click()
    page.locator("#edit-dialog").wait_for(state="hidden")
    expect(page.locator("#global-error")).to_be_hidden()


def remove(page, op, **values):
    repair(page, op, **values)
    page.locator('#structure-fields input[type="checkbox"][required]').check()
    save_repair(page)


def body(run, argv):
    global Document, expect, sync_playwright, make_server, AUDIT_FOLDER
    audit = browser_setup(run, argv)
    AUDIT_FOLDER = audit
    from docx import Document
    from playwright.sync_api import expect, sync_playwright
    from hearing_compiler.server import make_server
    workspace = audit / ("state-" + uuid.uuid4().hex[:8])
    probe = audit / ("probe-" + uuid.uuid4().hex[:8] + ".txt")
    probe.write_text("writable QA workspace", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "writable QA workspace"
    probe.unlink()
    fixture = ROOT / "fixtures/transcripts/sample-hearing.json"
    source_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
    run.identity("inputs", "transcript", fixture)
    document = json.loads(fixture.read_text(encoding="utf-8"))
    document["hearing"]["id"] = "structural-review-audit"
    document["hearing"]["title"] = "Synthetic structural review hearing"
    document["segments"] += [
        {"id": "s05", "start": 5085, "end": 5093, "speaker": "Speaker 4",
         "text": "My testimony on H.1234 concerns access for families with limited resources."},
        {"id": "s06", "start": 5095, "end": 5102, "speaker": "Speaker 5",
         "text": "Please explain the eligibility provisions."},
        {"id": "s07", "start": 5104, "end": 5113, "speaker": "Speaker 4",
         "text": "The eligibility provisions reach families who cannot otherwise obtain assistance."},
    ]
    # Procedural segments test review scroll, not extraction accuracy or scale.
    document["segments"] += [
        {"id": f"extra{index}", "start": 5120 + index * 10, "end": 5129 + index * 10,
         "speaker": "Chair", "text": "Procedural remarks for the synthetic review-position check."}
        for index in range(80)
    ]
    server, thread, address = launch(workspace)
    run.cleanup(lambda: stop(server, thread))
    checks, errors, screenshots = [], [], []
    downloads = []
    browser = None
    page = None
    versions = {"python": platform.python_version(), "playwright": importlib.metadata.version("playwright"),
                "python_docx": importlib.metadata.version("python-docx"),
                "browser_executable": os.environ.get("HEARING_TEST_BROWSER")}
    report = {"status": "FAIL", "checks": checks, "browser_errors": errors,
              "workspace": str(workspace.resolve()), "versions": versions,
              "limits": "Synthetic browser behavior only; no real hearing, target Mac or packaged-app validation."}
    run.data.update({k: v for k, v in report.items() if k != "status"})
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=versions["browser_executable"], headless=True)
            versions["browser"] = browser.version
            context = browser.new_context(viewport={"width": 1366, "height": 900},
                                          permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("download", lambda download: downloads.append(download.suggested_filename))
            page.goto(address)
            expect(page.locator("#queue-title")).to_have_text("Batch queue")
            assert server.store.list() == []
            assert page.get_by_text("Load samples", exact=True).count() == 0
            checks.append("Runtime, ignored workspace, browser and local service smoke check; startup empty")

            page.locator("#import-files").set_input_files({"name": "review-synthetic.json",
                "mimeType": "application/json", "buffer": json.dumps(document).encode()})
            job_id = document["hearing"]["id"]
            page.locator(f'[data-open="{job_id}"]').click()
            expect(page.locator("#review-title")).to_have_text(document["hearing"]["title"])
            job = server.store.get(job_id)
            assert len(job["draft"]["testimonies"]) == 1
            assert job["draft"]["segments"] == document["segments"]
            checks.append("Synthetic transcript imported through the normal UI; original segments preserved")

            page.locator(".evidence-content").evaluate("e => {e.style.scrollBehavior = 'auto'; e.scrollTop = 800}")
            before = page.locator(".evidence-content").evaluate("e => e.scrollTop")
            page.locator('[data-approve="/testimonies/0/name"]').click()
            expect(page.locator('[data-field="/testimonies/0/name"] .approved-mark')).to_be_visible()
            after = page.locator(".evidence-content").evaluate("e => e.scrollTop")
            assert before == after and before > 0, {"before": before, "after": after}
            assert page.evaluate("document.activeElement.id") == "draft-heading"
            checks.append("Approving a field preserves transcript scroll instead of resetting it")

            original_arguments = len(server.store.get(job_id)["draft"]["testimonies"][0]["statements"][0]["arguments"])
            repair(page, "add_argument", testimony=0, statement=0)
            page.locator("#repair-value").fill("The proposal improves access for families needing assistance.")
            choose_evidence(page, "repair-evidence", ["s02"])
            save_repair(page)
            job = server.store.get(job_id)
            assert len(job["draft"]["testimonies"][0]["statements"][0]["arguments"]) == original_arguments + 1
            argument_path = f"/testimonies/0/statements/0/arguments/{original_arguments}"
            argument = job["draft"]["testimonies"][0]["statements"][0]["arguments"][-1]
            assert argument["evidence"] == ["s02"] and argument["review"] != "APPROVED"
            remove(page, "remove_argument", testimony=0, statement=0, argument=original_arguments)
            assert len(server.store.get(job_id)["draft"]["testimonies"][0]["statements"][0]["arguments"]) == original_arguments
            checks.append("Missing argument can be added with evidence and false argument removed; additions remain drafts")

            repair(page, "add_testimony")
            page.locator("#repair-speaker").select_option("Speaker 4")
            page.locator("#repair-name").fill("Casey Rivera")
            choose_evidence(page, "repair-evidence", ["s05", "s07"])
            save_repair(page)
            assert len(server.store.get(job_id)["draft"]["testimonies"]) == 2
            edit(page, "/testimonies/1/statements/0/bill", "H.1234")
            edit(page, "/testimonies/1/name", "Casey Rivera")
            edit(page, "/testimonies/1/organization", not_applicable=True)
            organization = server.store.get(job_id)["draft"]["testimonies"][1]["organization"]
            assert organization["value"] is None and organization["not_applicable"] and organization["review"] == "APPROVED"
            expect(page.locator('[data-field="/testimonies/1/organization"] .field-value')).to_have_text("Not applicable")
            checks.append("Missed testifier can be added; optional organization is reviewed as not applicable")

            repair(page, "add_testimony")
            page.locator("#repair-speaker").select_option("Speaker 5")
            page.locator("#repair-name").fill("Temporary erroneous witness")
            choose_evidence(page, "repair-evidence", ["s06"])
            save_repair(page)
            assert len(server.store.get(job_id)["draft"]["testimonies"]) == 3
            remove(page, "remove_testimony", testimony=2)
            assert len(server.store.get(job_id)["draft"]["testimonies"]) == 2
            repair(page, "add_statement", testimony=1)
            page.locator("#repair-bill").fill("H.5678")
            page.locator("#repair-position").select_option("Oppose")
            choose_evidence(page, "repair-evidence", ["s05"])
            save_repair(page)
            assert len(server.store.get(job_id)["draft"]["testimonies"][1]["statements"]) == 2
            remove(page, "remove_statement", testimony=1, statement=1)
            assert len(server.store.get(job_id)["draft"]["testimonies"][1]["statements"]) == 1
            checks.append("Testimony and bill statements can be added and removed without changing the transcript")

            edit(page, "/testimonies/1/statements/0/bill", "H.1234", approved=False, evidence=["s01", "s05"])
            bill = server.store.get(job_id)["draft"]["testimonies"][1]["statements"][0]["bill"]
            assert bill["evidence"] == ["s01", "s05"] and bill["review"] != "APPROVED", bill
            page.locator('[data-approve="/testimonies/1/statements/0/bill"]').click()
            expect(page.locator('[data-field="/testimonies/1/statements/0/bill"] .approved-mark')).to_be_visible()
            # Choosing Approved by me in the same save keeps the approval (the dialog says so).
            edit(page, "/testimonies/1/statements/0/bill", "H.1234", evidence=["s05"])
            bill = server.store.get(job_id)["draft"]["testimonies"][1]["statements"][0]["bill"]
            assert bill["evidence"] == ["s05"] and bill["review"] == "APPROVED", bill
            checks.append("Evidence can be repaired independently; saved as Needs review it needs a fresh approval, "
                          "and an approval chosen in the same save stays")

            repair(page, "add_question")
            page.locator("#repair-asker-speaker").select_option("Speaker 5")
            page.locator("#repair-asker").fill("Rep. Blair")
            page.locator("#repair-respondent-speaker").select_option("Speaker 4")
            page.locator("#repair-respondent").fill("Casey Rivera")
            page.locator("#repair-bill").fill("H.1234")
            page.locator("#repair-question").fill("Please explain the eligibility provisions.")
            choose_evidence(page, "repair-evidence", ["s06"])
            page.locator("#repair-response").fill("The provisions reach families who cannot obtain assistance.")
            choose_evidence(page, "repair-response-evidence", ["s07"])
            save_repair(page)
            assert len(server.store.get(job_id)["draft"]["questions"]) == 2
            repair(page, "set_question_speakers", question=1)
            page.locator("#repair-asker-speaker").select_option("Speaker 3")
            page.locator("#repair-respondent-speaker").select_option("Speaker 2")
            save_repair(page)
            question = server.store.get(job_id)["draft"]["questions"][1]
            assert question["asker_speaker"] == "Speaker 3" and question["respondent_speaker"] == "Speaker 2"
            assert question["asker"]["review"] != "APPROVED" and question["response"]["review"] != "APPROVED"
            remove(page, "remove_question", question=1)
            assert len(server.store.get(job_id)["draft"]["questions"]) == 1
            checks.append("Missed Q&A, independent question/response evidence, repaired speaker links and removal work")

            repair(page, "confirm_no_questions", testimony=1, statement=0)
            page.locator("#repair-none-confirmed").check()
            choose_evidence(page, "repair-evidence", ["s05", "s07"])
            save_repair(page)
            none = server.store.get(job_id)["draft"]["testimonies"][1]["statements"][0]["questions_none"]
            assert none["value"] == "None noted during this testimony." and none["review"] == "APPROVED"
            checks.append("No-question wording requires explicit reviewer confirmation and selected evidence")

            edit(page, "/testimonies/1/statements/0/position", "Support")
            repair(page, "add_argument", testimony=1, statement=0)
            page.locator("#repair-value").fill("Eligibility reaches families who cannot otherwise obtain assistance.")
            choose_evidence(page, "repair-evidence", ["s07"])
            save_repair(page)

            repair(page, "group_testimonies")
            page.locator('[data-group-member][value="0"]').check()
            page.locator('[data-group-member][value="1"]').check()
            save_repair(page)
            group = server.store.get(job_id)["draft"]["testimony_groups"][0]
            assert group["confirmation"]["review"] != "APPROVED" and len(group["members"]) == 2
            page.locator('[data-approve="/testimony_groups/0/confirmation"]').click()
            expect(page.locator('[data-field="/testimony_groups/0/confirmation"] .approved-mark')).to_be_visible()
            shared_note = server.store.get(job_id)["markdown"]
            shared_line = [line for line in shared_note.splitlines() if line.startswith("Testifiers:")][0]
            assert "Alex Morgan" in shared_line and "Casey Rivera" in shared_line
            remove(page, "ungroup_testimonies", group_id=group["id"])
            assert server.store.get(job_id)["draft"]["testimony_groups"] == []
            assert len(server.store.get(job_id)["draft"]["testimonies"]) == 2
            checks.append("Shared testimony groups require separate approval; ungroup preserves each speaker's records")

            page.locator('[data-edit="/testimonies/1/name"]').click()
            page.locator("#edit-value").fill("Unsaved modal correction")
            downloads_before = len(downloads)
            page.evaluate("performAction('snapshot')")
            expect(page.locator("#toast")).to_contain_text("Save or cancel")
            page.evaluate("setView('output')")
            expect(page.locator("#edit-dialog")).to_be_visible()
            expect(page.locator("#edit-value")).to_have_value("Unsaved modal correction")
            assert len(downloads) == downloads_before
            assert server.store.get(job_id)["draft"]["testimonies"][1]["name"]["value"] == "Casey Rivera"
            # Simulate another legitimate local review tab while this form is open.
            current = server.store.get(job_id)
            other_organization = "Example Legal Services — reviewed in another tab"
            session = page.request.get(address + "/api/session").json()
            response = page.request.post(address + "/api/review", headers={"X-Review-Token": session["token"]},
                data={"id": job_id, "revision": current["revision"], "changes": [{"path": "/testimonies/0/organization",
                      "value": other_organization, "review": "APPROVED"}], "operations": []})
            assert response.ok
            other_revision = response.json()["revision"]
            page.wait_for_function("revision => state.hearings.some(job => job.id === 'structural-review-audit' && job.revision === revision)", arg=other_revision)
            expect(page.locator("#edit-value")).to_have_value("Unsaved modal correction")
            page.locator("#cancel-edit").click()
            expect(page.locator('[data-field="/testimonies/0/organization"] .field-value')).to_have_text(other_organization)
            checks.append("Open unsaved correction survives navigation, export and another tab's save; polling resumes after dismissal")

            for viewport in [{"width": 1366, "height": 900}, {"width": 390, "height": 844}]:
                page.set_viewport_size(viewport)
                for view in ("review", "output"):
                    page.locator(f'[data-view="{view}"]').click()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (view, viewport)
                    filename = f"{view}-{'desktop' if viewport['width'] == 1366 else 'narrow'}.png"
                    page.screenshot(path=str(audit / filename), full_page=True)
                    screenshots.append(filename)
                page.locator('[data-view="review"]').click()
                repair(page, "add_argument", testimony=1, statement=0)
                page.locator("#repair-value").fill("Synthetic repair dialog layout check.")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), viewport
                filename = f"repair-dialog-{'desktop' if viewport['width'] == 1366 else 'narrow'}.png"
                page.screenshot(path=str(audit / filename))
                screenshots.append(filename)
                page.locator("#cancel-structure").click()
            checks.append("Review, output and evidence-selection dialog fit desktop and narrow widths")

            page.set_viewport_size({"width": 1366, "height": 900})
            page.locator('[data-view="output"]').click()
            page.locator('[data-action="copy"]').click()
            expect(page.locator("#toast")).to_contain_text("copied")
            copied = page.evaluate("navigator.clipboard.readText()")
            assert "Casey Rivera" in copied and "[draft; review needed]" in copied
            assert "None noted during this testimony." in copied
            with page.expect_download() as download:
                page.locator('[data-action="docx"]').click()
            word_path = audit / "reviewed-notes.docx"
            download.value.save_as(str(word_path))
            word = Document(word_path)
            word_text = "\n".join(paragraph.text for paragraph in word.paragraphs)
            assert "Casey Rivera" in word_text and "[draft; review needed]" in word_text
            assert "None noted during this testimony." in word_text and "Hearing Notes" in word_text
            with page.expect_download() as download:
                page.locator('[data-action="snapshot"]').click()
            json_path = audit / "saved-review.json"
            download.value.save_as(str(json_path))
            snapshot = json.loads(json_path.read_text(encoding="utf-8"))
            assert snapshot["job"]["draft"]["segments"] == document["segments"]
            assert snapshot["job"]["draft"]["testimonies"][1]["organization"]["not_applicable"]
            assert "audio_url" not in snapshot["job"]
            assert snapshot.get("recording_included") is False
            assert len(snapshot["job"]["draft"].get("review_log", [])) > 0
            checks.append("Actual Word and JSON UI downloads parse; copied/exported notes retain draft labels and reviewed repairs")

            saved_draft = server.store.get(job_id)["draft"]
            saved_revision = server.store.get(job_id)["revision"]
            assert saved_draft["segments"] == document["segments"]
            assert hashlib.sha256(fixture.read_bytes()).hexdigest() == source_hash
            stop(server, thread)
            server, thread, address = launch(workspace)
            page.goto(address)
            page.locator(f'[data-open="{job_id}"]').click()
            job = server.store.get(job_id)
            assert job["draft"] == saved_draft and job["revision"] == saved_revision
            assert job["draft"]["testimonies"][1]["name"]["value"] == "Casey Rivera"
            expect(page.locator('[data-field="/testimonies/1/organization"] .field-value')).to_have_text("Not applicable")
            assert hashlib.sha256(fixture.read_bytes()).hexdigest() == source_hash
            checks.append("Real server stop/restart restores repairs, reviews, evidence and history without reprocessing or fixture changes")
            assert not errors, errors
            browser.close()
            browser = None
        report.update(status="PASS", screenshots=screenshots, downloads=downloads)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        if page is not None:
            try:
                page.screenshot(path=str(audit / "failure.png"), full_page=True)
                report["visible_error"] = page.locator("#structure-error").inner_text() if page.locator("#structure-dialog").is_visible() else page.locator("#global-error").inner_text()
            except Exception:
                pass
        raise
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception as error:
                run.cleanup(lambda error=error: (_ for _ in ()).throw(error))
        run.data.update({k: v for k, v in report.items() if k not in {"status", "versions"}})


def main(argv=None):
    return audit_main("review-browser", body, argv, latest=ROOT / ".scratch/review-browser-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
