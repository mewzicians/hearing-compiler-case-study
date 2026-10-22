"""Browser fault regressions from the independent recovery audit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.audit_reporting import audit_main, browser_setup


def body(run, argv):
    audit = browser_setup(run, argv)
    from playwright.sync_api import expect, sync_playwright
    from hearing_compiler.server import make_server
    source = ROOT / "fixtures/transcripts/sample-hearing.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document["hearing"]["id"] = "recovery-browser-regression"
    run.identity("inputs", "synthetic_source", source)
    run.data["limits"] = "Synthetic network/concurrency regressions; no human hearing review, Mac integration or model-quality claim."
    server = make_server(audit / "state", 0)
    server.store.import_one(document)
    job_id = document["hearing"]["id"]
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
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        run.data["versions"]["browser"] = browser.version
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        address = f"http://127.0.0.1:{server.server_port}"
        def open_job():
            page.goto(address)
            page.locator(f'[data-open="{job_id}"]').click()
            expect(page.locator(".draft-pane")).to_be_visible()
        def add_argument(value):
            page.get_by_role("button", name="Add argument", exact=True).first.click()
            page.locator("#repair-value").fill(value)
            page.locator('[data-evidence-choice="repair-evidence"]').first.check()
        def count(value):
            return sum(a["value"] == value for a in server.store.get(job_id)["draft"]["testimonies"][0]["statements"][0]["arguments"])
        open_job()
        page.evaluate("state.stopped = true")
        def abort_queue(route):
            route.abort("failed")
        page.route("**/api/hearings", abort_queue)
        value = "Entered once despite failed queue refresh."
        add_argument(value)
        page.locator("#save-structure").click()
        expect(page.locator("#structure-dialog")).not_to_be_visible()
        assert count(value) == 1
        assert not page.evaluate("state.saveError")
        expect(page.locator("#global-error")).to_contain_text("Review was saved")
        page.unroute("**/api/hearings", abort_queue)
        checks.append("Confirmed save closes the repair even when its queue refresh fails; exactly one repair persists")

        open_job()
        page.evaluate("state.stopped = true")
        committed = []
        def lose_post_response(route):
            response = route.fetch()
            assert response.status == 200
            committed.append(response.status)
            route.abort("failed")
        page.route("**/api/review", lose_post_response)
        value = "Entered once despite lost save response."
        add_argument(value)
        page.locator("#save-structure").click()
        expect(page.locator("#structure-error")).to_be_visible()
        assert count(value) == 1 and committed == [200]
        page.unroute("**/api/review", lose_post_response)
        page.locator("#save-structure").click()
        expect(page.locator("#structure-dialog")).not_to_be_visible()
        assert count(value) == 1
        checks.append("Lost POST response is retried with the same bound request ID and cannot duplicate a saved repair")

        # A detail fetch already in flight must not change the form's revision.
        open_job()
        old = server.store.get(job_id)
        pending = []
        def hold_detail(route):
            pending.append(route)
        page.route(f"**/api/hearings/{job_id}", hold_detail)
        updated = server.store.review(job_id, old["revision"],
            [{"path": "/testimonies/0/name", "value": "Other tab preserved correction", "review": "APPROVED"}])
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        assert pending, "The delayed poll did not start"
        page.locator('[data-edit="/testimonies/0/name"]').click()
        previous_text = page.locator("#edit-value").input_value()
        pending.pop(0).fulfill(status=200, content_type="application/json", body=json.dumps(updated))
        page.wait_for_function("() => !polling")
        assert page.evaluate("state.job.revision") == old["revision"]
        assert page.locator("#edit-value").input_value() == previous_text
        page.unroute(f"**/api/hearings/{job_id}", hold_detail)
        page.locator("#save-edit").click()
        expect(page.locator("#edit-error")).to_contain_text("another tab")
        assert server.store.get(job_id) == updated
        checks.append("An in-flight poll cannot change an open form's source revision; stale save rejects without overwriting the other tab")

        # All output paths must use the same saved revision.
        open_job()
        page.evaluate("state.stopped = true")
        loaded = server.store.get(job_id)
        newest = server.store.review(job_id, loaded["revision"],
            [{"path": "/questions/0/asker", "value": "Newest saved asker", "review": "APPROVED"}])
        page.locator('[data-view="output"]').click()
        downloads = []
        page.on("download", lambda download: downloads.append(download.suggested_filename))
        for action in ("download", "docx", "snapshot", "copy"):
            page.locator(f'[data-action="{action}"]').click()
            expect(page.locator("#global-error")).to_contain_text("another tab")
        assert downloads == []
        assert server.store.get(job_id) == newest
        checks.append("Stale Markdown, Word, JSON and clipboard output all reject the same revision mismatch")
        assert not errors, errors
        context.close()
        browser.close()


def main(argv=None):
    return audit_main("recovery-browser", body, argv, latest=ROOT / ".scratch/recovery-browser-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
