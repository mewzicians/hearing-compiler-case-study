"""Synthetic subtitle transport, source integrity, audio navigation and recovery."""
from __future__ import annotations

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
    def clock(seconds, separator):
        millis = round(seconds * 1000)
        return f"{millis // 3600000:02}:{millis // 60000 % 60:02}:{millis // 1000 % 60:02}{separator}{millis % 1000:03}"
    srt = "\ufeff" + "\r\n\r\n".join(
        f"{i + 1}\r\n{clock(s['start'], ',')} --> {clock(s['end'], ',')}\r\n{s['text']}"
        for i, s in enumerate(document["segments"])) + "\r\n"
    vtt = "WEBVTT\n\n" + "\n\n".join(
        f"cue-{i + 1}\n{clock(s['start'], '.')} --> {clock(s['end'], '.')}\n{s['text']}"
        for i, s in enumerate(document["segments"])) + "\n"
    run.identity("inputs", "scripted_source", fixture)
    run.identity("inputs", "synthetic_audio", media)
    run.data["limits"] = "Generated subtitle clocks from synthetic scripted timing; no actual MacWhisper or real-hearing accuracy evidence."
    workspace = audit / "state"
    server = make_server(workspace, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def stop():
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()
    run.cleanup(stop)
    checks, errors = run.data["checks"], []
    run.data["browser_errors"] = errors
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("HEARING_TEST_BROWSER"), headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        run.data["versions"]["browser"] = browser.version
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}")
        assert server.store.list() == []
        page.get_by_role("button", name="Import subtitles (.srt/.vtt)", exact=True).click()
        page.locator("#text-files").set_input_files([
            {"name": "subtitle-pilot.srt", "mimeType": "text/plain", "buffer": srt.encode()},
            {"name": "invalid.vtt", "mimeType": "text/vtt", "buffer": b"WEBVTT\n\nnot a cue"},
        ])
        page.locator("#text-committee").fill("Example Committee")
        for i in range(2):
            page.locator(f"#text-date-{i}").fill("2026-10-06")
        page.locator("#text-public").check()
        page.locator("#save-text").click()
        expect(page.locator("#text-dialog")).not_to_be_visible()
        expect(page.locator("#review-title")).to_have_text("subtitle pilot")
        expect(page.locator("#global-error")).to_contain_text("timing")
        assert len(server.store.list()) == 1
        job_id = server.store.list()[0]["id"]
        saved = server.store.get(job_id)
        assert saved["input"]["source_text"] == srt
        assert all(s["speaker_unavailable"] for s in saved["input"]["segments"])
        assert saved["review"]["status"] != "APPROVED"
        checks.append("Valid SRT imports with exact original text; malformed VTT is isolated and missing speakers remain unresolved")
        page.locator("[data-attach-audio]").first.click()
        page.locator("#attach-audio-file").set_input_files(str(media))
        expect(page.locator("#audio-duration")).not_to_have_text("00:00:00")
        index = 1
        segment = saved["draft"]["segments"][index]
        page.locator(f'[data-segment="{segment["id"]}"] [data-evidence]').click()
        page.wait_for_function("(expected) => Math.abs(document.querySelector('#source-audio').currentTime - expected) < 1.5", arg=segment["start"])
        page.locator("#source-audio").evaluate("audio => audio.pause()")
        checks.append("Clicking a validated subtitle cue seeks the attached original synthetic MP3")
        page.locator('[data-edit="/testimonies/0/name"]').click()
        page.locator("#edit-value").fill("Subtitle reviewer correction")
        page.locator("#edit-review").select_option("NEEDS_REVIEW")
        page.locator("#save-edit").click()
        expect(page.locator("#edit-dialog")).not_to_be_visible()
        page.locator('[data-view="output"]').click()
        with page.expect_download() as exported:
            page.get_by_role("button", name="Export saved data (.json)").click()
        target = audit / "subtitle-review.json"
        exported.value.save_as(str(target))
        snapshot = json.loads(target.read_text(encoding="utf-8"))
        assert snapshot["job"]["input"]["source_text"] == srt
        assert snapshot["job"]["draft"]["source_text"] == srt
        assert snapshot["job"]["draft"]["segments"][index]["start"] == segment["start"]
        checks.append("Saved correction and JSON export preserve subtitle clocks and original-source provenance")
        stop()
        server = make_server(workspace, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.locator(f'[data-open="{job_id}"]').click()
        expect(page.locator('[data-field="/testimonies/0/name"]')).to_contain_text("Subtitle reviewer correction")
        assert server.store.get(job_id)["input"]["source_text"] == srt
        checks.append("Service restart preserves subtitle source, corrections, original audio and unapproved review")
        page.locator('[data-view="hearings"]').click()
        page.get_by_role("button", name="Add MP3 files", exact=True).first.click()
        # A distinct generated fixture creates an independent pending recording.
        second_audio = ROOT / "fixtures/audio/multi-bill-conditional.mp3"
        run.identity("inputs", "second_synthetic_audio", second_audio)
        page.locator("#audio-files").set_input_files(str(second_audio))
        page.locator("#audio-committee").fill("Example Committee")
        page.locator("#audio-date-0").fill("2026-10-06")
        page.locator("#audio-public").check()
        page.locator("#save-audio-import").click()
        expect(page.locator("#audio-import-dialog")).not_to_be_visible()
        pending = next(j for j in server.store.list() if j["processing"] == "Awaiting transcript")
        page.locator(f'[data-attach-transcript="{pending["id"]}"]').click()
        page.locator("#attach-transcript-file").set_input_files(
            {"name": "attachment.vtt", "mimeType": "text/vtt", "buffer": vtt.encode()})
        expect(page.locator(f'[data-open="{pending["id"]}"]')).to_be_visible()
        page.locator(f'[data-open="{pending["id"]}"]').click()
        expect(page.locator(".draft-pane")).to_be_visible()
        attached = server.store.get(pending["id"])
        assert attached["input"]["source_format"] == "webvtt-utf8"
        assert attached["input"]["source_text"] == vtt
        assert attached["audio"]["sha256"] != server.store.get(job_id)["audio"]["sha256"]
        checks.append("VTT attachment uses the selected waiting recording and completes through the queue")
        assert not errors, errors
        browser.close()


def main(argv=None):
    return audit_main("subtitle-browser", body, argv, latest=ROOT / ".scratch/subtitle-browser-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
