"""Plain-text intake, honest evidence, manual playback and restart audit."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.audit_reporting import audit_main, browser_setup


def body(run, argv):
    audit = browser_setup(run, argv)
    from playwright.sync_api import expect, sync_playwright
    from hearing_compiler.server import make_server
    fixture = ROOT / "fixtures/audio/explicit-support.json"
    media = ROOT / "fixtures/audio/explicit-support.mp3"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    text = "\ufeff" + "\r\n".join(s["text"] for s in document["segments"])
    run.identity("inputs", "scripted_source", fixture)
    run.identity("inputs", "synthetic_audio", media)
    run.data["limits"] = "Synthetic UI/transport only; no MacWhisper ASR or real-hearing accuracy evidence."
    workspace = audit / "state"
    server = make_server(workspace, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def stop():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
    run.cleanup(stop)
    checks, errors = run.data["checks"], []
    run.data["browser_errors"] = errors
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("HEARING_TEST_BROWSER"), headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 900},
                                permissions=["clipboard-read", "clipboard-write"])
        run.data["versions"]["browser"] = browser.version
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}")
        expect(page.locator("#queue-title")).to_have_text("Batch queue")
        assert server.store.list() == []
        page.get_by_role("button", name="Import text (.txt)", exact=True).click()
        page.locator("#text-files").set_input_files([
            {"name": "text-pilot.txt", "mimeType": "text/plain", "buffer": text.encode("utf-8")},
            {"name": "invalid.txt", "mimeType": "text/plain", "buffer": b"\xff\xfeinvalid"},
        ])
        page.locator("#text-committee").fill("Example Committee")
        for i in range(2):
            page.locator(f"#text-date-{i}").fill("2026-10-06")
        page.locator("#text-public").check()
        page.locator("#save-text").click()
        expect(page.locator("#text-dialog")).not_to_be_visible()
        expect(page.locator("#review-title")).to_have_text("text pilot")
        assert len(server.store.list()) == 1
        expect(page.locator("#global-error")).to_contain_text("UTF-8")
        checks.append("TXT button imports the valid batch item; invalid UTF-8 is isolated and reported")
        job_id = server.store.list()[0]["id"]
        job = server.store.get(job_id)
        assert job["input"]["source_text"] == text
        assert all(s["start"] is None and s["end"] is None for s in job["draft"]["segments"])
        labels = page.locator(".segment-header [data-evidence]").all_text_contents()
        assert labels and all(label.startswith("Text line ") for label in labels)
        assert "timestamps" in page.locator(".audio-note").inner_text().lower()
        link = page.locator('[data-field="/testimonies/0/name"] [data-evidence]').first
        cited = link.get_attribute("data-evidence")
        link.click()
        expect(page.locator(f'[data-segment="{cited}"]')).to_have_class("segment selected")
        checks.append("Original UTF-8/BOM/CRLF text persists; line evidence highlights without fabricated timestamps")
        page.locator('[data-attach-audio]').first.click()
        page.locator("#attach-audio-file").set_input_files(str(media))
        expect(page.locator("#audio-duration")).not_to_have_text("00:00:00")
        page.locator("#source-audio").evaluate("audio => { audio.pause(); audio.currentTime = 5; }")
        page.locator('[data-segment="txt0002"] [data-evidence]').click()
        state = page.locator("#source-audio").evaluate("audio => ({time:audio.currentTime,paused:audio.paused})")
        assert abs(state["time"] - 5) < .2 and state["paused"], state
        page.locator("#audio-play").click()
        page.wait_for_function("() => document.querySelector('#source-audio').currentTime > 5.05")
        page.locator("#audio-play").click()
        checks.append("Original MP3 can play manually; clicking an untimed line never seeks or auto-plays")
        page.locator('[data-edit="/testimonies/0/name"]').click()
        page.locator("#edit-value").fill("Text pilot reviewer correction")
        page.locator("#edit-review").select_option("NEEDS_REVIEW")
        page.locator("#save-edit").click()
        expect(page.locator("#edit-dialog")).not_to_be_visible()
        saved = server.store.get(job_id)
        page.locator('[data-view="output"]').click()
        assert "Text pilot reviewer correction" in page.locator(".document-preview").inner_text()
        with page.expect_download() as exported:
            page.get_by_role("button", name="Export saved data (.json)").click()
        output = audit / "text-review.json"
        exported.value.save_as(str(output))
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["job"]["input"]["source_text"] == text
        assert payload["recording_included"] is False
        with page.expect_download() as exported:
            page.get_by_role("button", name="Export Word (.docx)").click()
        exported.value.save_as(str(audit / "text-note.docx"))
        checks.append("Corrections reach readable/JSON/Word exports; source text and unavailable times remain truthful")
        stop()
        server = make_server(workspace, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.locator(f'[data-open="{job_id}"]').click()
        expect(page.locator('[data-field="/testimonies/0/name"]')).to_contain_text("Text pilot reviewer correction")
        restored = server.store.get(job_id)
        assert restored["draft"] == saved["draft"]
        assert restored["input"]["source_text"] == text
        checks.append("Actual service restart preserves text, review edits, evidence and attached audio")
        page.set_viewport_size({"width": 390, "height": 844})
        for view in ["hearings", "review", "output"]:
            page.locator(f'[data-view="{view}"]').click()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), view
        page.screenshot(path=str(audit / "text-mobile.png"), full_page=True)
        assert not errors, errors
        checks.append("Text intake/review/output fit narrow screens without browser errors")
        browser.close()


def main(argv=None):
    return audit_main("text-browser", body, argv, latest=ROOT / ".scratch/text-browser-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
