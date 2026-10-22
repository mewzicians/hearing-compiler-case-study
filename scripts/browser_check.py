"""Fresh-workspace browser audit with generated MP3 media; test data is not bundled."""
from __future__ import annotations
import json
import os
import sys
import threading
import uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from scripts.audit_reporting import audit_main, browser_setup


def launch(directory):
    server = make_server(directory, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def body(run, argv):
    global expect, sync_playwright, export, mp3_bytes, make_server
    audit = browser_setup(run, argv)
    from playwright.sync_api import expect, sync_playwright
    from audio_test_helpers import export, mp3_bytes
    from hearing_compiler.server import make_server
    workspace = audit / ("state-" + uuid.uuid4().hex[:8])
    server, thread, address = launch(workspace)
    def cleanup():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Audit service failed to stop."
    run.cleanup(cleanup)
    checks, errors = [], []
    run.data.update(checks=checks, browser_errors=errors, screenshots=str(audit))
    browser_path = os.environ.get("HEARING_TEST_BROWSER")
    media = mp3_bytes(30)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=browser_path, headless=True)
            run.data["versions"]["browser"] = browser.version
            context = browser.new_context(viewport={"width": 1366, "height": 900}, permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(address)
            expect(page.locator("#queue-title")).to_have_text("Batch queue")
            assert server.store.list() == []
            assert page.get_by_text("Load samples", exact=True).count() == 0
            assert "Synthetic test data" not in page.locator("body").inner_text()
            page.screenshot(path=str(audit / "fresh-workspace.png"), full_page=True)
            checks.append("Fresh empty workspace, MP3/folder entry points and no demo controls")

            page.locator('[data-action="connection"]').first.click()
            expect(page.locator("#connection-dialog")).to_be_visible()
            assert page.locator('#connection-dialog input[type="password"]').count() == 0
            assert "separate API key" in page.locator("#connection-dialog").inner_text()
            if sys.platform != "darwin":
                assert page.locator("#check-connection").is_disabled()
            page.locator("#close-connection").click()
            checks.append("Mac connection describes activation inside MacWhisper and has no credential field")

            page.locator("#audio-files").set_input_files([
                {"name": "hearing_2025-04-22.mp3", "mimeType": "audio/mpeg", "buffer": media},
                {"name": "invalid_2025-04-23.mp3", "mimeType": "audio/mpeg", "buffer": b"not mp3"},
            ])
            page.locator("#audio-committee").fill("Example Committee")
            page.locator("#audio-public").check()
            page.locator("#save-audio-import").click()
            page.locator("#audio-import-dialog").wait_for(state="hidden")
            expect(page.locator("tbody tr")).to_have_count(1)
            assert "expected audio header" in page.locator("#global-error").inner_text()
            job_id = server.store.list()[0]["id"]
            assert server.store.get(job_id)["processing"] == "Awaiting transcript"
            page.screenshot(path=str(audit / "audio-queue.png"), full_page=True)
            checks.append("Real MP3 batch upload saves valid media while reporting an invalid file independently")

            page.locator(f'[data-attach-transcript="{job_id}"]').click()
            page.locator("#attach-transcript-file").set_input_files({"name": "macwhisper.json", "mimeType": "application/json", "buffer": json.dumps(export()).encode()})
            expect(page.locator(f'[data-open="{job_id}"]')).to_have_text("Open draft")
            assert server.store.get(job_id)["draft"]["segments"][2]["start"] == 15
            page.locator(f'[data-open="{job_id}"]').click()
            expect(page.locator("#audio-player")).to_be_visible()
            expect(page.locator("#audio-duration")).not_to_have_text("00:00:00")
            page.locator('[data-field="/questions/0/question"] [data-evidence="mw03"]').click()
            expect(page.locator("#audio-time")).to_have_text("00:00:15")
            page.wait_for_function("""() => {
                const audio = document.querySelector('#source-audio');
                return audio.readyState >= 2 && !audio.paused && audio.currentTime > 15.02;
            }""", timeout=10000)
            media_state = page.locator("#source-audio").evaluate("element => ({time: element.currentTime, paused: element.paused, duration: element.duration, ready: element.readyState})")
            assert 15 < media_state["time"] < 18, media_state
            assert not media_state["paused"] and media_state["ready"] >= 2, media_state
            page.locator("#audio-play").click()
            page.locator("#audio-speed").select_option("1.5")
            assert page.locator("#source-audio").evaluate("element => element.playbackRate") == 1.5
            page.screenshot(path=str(audit / "review-audio-seek.png"), full_page=True)
            checks.append("MacWhisper transcript attaches to its original MP3; clicking evidence seeks to 15 seconds and actually plays")
            checks.append("Player pause and playback speed work on decoded MP3 media")

            page.locator('[data-edit="/questions/0/asker"]').click()
            page.locator("#edit-value").fill("Rep. Avery")
            page.locator("#edit-review").select_option("APPROVED")
            page.locator("#save-edit").click()
            page.locator("#edit-dialog").wait_for(state="hidden")
            page.locator('[data-view="output"]').click()
            assert "Rep. Avery" in page.locator(".document-preview").inner_text()
            page.locator('[data-action="copy"]').click()
            expect(page.locator("#toast")).to_contain_text("copied")
            copied = page.evaluate("navigator.clipboard.readText()")
            assert "Rep. Avery" in copied and "[draft; review needed]" in copied
            with page.expect_download() as download:
                page.locator('[data-action="download"]').click()
            download.value.save_as(str(audit / "note.md"))
            assert (audit / "note.md").read_text(encoding="utf-8") == copied.replace("\r\n", "\n")
            checks.append("Saved human correction reaches copied/exported notes while other fields stay marked as drafts")

            # Confirm transcript-first input remains available without sample controls.
            page.locator('[data-view="hearings"]').click()
            page.locator("#macwhisper-file").set_input_files({"name": "standalone.json", "mimeType": "application/json", "buffer": json.dumps(export()).encode()})
            page.locator("#mw-title").fill("Imported hearing")
            page.locator("#mw-date").fill("2025-04-22")
            page.locator("#mw-committee").fill("Example Committee")
            page.locator("#mw-public").check()
            page.locator("#save-macwhisper").click()
            page.locator("#macwhisper-dialog").wait_for(state="hidden")
            expect(page.locator("#review-title")).to_have_text("Imported hearing")
            assert page.locator("#audio-player").is_hidden()
            transcript_id = [j["id"] for j in server.store.list() if j["id"] != job_id][0]
            page.locator(f'[data-attach-audio="{transcript_id}"]').click()
            page.locator("#attach-audio-file").set_input_files({"name": "original.mp3", "mimeType": "audio/mpeg", "buffer": media})
            expect(page.locator("#audio-player")).to_be_visible()
            checks.append("Additional MacWhisper import works first; attaching original audio preserves the existing draft")

            for viewport in [{"width": 1366, "height": 900}, {"width": 390, "height": 844}]:
                page.set_viewport_size(viewport)
                for view in ("hearings", "review", "output"):
                    page.locator(f'[data-view="{view}"]').click()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (view, viewport)
                    if viewport["width"] == 390:
                        page.screenshot(path=str(audit / f"{view}-mobile.png"), full_page=True)
            checks.append("All views and the audio player fit desktop and narrow screens")

            server.shutdown(); server.server_close(); thread.join()
            server, thread, address = launch(workspace)
            page.goto(address)
            expect(page.locator("tbody tr")).to_have_count(2)
            page.locator(f'[data-open="{job_id}"]').click()
            assert "Rep. Avery" in page.locator('[data-field="/questions/0/asker"]').inner_text()
            expect(page.locator("#audio-player")).to_be_visible()
            assert server.store.get(job_id)["audio"]["sha256"] == __import__("hashlib").sha256(media).hexdigest()
            checks.append("Actual restart restores the source recording, cached transcript and human correction without sample seeding")
            page.locator('.app-footer [data-action="stop"]').click()
            expect(page.locator("#save-status")).to_contain_text("Stopping")
            assert "App stopped" not in page.locator("#save-status").inner_text()
            checks.append("Stop app reports shutdown requested while ownership remains protected during cleanup")
            assert not errors, errors
            browser.close()
    finally:
        pass  # Shared lifecycle finalizes after registered cleanup.


def main(argv=None):
    return audit_main("audio-browser", body, argv, latest=ROOT / ".scratch/audio-browser-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
