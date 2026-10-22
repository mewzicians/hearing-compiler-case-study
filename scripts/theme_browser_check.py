"""Theme persistence, accessible contrast and both review formats in a disposable workspace."""
from __future__ import annotations

import io
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
    from hearing_compiler.audio import receive_audio
    from hearing_compiler.rich_review import from_packets
    from audio_test_helpers import wav_bytes
    from test_pipeline import fixture
    from test_rich_review import joint_packet

    document, packet = joint_packet()
    server = make_server(audit / "state", 0)
    server.store.extractor = lambda source: from_packets(source, [packet], {})
    rich_id = server.store.import_one(document)["id"]
    server.store.extractor = None
    legacy_id = server.store.import_one(fixture("sample-hearing"))["id"]
    audio = wav_bytes(45)
    asset = receive_audio(server.store.directory, io.BytesIO(audio), len(audio), "fictional.wav")
    server.store.attach_audio(rich_id, asset)
    before = server.store.get(rich_id)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def stop():
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()
    run.cleanup(stop)
    run.data["limits"] = "Synthetic browser usability/contrast checks; no hearing or model accuracy claim."
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("HEARING_TEST_BROWSER"), headless=True)
        run.data["versions"]["browser"] = browser.version
        context = browser.new_context(color_scheme="dark", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        address = f"http://127.0.0.1:{server.server_port}"
        page.goto(address)
        toggle = page.locator("#theme-toggle")  # Its label switches between Dark mode and Light mode.
        expect(toggle).to_have_attribute("aria-pressed", "true")
        toggle.focus()
        page.keyboard.press("Space")
        expect(toggle).to_have_attribute("aria-pressed", "false")
        page.emulate_media(color_scheme="dark")
        page.reload()
        expect(toggle).to_have_attribute("aria-pressed", "false")
        run.data["checks"].append("System default and keyboard toggle work; explicit light preference survives reload")
        toggle.click()
        page.reload()
        expect(toggle).to_have_attribute("aria-pressed", "true")
        second = context.new_page()
        second.goto(address)
        expect(second.locator("#theme-toggle")).to_have_attribute("aria-pressed", "true")
        second.locator("#theme-toggle").click()
        expect(toggle).to_have_attribute("aria-pressed", "false")
        second.close()
        run.data["checks"].append("Explicit dark preference survives reload and new tabs; changes synchronize open tabs")
        other_server = make_server(audit / "other-port-state", 0)
        other_thread = threading.Thread(target=other_server.serve_forever, daemon=True)
        other_thread.start()
        def stop_other():
            other_server.shutdown()
            other_server.server_close()
            other_thread.join(5)
            assert not other_thread.is_alive()
        run.cleanup(stop_other)
        other = context.new_page()
        other.goto(f"http://127.0.0.1:{other_server.server_port}")
        expect(other.locator("#theme-toggle")).to_have_attribute("aria-pressed", "false")
        other.locator("#theme-toggle").click()
        other.close()
        page.reload()
        expect(toggle).to_have_attribute("aria-pressed", "true")
        sibling = context.new_page()
        sibling.goto(address)
        sibling.locator("#theme-toggle").click()
        expect(toggle).to_have_attribute("aria-pressed", "false")
        sibling.close()
        run.data["checks"].append("Appearance-only preference survives different launcher ports; newer shared choice overrides stale origin storage")
        migrated = browser.new_context(color_scheme="light")
        migrated.add_init_script(
            f"if (location.port === '{server.server_port}') localStorage.setItem('hearing-compiler-theme', 'dark');")
        old_choice = migrated.new_page()
        old_choice.goto(address)
        expect(old_choice.locator("#theme-toggle")).to_have_attribute("aria-pressed", "true")
        old_choice.goto(f"http://127.0.0.1:{other_server.server_port}")
        expect(old_choice.locator("#theme-toggle")).to_have_attribute("aria-pressed", "true")
        run.data["checks"].append("Existing explicit origin preference migrates to cross-port persistence without saving the system default")

        # Actual rendered foreground/background pairs, including warnings and controls.
        contrast = """() => {
          function rgb(s) { return s.match(/[\\d.]+/g).map(Number).slice(0,3); }
          function luminance(c) { return c.map(x => { x/=255; return x<=.04045 ? x/12.92 : ((x+.055)/1.055)**2.4; }).reduce((v,x,i)=>v+x*[.2126,.7152,.0722][i],0); }
          return [...document.querySelectorAll('.card,.muted,.pill,.primary,.secondary,.uncertain .field-note,.selected .time-button,dialog input,dialog textarea')]
            .filter(e => e.getClientRects().length).map(e => {
              let p=e, background;
              while (p) { const c=getComputedStyle(p).backgroundColor;
                if (c!=='rgba(0, 0, 0, 0)' && c!=='transparent') { background=c; break; } p=p.parentElement; }
              const a=luminance(rgb(getComputedStyle(e).color)), b=luminance(rgb(background));
              return {selector:e.className || e.id,ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
            });
        }"""
        run.data["contrast"] = {}
        for theme in ("light", "dark"):
            if page.locator("html").get_attribute("data-theme") != theme:
                toggle.click()
            page.locator('[data-view="hearings"]').click()
            page.locator(f'[data-open="{rich_id}"]').click()
            page.locator('[data-evidence="joint4"]').first.click()
            expect(page.locator('.segment.selected')).to_be_visible()
            expect(page.locator("#audio-player")).to_be_visible()
            page.locator('[data-edit="/units/0/arguments/0/text"]').click()
            expect(page.locator("#edit-dialog")).to_be_visible()
            samples = page.evaluate(contrast)
            assert samples and all(item["ratio"] >= 4.5 for item in samples), samples
            run.data["contrast"][theme] = samples
            page.screenshot(path=str(audit / f"{theme}-editor.png"))
            page.locator("#edit-dialog").evaluate("(dialog) => dialog.close()")
            page.locator('[data-view="output"]').click()
            expect(page.locator(".document-preview")).to_contain_text("written authorization")
            page.screenshot(path=str(audit / f"{theme}-output.png"), full_page=True)
            page.locator('[data-view="hearings"]').click()
            page.locator(f'[data-open="{legacy_id}"]').click()
            expect(page.locator("#draft-heading")).to_be_visible()
            page.get_by_role("button", name="AI extraction", exact=True).click()
            expect(page.locator("#extraction-dialog")).to_be_visible()
            page.locator("#close-extraction").click()
        run.data["checks"].append("Both themes meet 4.5:1 text contrast in rich review, evidence, controls and editor; legacy review and settings remain usable")
        page.set_viewport_size({"width": 390, "height": 844})
        expect(toggle).to_be_visible()
        expect(page.get_by_role("button", name="Backup and recovery", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(audit / "dark-mobile.png"), full_page=True)
        run.data["checks"].append("Theme and recovery/settings controls remain available at narrow width without page overflow")
        no_storage = browser.new_context(color_scheme="light")
        no_storage.add_init_script("Object.defineProperty(window, 'localStorage', {get(){throw new Error('disabled');}})")
        fallback = no_storage.new_page()
        fallback.on("pageerror", lambda error: errors.append(str(error)))
        fallback.goto(address)
        fallback.locator("#theme-toggle").click()
        expect(fallback.locator("#theme-toggle")).to_have_attribute("aria-pressed", "true")
        other_fallback = no_storage.new_page()
        other_fallback.goto(f"http://127.0.0.1:{other_server.server_port}")
        expect(other_fallback.locator("#theme-toggle")).to_have_attribute("aria-pressed", "true")
        blocked_cookies = browser.new_context(color_scheme="light")
        blocked_cookies.add_init_script("Object.defineProperty(document, 'cookie', {get(){return '';},set(_){}})")
        limited = blocked_cookies.new_page()
        limited.goto(address)
        limited.locator("#theme-toggle").click()
        expect(limited.locator("#theme-status")).to_be_visible()
        expect(limited.locator("#theme-status")).to_contain_text("another port")
        limited.reload()
        expect(limited.locator("#theme-toggle")).to_have_attribute("aria-pressed", "true")
        readable_old_cookie = browser.new_context(color_scheme="light")
        readable_old_cookie.add_cookies([{"name": "hearing_compiler_theme", "value": "dark",
                                         "url": address, "sameSite": "Strict"}])
        readable_old_cookie.add_init_script("""const cookieDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
          Object.defineProperty(document, 'cookie', {get(){return cookieDescriptor.get.call(document);},set(_){}});""")
        newer_local = readable_old_cookie.new_page()
        newer_local.goto(address)
        expect(newer_local.locator("#theme-toggle")).to_have_attribute("aria-pressed", "true")
        newer_local.locator("#theme-toggle").click()
        newer_local.reload()
        expect(newer_local.locator("#theme-toggle")).to_have_attribute("aria-pressed", "false")
        expect(newer_local.locator("#theme-status")).to_contain_text("this address")
        blocked_all = browser.new_context(color_scheme="light")
        blocked_all.add_init_script("Object.defineProperty(window, 'localStorage', {get(){throw new Error('disabled');}});Object.defineProperty(document, 'cookie', {get(){return '';},set(_){}})")
        temporary = blocked_all.new_page()
        temporary.goto(address)
        temporary.locator("#theme-toggle").click()
        expect(temporary.locator("#theme-toggle")).to_have_attribute("aria-pressed", "true")
        expect(temporary.locator("#theme-status")).to_contain_text("could not save")
        assert server.store.get(rich_id)["draft"] == before["draft"]
        assert not errors, errors
        run.data["checks"].append("Disabled storage/cookies preserve usable controls and honest persistence notices; themes never change saved hearing data")
        browser.close()


def main(argv=None):
    return audit_main("theme-browser", body, argv, latest=ROOT / ".scratch/theme-browser-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
