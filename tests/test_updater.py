"""Updates must never install a mismatched build, interrupt processing, or lose the old app."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from hearing_compiler import updater
from hearing_compiler.pipeline import InputError
from hearing_compiler.store import Store
from scripts import publish_mac_release
from test_pipeline import fixture


def manifest(build=5, **changes):
    data = {"version": "0.1.0", "build": build, "commit": "abcdef1", "tag": f"build-{build}",
            "zip": updater.ZIP_NAME, "sha256": "0" * 64, "bytes": 10}
    data.update(changes)
    return data


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state")
        self.app = self.root / "Applications" / "Hearing Compiler.app"
        self.app.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def status(self, published, current_build=3, app=None):
        with patch.object(updater, "current_app", return_value=app or self.app), \
                patch.object(updater, "latest", return_value=published), \
                patch.object(updater, "version_info", return_value={"label": "v0.1.0", "build": current_build}):
            return updater.status(self.store)

    def test_malformed_published_descriptions_are_rejected(self):
        self.assertEqual(updater.parse_manifest(manifest()), manifest())
        for bad in (manifest(build=0), manifest(sha256="x" * 64), manifest(zip="other.zip"),
                    manifest(tag="latest"), manifest(bytes=updater.MAX_ZIP_BYTES + 1), manifest(build=True)):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                updater.parse_manifest(bad)

    def test_newer_build_installs_once_automatically_unless_busy_or_misplaced(self):
        ready = self.status(manifest(5))
        self.assertEqual((ready["available"], ready["can_install"], ready["auto"]), (True, True, True))
        self.assertFalse(self.status(manifest(3))["available"])
        (self.store.directory / "updates").mkdir()
        (self.store.directory / "updates/attempted.json").write_text('["build-5"]', encoding="utf-8")
        retry = self.status(manifest(5))
        self.assertEqual((retry["can_install"], retry["auto"]), (True, False))
        translocated = self.root / "private/var/folders/x/AppTranslocation/y/Hearing Compiler.app"
        translocated.mkdir(parents=True)
        self.assertIn("Applications folder", self.status(manifest(6), app=translocated)["reason"])
        self.store.import_one(fixture("sample-hearing"))
        self.store.data["jobs"]["sample-hearing"]["processing"] = "Analyzing"
        busy = self.status(manifest(6))
        self.assertEqual((busy["available"], busy["can_install"]), (True, False))
        self.assertIn("processing", busy["reason"])

    def test_source_and_windows_runs_never_contact_the_release_server(self):
        with patch.object(updater, "current_app", return_value=None), \
                patch.object(updater, "latest", side_effect=AssertionError("no network")):
            result = updater.status(self.store)
        self.assertFalse(result["available"])
        self.assertIn("installed Mac app", result["reason"])

    def test_download_must_match_the_published_size_and_fingerprint(self):
        payload = b"verified update"
        good = manifest(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
        cases = {"good": good, "wrong hash": {**good, "sha256": "1" * 64}, "too large": {**good, "bytes": 3}}
        for label, published in cases.items():
            target = self.root / f"{label}.zip"
            with self.subTest(label), patch.object(updater.urllib.request, "urlopen",
                                                   return_value=Response(payload)):
                if label == "good":
                    updater.download(published, target)
                    self.assertEqual(target.read_bytes(), payload)
                else:
                    with self.assertRaises(InputError):
                        updater.download(published, target)

    @unittest.skipUnless(shutil.which("sh"), "Needs a POSIX shell")
    def test_swap_script_installs_after_exit_and_keeps_or_restores_the_old_app(self):
        shims, calls = self.root / "bin", self.root / "calls.log"
        shims.mkdir()
        for name in ("open", "xattr"):
            # `xattr -p` finds quarantine only once the test creates the "quarantined" file.
            found = f'\n[ "$1" != "-p" ] || [ -e "{(self.root / "quarantined").as_posix()}" ]' if name == "xattr" else ""
            (shims / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls.as_posix()}"{found}\n', newline="\n")
            (shims / name).chmod(0o755)
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"}

        def swap(staged_exists):
            current, staged = self.root / "app/Hearing Compiler.app", self.root / "staged/Hearing Compiler.app"
            previous = self.root / "updates/previous/Hearing Compiler.app"
            for path in (current.parent, staged.parent, previous.parent):
                shutil.rmtree(path, ignore_errors=True)
            current.mkdir(parents=True)
            (current / "old.txt").write_text("old")
            if staged_exists:
                staged.mkdir(parents=True)
                (staged / "new.txt").write_text("new")
            script = self.root / "install.sh"
            script.write_text(updater.install_script(current, staged, previous, exited.pid, self.root / "update.log"),
                              encoding="utf-8", newline="\n")
            subprocess.run(["sh", str(script)], env=env, check=False, timeout=60)
            return current, previous

        current, previous = swap(staged_exists=True)
        self.assertTrue((current / "new.txt").exists())
        self.assertTrue((previous / "old.txt").exists())
        # Nothing quarantined the download, so the installed app's files are left alone.
        self.assertNotIn("xattr -dr", calls.read_text())
        (self.root / "quarantined").touch()
        swap(staged_exists=True)
        self.assertIn("xattr -dr com.apple.quarantine", calls.read_text())
        current, previous = swap(staged_exists=False)
        self.assertTrue((current / "old.txt").exists())
        self.assertIn("open", calls.read_text())

    def test_updates_must_carry_the_pinned_release_signature(self):
        bundle = self.root / "Hearing Compiler.app"
        (bundle / "Contents/Resources").mkdir(parents=True)
        import plistlib
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": updater.BUNDLE_ID}))
        (bundle / "Contents/Resources/build-stamp.json").write_text('{"build": 5}', encoding="utf-8")
        with patch.object(updater.subprocess, "run") as codesign:
            updater.verify_bundle(bundle, manifest(5))
        command = codesign.call_args.args[0]
        self.assertIn(f'-R=certificate leaf = H"{updater.SIGNER_SHA1}"', command)
        self.assertEqual(command[:4], ["/usr/bin/codesign", "--verify", "--deep", "--strict"])
        with patch.object(updater.subprocess, "run", side_effect=subprocess.CalledProcessError(3, "codesign")),                 self.assertRaises(subprocess.CalledProcessError):
            updater.verify_bundle(bundle, manifest(5))

    @unittest.skipUnless(shutil.which("sh"), "Needs a POSIX shell")
    def test_new_version_must_start_or_the_previous_one_is_put_back(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        healthy, failed = updater.health_marker(self.root, 9), updater.failed_marker(self.root, 9)

        def attempt(starts):
            shims = self.root / f"bin-{starts}"
            shims.mkdir()
            # Opening the app "starts" it only when this attempt should succeed.
            body = f'mkdir -p "{healthy.parent.as_posix()}"; touch "{healthy.as_posix()}"' if starts else "true"
            (shims / "open").write_text(f"#!/bin/sh\n{body}\n")
            (shims / "xattr").write_text("#!/bin/sh\ntrue\n")
            for shim in shims.iterdir():
                shim.chmod(0o755)
            current, staged = self.root / "app/Hearing Compiler.app", self.root / "staged/Hearing Compiler.app"
            previous = self.root / "updates/previous/Hearing Compiler.app"
            for path in (current.parent, staged.parent, previous.parent):
                shutil.rmtree(path, ignore_errors=True)
            current.mkdir(parents=True)
            (current / "old.txt").write_text("old")
            staged.mkdir(parents=True)
            (staged / "new.txt").write_text("new")
            script = self.root / "install.sh"
            with patch.object(updater, "HEALTH_SECONDS", 3):
                script.write_text(updater.install_script(current, staged, previous, exited.pid, self.root / "update.log",
                                                         healthy, failed), encoding="utf-8", newline="\n")
            env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"}
            subprocess.run(["sh", str(script)], env=env, check=False, timeout=60)
            return current

        current = attempt(starts=True)
        self.assertTrue((current / "new.txt").exists())
        self.assertFalse(failed.exists())
        current = attempt(starts=False)
        self.assertTrue((current / "old.txt").exists())  # Rolled back.
        self.assertTrue(failed.exists())
        self.assertIn("previous version restored", (self.root / "update.log").read_text())
        with patch.object(updater, "current_app", return_value=self.app),                 patch.object(updater, "latest", return_value=manifest(9)),                 patch.object(updater, "version_info", return_value={"label": "v0.1.0", "build": 8}):
            state = updater.status(Store(self.root))
        self.assertEqual((state["available"], state["can_install"]), (True, False))
        self.assertIn("didn't start", state["reason"])

    @unittest.skipUnless(shutil.which("sh"), "Needs a POSIX shell")
    def test_a_refused_move_reopens_the_unchanged_app_without_killing_it_and_says_why(self):
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait()
        current, staged = self.root / "app/Hearing Compiler.app", self.root / "staged/Hearing Compiler.app"
        for app in (current, staged):
            app.mkdir(parents=True)
        (current / "old.txt").write_text("old")
        healthy, failed, blocked = (marker(self.root, 9) for marker in
                                    (updater.health_marker, updater.failed_marker, updater.blocked_marker))
        shims, calls = self.root / "bin", self.root / "calls.log"
        shims.mkdir()
        # macOS refuses only moving the installed app (as it can outside Applications).
        refuse = (f'if [ "$1" = {shlex.quote(str(current))} ]; then echo "mv: Operation not permitted" >&2; exit 1; fi\n'
                  'exec /bin/mv "$@"')
        for name, body in (("mv", refuse), ("open", "true"), ("pkill", "true"), ("xattr", "true")):
            (shims / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls.as_posix()}"\n{body}\n', newline="\n")
            (shims / name).chmod(0o755)
        script = self.root / "install.sh"
        with patch.object(updater, "HEALTH_SECONDS", 2):
            script.write_text(updater.install_script(current, staged, self.root / "updates/previous/Hearing Compiler.app",
                                                     exited.pid, self.root / "update.log", healthy, failed, blocked),
                              encoding="utf-8", newline="\n")
        env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"}
        self.assertEqual(subprocess.run(["sh", str(script)], env=env, check=False, timeout=60).returncode, 1)
        commands = [line.split()[0] for line in calls.read_text().splitlines()]
        self.assertEqual((commands.count("open"), commands.count("pkill")), (1, 0))  # Reopened, never killed.
        self.assertTrue((current / "old.txt").exists())
        self.assertEqual((failed.exists(), blocked.exists()), (False, True))
        self.assertIn("could not move the current app aside; nothing changed", (self.root / "update.log").read_text())
        with patch.object(updater, "current_app", return_value=self.app), \
                patch.object(updater, "latest", return_value=manifest(9)), \
                patch.object(updater, "version_info", return_value={"label": "v0.1.0", "build": 8}):
            state = updater.status(Store(self.root))
        self.assertEqual((state["available"], state["can_install"]), (True, False))
        self.assertIn("macOS didn't let Hearing Compiler replace itself, so nothing changed", state["reason"])

    def test_the_copy_kept_for_rollback_never_updates_itself(self):
        kept = self.store.directory / "updates/previous/Hearing Compiler.app"
        kept.mkdir(parents=True)
        state = self.status(manifest(5), app=kept)
        self.assertEqual((state["available"], state["can_install"], state["auto"]), (True, False, False))
        self.assertIn("previous version kept for rollback. Click Stop app", state["reason"])
        self.assertTrue(self.status(manifest(5))["can_install"])  # The copy in Applications still updates.
        with patch.object(updater, "current_app", return_value=kept), \
                patch.object(updater, "latest", return_value=manifest(5)), \
                patch.object(updater, "version_info", return_value={"label": "v0.1.0", "build": 3}), \
                patch.object(updater, "download", side_effect=AssertionError("must not download")), \
                self.assertRaisesRegex(InputError, "kept for rollback"):
            updater.install(self.store)
        self.assertTrue(kept.exists())

    def test_a_second_install_during_the_download_is_refused_and_a_failed_one_frees_the_next(self):
        downloading, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def slow_download(published, target):
            downloading.set()
            finish.wait(10)
            raise InputError("The update download does not match its published fingerprint; it was discarded.")

        with patch.object(updater, "current_app", return_value=self.app), \
                patch.object(updater, "latest", return_value=manifest(5)), \
                patch.object(updater, "version_info", return_value={"label": "v0.1.0", "build": 3}), \
                patch.object(updater, "download", side_effect=slow_download):
            with ThreadPoolExecutor(1) as pool:
                first = pool.submit(updater.install, self.store)
                self.assertTrue(downloading.wait(10))
                # A reload mid-download must not start a second install into the same folder.
                with self.assertRaisesRegex(InputError, "already downloading. Keep this tab open"):
                    updater.install(self.store)
                finish.set()
                with self.assertRaisesRegex(InputError, "fingerprint"):
                    first.result(10)
            with self.assertRaisesRegex(InputError, "fingerprint"):  # The failure freed the next attempt.
                updater.install(self.store)

    def test_closing_the_tab_during_an_update_still_stops_the_app_so_the_swap_can_run(self):
        from hearing_compiler.server import make_server
        installing = {"installing": "v0.1.0 · build 5 · abcdef1"}
        for tab_open in (True, False):
            with self.subTest(tab_open=tab_open), tempfile.TemporaryDirectory() as folder, \
                    patch.object(updater, "install", return_value=installing):
                server = make_server(Path(folder), 0)
                send = server.RequestHandlerClass.send

                def reply(handler, status, payload, *args, **kwargs):
                    if payload == installing and not tab_open:
                        raise ConnectionAbortedError("The browser tab was closed.")
                    return send(handler, status, payload, *args, **kwargs)

                serving = threading.Thread(target=server.serve_forever, daemon=True)
                with patch.object(server.RequestHandlerClass, "send", reply):
                    serving.start()
                    base = f"http://127.0.0.1:{server.server_port}"
                    token = json.loads(urlopen(base + "/api/session", timeout=10).read())["token"]
                    try:
                        answer = json.loads(urlopen(Request(base + "/api/update/install", data=b"{}", headers={
                            "Content-Type": "application/json", "X-Review-Token": token}), timeout=10).read())
                    except OSError:
                        answer = None
                    serving.join(10)
                    stopped = not serving.is_alive()
                    if not stopped:
                        server.shutdown()
                    server.server_close()
                self.assertTrue(stopped, "The app kept running, so the update could never replace it.")
                self.assertEqual(answer, installing if tab_open else None)

    def test_publisher_releases_only_as_prerelease_never_reuses_numbers_and_keeps_latest(self):
        archive = self.root / updater.ZIP_NAME
        archive.write_bytes(b"zip bytes")
        described = publish_mac_release.manifest(archive, 12, "d728eddeedd9162790dd2f0303011dbdc9411893")
        self.assertEqual((described["tag"], described["commit"], described["bytes"]), ("build-12", "d728edd", 9))
        self.assertEqual(described["sha256"], hashlib.sha256(b"zip bytes").hexdigest())
        dist = self.root / "dist/macOS"
        dist.mkdir(parents=True)
        shutil.copy(archive, dist / updater.ZIP_NAME)
        # Build 1 is the promoted latest; 2-11 are pre-releases. GitHub dates each release by its tag's
        # commit, and every tag points at the releases repository's one commit, so all dates tie.
        listed = [{"tagName": f"build-{n}", "createdAt": "2026-10-13T17:28:29Z", "isLatest": n == 1}
                  for n in range(1, 12)]
        calls = []

        def fake_gh(*args, capture=False):
            calls.append(args)
            if args[:2] == ("release", "create"):
                listed.append({"tagName": args[2], "createdAt": "2026-10-13T17:28:29Z", "isLatest": False})
            return json.dumps(listed) if capture else ""

        def publish(build):
            with patch.object(publish_mac_release, "ROOT", self.root), patch.object(publish_mac_release, "gh", fake_gh),                     patch.dict(os.environ, {"RELEASES_REPO": "owner/releases", "GITHUB_RUN_NUMBER": str(build),
                                            "GITHUB_SHA": "d728eddeedd9162790dd2f0303011dbdc9411893"}),                     patch("builtins.print"):
                publish_mac_release.main([])

        publish(12)
        create = next(c for c in calls if c[:2] == ("release", "create"))
        self.assertIn("--prerelease", create)
        self.assertNotIn("--latest", create)
        # Promotion keeps the notes, so they must still be true on the latest release's download page.
        notes = create[create.index("--notes") + 1]
        self.assertIn(f"download {updater.ZIP_NAME} under Assets (not Source code)", notes)
        self.assertIn("https://github.com/owner/releases#first-install", notes)
        self.assertNotRegex(notes, "(?i)pre-release|under test")
        # Twelve releases: the lowest non-latest build (build-2) is pruned; promoted build-1 and new build-12 survive.
        self.assertEqual([c[2] for c in calls if c[:2] == ("release", "delete")], ["build-2"])
        with self.assertRaisesRegex(SystemExit, "not newer"):
            publish(5)
        calls.clear()
        checked = []
        signature = patch.object(publish_mac_release, "opens_on_a_mac", lambda repo, build: checked.append(build))
        with patch.object(publish_mac_release, "gh", fake_gh), patch.dict(os.environ, {"RELEASES_REPO": "owner/releases"}), \
                signature, patch("builtins.print"):
            publish_mac_release.main(["promote", "12"])
            with self.assertRaisesRegex(SystemExit, "not published"):
                publish_mac_release.main(["promote", "99"])
        self.assertIn(("release", "edit", "build-12", "--repo", "owner/releases", "--prerelease=false", "--latest"), calls)
        self.assertEqual(checked, [12])  # Every release first passes the signature check.
        # With no number, the newest pre-release newer than the current latest is released.
        for release in listed:
            release["isPrerelease"] = not release["isLatest"]
        calls.clear()
        with patch.object(publish_mac_release, "gh", fake_gh), patch.dict(os.environ, {"RELEASES_REPO": "owner/releases"}), \
                signature, patch("builtins.print"):
            self.assertEqual(publish_mac_release.promote("owner/releases"), 12)
            for release in listed:
                release["isLatest"], release["isPrerelease"] = release["tagName"] == "build-12", release["tagName"] != "build-12"
            with self.assertRaisesRegex(SystemExit, "Nothing newer than build 12"):
                publish_mac_release.promote("owner/releases")
