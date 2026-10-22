"""Decode and seek the committed synthetic speech fixtures in a disposable browser workspace."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.audit_reporting import audit_main, browser_setup


def body(run, argv):
    audit = browser_setup(run, argv)
    from playwright.sync_api import expect, sync_playwright
    from hearing_compiler.server import make_server
    audio = ROOT / "fixtures/audio"
    manifest = json.loads((audio / "manifest.json").read_text(encoding="utf-8"))
    run.identity("inputs", "manifest", audio / "manifest.json")
    server = make_server(audit / ("state-" + uuid.uuid4().hex[:8]), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def cleanup():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Audit service failed to stop."
    run.cleanup(cleanup)
    checks, errors, recordings = [], [], []
    report = {"status": "ERROR", "checks": checks, "browser_errors": errors, "recordings": recordings,
              "manual_listening": "pending", "macwhisper_transcription": "not run"}
    run.data.update({k: v for k, v in report.items() if k != "status"})
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ.get("HEARING_TEST_BROWSER"), headless=True)
            run.data["versions"]["browser"] = browser.version
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator("#queue-title")).to_have_text("Batch queue")
            assert server.store.list() == []
            checks.append("Fresh disposable workspace starts empty; no generated fixtures are injected")
            for entry in manifest["recordings"]:
                media = audio / entry["filename"]
                transcript = audio / entry["transcript"]
                run.identity("inputs", media.name, media)
                run.identity("inputs", transcript.name, transcript)
                assert hashlib.sha256(media.read_bytes()).hexdigest() == entry["sha256"]
                assert hashlib.sha256(transcript.read_bytes()).hexdigest() == entry["transcript_sha256"]
                document = json.loads(transcript.read_text(encoding="utf-8"))
                assert document["synthetic"] is True
                page.locator('[data-view="hearings"]').click()
                page.locator("#import-files").set_input_files(str(transcript))
                job_id = document["hearing"]["id"]
                expect(page.locator(f'[data-open="{job_id}"]')).to_be_visible()
                page.locator(f'[data-open="{job_id}"]').click()
                expect(page.locator("#audio-player")).to_be_hidden()
                before = server.store.get(job_id)["draft"]
                page.locator(f'[data-attach-audio="{job_id}"]').click()
                page.locator("#attach-audio-file").set_input_files(str(media))
                expect(page.locator("#audio-player")).to_be_visible()
                expect(page.locator("#audio-duration")).not_to_have_text("00:00:00")
                page.wait_for_function("""id => {
                    const audio = document.querySelector('#source-audio');
                    return audio.readyState >= 1 && Number.isFinite(audio.duration)
                        && new URL(audio.currentSrc).pathname.endsWith('/' + id);
                }""", arg=job_id)
                duration = page.locator("#source-audio").evaluate("audio => audio.duration")
                assert abs(duration - entry["pcm_duration_seconds"]) < .3, (entry["filename"], duration)
                job = server.store.get(job_id)
                assert job["draft"] == before and job["draft"]["synthetic"] is True
                assert job["audio"]["sha256"] == entry["sha256"]
                anchors = []
                for segment in (document["segments"][1], document["segments"][-2]):
                    page.locator(f'.evidence-content [data-segment="{segment["id"]}"] [data-evidence]').click()
                    page.wait_for_function(
                        """expected => {
                            const audio = document.querySelector('#source-audio');
                            return audio && audio.readyState >= 2 && !audio.paused
                                && audio.currentTime > expected + 0.02;
                        }""", arg=segment["start"], timeout=10000)
                    state = page.locator("#source-audio").evaluate(
                        "audio => ({time:audio.currentTime,paused:audio.paused,ready:audio.readyState})")
                    assert abs(state["time"] - segment["start"]) < 1, (segment, state)
                    assert state["ready"] >= 2 and not state["paused"], state
                    anchors.append({"segment_id": segment["id"], "expected_start": segment["start"],
                                    "observed_playback": state})
                    page.locator("#audio-play").click()
                page.screenshot(path=str(audit / (media.stem + ".png")), full_page=True)
                recordings.append({"filename": media.name, "decoded_duration": duration, "anchors": anchors})
                checks.append(f"{media.name}: decoded speech, preserved synthetic transcript, source attachment and two timestamp playback anchors")
            assert len(server.store.list()) == 3
            assert not errors, errors
            browser.close()
        report["status"] = "PASS"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        pass


def main(argv=None):
    return audit_main("speech", body, argv, latest=ROOT / ".scratch/speech-browser-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
