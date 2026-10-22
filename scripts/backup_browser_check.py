"""Full verified workspace ZIP and empty-workspace browser restore audit."""
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
    from hearing_compiler.workspace_backup import verify_backup
    source = ROOT / "fixtures/audio/explicit-support.json"
    media = ROOT / "fixtures/audio/explicit-support.mp3"
    document = json.loads(source.read_text(encoding="utf-8"))
    run.identity("inputs", "synthetic_transcript", source)
    run.identity("inputs", "synthetic_recording", media)
    run.data["limits"] = "Synthetic recovery workflow only; no real runtime data, Mac validation or hearing accuracy claim."
    server = make_server(audit / "source", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def stop():
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()
    run.cleanup(stop)
    errors = []
    run.data["browser_errors"] = errors
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("HEARING_TEST_BROWSER"), headless=True)
        run.data["versions"]["browser"] = browser.version
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        base = f"http://127.0.0.1:{server.server_port}"
        page.goto(base)
        page.get_by_role("button", name="Import transcript JSON", exact=True).click()
        page.locator("#import-files").set_input_files({"name": "scripted.json", "mimeType": "application/json",
                                                       "buffer": json.dumps(document).encode()})
        job_id = document["hearing"]["id"]
        page.locator(f'[data-open="{job_id}"]').click()
        page.locator('[data-attach-audio]').first.click()
        page.locator("#attach-audio-file").set_input_files(str(media))
        expect(page.locator("#audio-duration")).not_to_have_text("00:00:00")
        page.locator('[data-edit="/testimonies/0/name"]').click()
        page.locator("#edit-value").fill("Backup reviewer correction")
        page.locator("#edit-review").select_option("NEEDS_REVIEW")
        page.locator("#save-edit").click()
        expect(page.locator("#edit-dialog")).not_to_be_visible()
        saved = server.store.get(job_id)
        # A backup must remain a native download even when blob buffering is
        # unavailable. JSON API responses and source playback are unaffected.
        page.evaluate("""() => {
            Response.prototype.blob = function() { throw new Error('Backup blob buffering forbidden by audit'); };
        }""")
        page.get_by_role("button", name="Backup and recovery", exact=True).click()
        expect(page.locator("#workspace-location")).to_contain_text(str(server.store.directory))
        expect(page.locator("#restore-workspace")).to_be_disabled()
        with page.expect_download() as downloaded:
            page.locator("#download-backup").click()
        archive = audit / "verified-workspace.zip"
        downloaded.value.save_as(str(archive))
        manifest, state = verify_backup(archive)
        assert manifest["recording_included"] and state["jobs"][job_id]["draft"] == saved["draft"]
        run.data["checks"].append("UI exports a verified full ZIP with exact original recording, transcript, saved correction and history")
        assert downloaded.value.url.endswith("/api/workspace/backup-download")
        expect(page.locator("#backup-progress")).to_contain_text("Check that your browser")
        expect(page.locator("#backup-progress")).not_to_contain_text("downloaded")
        assert not list(server.store.directory.glob(".backup-*.zip"))
        denied = page.request.get(base + "/api/workspace/backup-download")
        assert denied.status == 403
        run.data["checks"].append("Native authenticated download avoids Response.blob, consumes its one-use cookie and removes only the generated snapshot")
        stop()
        server = make_server(audit / "restored", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.get_by_role("button", name="Backup and recovery", exact=True).click()
        expect(page.locator("#restore-workspace")).to_be_enabled()
        page.locator("#restore-workspace").click()
        page.locator("#restore-workspace-file").set_input_files(str(archive))
        expect(page.locator("#backup-dialog")).not_to_be_visible()
        page.locator(f'[data-open="{job_id}"]').click()
        expect(page.locator('[data-field="/testimonies/0/name"]')).to_contain_text("Backup reviewer correction")
        restored = server.store.get(job_id)
        assert restored["draft"] == saved["draft"]
        assert restored["input"] == saved["input"]
        asset = restored["audio"]
        target = server.store.directory / asset["path"]
        assert hashlib.sha256(target.read_bytes()).hexdigest() == asset["sha256"]
        run.data["checks"].append("Full ZIP restores into a fresh workspace with original audio identity and exact source/review data")
        stop()
        server = make_server(audit / "restored", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.locator(f'[data-open="{job_id}"]').click()
        expect(page.locator("#audio-duration")).not_to_have_text("00:00:00")
        assert server.store.get(job_id)["draft"] == saved["draft"]
        page.get_by_role("button", name="Backup and recovery", exact=True).click()
        expect(page.locator("#restore-workspace")).to_be_disabled()
        run.data["checks"].append("Restart preserves restored corrections and playback; nonempty workspace restore remains blocked")
        assert not errors, errors
        browser.close()


def main(argv=None):
    return audit_main("backup-browser", body, argv, latest=ROOT / ".scratch/backup-browser-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
