from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import build_macos, mac_signature_check
from scripts.build_windows import FILES, promote


def macho(flags, team=None, entitlements=False):
    """A tiny signed arm64 Mach-O: header, LC_CODE_SIGNATURE and a SuperBlob with one CodeDirectory."""
    ident, team_name = b"org.example.tool\0", team.encode() + b"\0" if team else b""
    directory = struct.pack(">9I4B3I", 0xFADE0C02, 52 + len(ident) + len(team_name), 0x20200, flags, 0, 52,
                            0, 0, 0, 32, 2, 0, 12, 0, 0, 52 + len(ident) if team else 0) + ident + team_name
    blobs = [(0, directory)] + ([(5, struct.pack(">2I", 0xFADE7171, 17) + b"<plist/>\n")] if entitlements else [])
    start, index, body = 12 + 8 * len(blobs), b"", b""
    for kind, blob in blobs:
        index, body = index + struct.pack(">2I", kind, start + len(body)), body + blob
    signature = struct.pack(">3I", 0xFADE0CC0, start + len(body), len(blobs)) + index + body
    return (struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 16, 0, 0)
            + struct.pack("<4I", 0x1D, 16, 48, len(signature)) + signature)


def universal(*images):
    entries, body, start = b"", b"", 8 + 20 * len(images)
    for image in images:
        entries, body = entries + struct.pack(">5I", 0x0100000C, 0, start + len(body), len(image), 14), body + image
    return struct.pack(">2I", 0xCAFEBABE, len(images)) + entries + body


class MacSignatureTests(unittest.TestCase):
    def check(self, members):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "release.zip"
            with zipfile.ZipFile(archive, "w") as output:
                for name, data in members.items():
                    output.writestr("HearingCompilerPrototype-Mac-AppleSilicon/Hearing Compiler.app/Contents/" + name, data)
            shown = io.StringIO()
            with contextlib.redirect_stdout(shown):
                code = mac_signature_check.main([str(archive)])
            return code, shown.getvalue()

    def test_release_zip_fails_when_any_program_has_the_hardened_runtime_without_a_team_id(self):
        clean = {"MacOS/Hearing Compiler": macho(0), "Frameworks/libssl.3.dylib": macho(0),
                 "Resources/README.txt": b"plain text"}
        code, shown = self.check(clean)
        self.assertEqual(code, 0)
        self.assertIn("flags=0x0(none)  team=none  entitlements=no  HearingCompilerPrototype-Mac-AppleSilicon/"
                      "Hearing Compiler.app/Contents/MacOS/Hearing Compiler", shown)
        self.assertIn("2 Mach-O files:", shown)
        # Build 20's state: the hardened runtime on a self-signed (no Team ID) program, here or in a nested library.
        for flagged in ({**clean, "MacOS/Hearing Compiler": macho(0x10000)},
                        {**clean, "Frameworks/Python.framework/Versions/3.13/Python": universal(macho(0), macho(0x10000))}):
            code, shown = self.check(flagged)
            self.assertEqual(code, 1)
            self.assertIn("flags=0x10000(runtime)  team=none", shown)
            self.assertIn("FAIL: 1 Mach-O file(s) have the hardened runtime without a Team ID", shown)
        # A Developer ID-style signature (runtime with a Team ID) loads its own libraries.
        code, shown = self.check({"MacOS/Hearing Compiler": macho(0x10000, team="ABCDE12345", entitlements=True)})
        self.assertEqual(code, 0)
        self.assertIn("flags=0x10000(runtime)  team=ABCDE12345  entitlements=yes", shown)
        self.assertEqual(self.check({"MacOS/Hearing Compiler": macho(0)[:60]})[0], 1)  # Truncated signature.
        self.assertEqual(self.check({"Resources/README.txt": b"no programs"})[0], 1)

    def test_a_build_that_fails_the_signature_check_is_never_released(self):
        from scripts import publish_mac_release
        for flags, released in ((0x10000, False), (0, True)):
            def fake_gh(*args, capture=False):
                folder = Path(args[args.index("--dir") + 1])
                with zipfile.ZipFile(folder / publish_mac_release.ZIP_NAME, "w") as output:
                    output.writestr("HearingCompilerPrototype-Mac-AppleSilicon/Hearing Compiler.app/Contents/MacOS/"
                                    "Hearing Compiler", macho(flags))
                return ""
            with self.subTest(flags=flags), patch.object(publish_mac_release, "gh", fake_gh), \
                    contextlib.redirect_stdout(io.StringIO()):
                if released:
                    publish_mac_release.opens_on_a_mac("owner/releases", 21)
                else:
                    with self.assertRaisesRegex(SystemExit, "build-20 failed the signature check"):
                        publish_mac_release.opens_on_a_mac("owner/releases", 20)

    def test_release_signing_drops_the_hardened_runtime_and_refuses_it_without_a_team_id(self):
        identity = "5985916D7FA9DCF67214E8538204703F1E1DC813"
        shown = ("Identifier=org.hearingcompiler.prototype\nFormat=app bundle with Mach-O thin (arm64)\n"
                 "CodeDirectory v=20500 size=29022 flags={flags} hashes=896+7 location=embedded\n"
                 "Authority=Hearing Compiler self-signed release signing\nTeamIdentifier={team}\n")
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "Hearing Compiler.app"
            (app / "Contents/Frameworks/Python.framework").mkdir(parents=True)

            def sign(flags="0x0(none)", team="not set", key=identity):
                with patch.object(build_macos.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, "", shown.format(flags=flags, team=team))) as run:
                    build_macos.sign_release(app, key)
                return [call.args[0] for call in run.call_args_list]

            commands = sign()
            self.assertEqual(commands[0], ["/usr/bin/codesign", "--force", "--deep", "--timestamp", "-s", identity, str(app)])
            self.assertFalse([arg for arg in commands[0] if "option" in arg or "runtime" in arg])
            self.assertEqual(commands[1:], [["/usr/bin/codesign", "-d", "--verbose=2", str(code)]
                                            for code in (app, app / "Contents/Frameworks/Python.framework")])
            with self.assertRaisesRegex(RuntimeError, "hardened runtime but no Team ID"):
                sign(flags="0x10000(runtime)")
            sign(flags="0x10000(runtime)", team="ABCDE12345")  # With a Team ID, library validation can pass.
            self.assertEqual(len(sign(key="")), 2)  # Builds without an identity stay ad hoc but are still checked.
        self.assertTrue(build_macos.hardened_without_team("CodeDirectory v=20500 flags=0x10002(adhoc,runtime)"))


def candidate(root, status="PASS"):
    folder = root / ".scratch/candidate"
    folder.mkdir(parents=True)
    for name in FILES[:3]:
        (folder / name).write_bytes(("new-" + name).encode())
    report = {"status": status, "executable_sha256": hashlib.sha256((folder / FILES[0]).read_bytes()).hexdigest(),
              "zip_sha256": hashlib.sha256((folder / FILES[1]).read_bytes()).hexdigest(),
              "build_info_sha256": hashlib.sha256((folder / FILES[2]).read_bytes()).hexdigest()}
    report.update(package_run_id="11111111-1111-1111-1111-111111111111", package_process_exit=0, working_tree_dirty=False)
    (folder / FILES[3]).write_text(json.dumps(report), encoding="utf-8")
    audit = folder / "package-audit"
    audit.mkdir()
    (audit / "report.json").write_text(json.dumps({
        "status": status, "report_version": 1, "run_id": report["package_run_id"],
        "run_status": "COMPLETED", "ended_utc": "2026-10-06T00:00:00+00:00",
        "result_status": status, "failures": [],
        "artifacts": {"executable": {"sha256": report["executable_sha256"]}},
        "executable_sha256": report["executable_sha256"]}))
    return folder


class BuildPromotionTests(unittest.TestCase):
    def test_stale_runs_artifacts_process_failures_and_dirty_source_cannot_promote(self):
        for field, value in [("package_run_id", "different-run"), ("package_process_exit", 1),
                             ("working_tree_dirty", True)]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                folder = candidate(root)
                report = json.loads((folder / FILES[3]).read_text())
                report[field] = value
                (folder / FILES[3]).write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    promote(folder, root / "dist", root / ".scratch/backup", root)
                self.assertFalse((root / ".scratch/backup").exists())

    def test_failed_audit_or_changed_candidate_preserves_previous_bundle(self):
        for mode in ("failed", "changed"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                folder = candidate(root, "FAIL" if mode == "failed" else "PASS")
                destination = root / "dist"
                destination.mkdir()
                old = destination / FILES[0]
                old.write_bytes(b"previous-working-executable")
                if mode == "changed":
                    (folder / FILES[0]).write_bytes(b"unexpected replacement")
                with self.assertRaises(ValueError):
                    promote(folder, destination, root / ".scratch/backup", root)
                self.assertEqual(old.read_bytes(), b"previous-working-executable")
                self.assertFalse((root / ".scratch/backup").exists())

    def test_success_keeps_the_previous_artifacts_in_a_local_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = candidate(root)
            destination = root / "dist"
            destination.mkdir()
            for name in FILES:
                (destination / name).write_bytes(("old-" + name).encode())
            backup = root / ".scratch/backup"
            promote(folder, destination, backup, root)
            for name in FILES:
                self.assertEqual((destination / name).read_bytes(), (folder / name).read_bytes())
                self.assertEqual((backup / name).read_bytes(), ("old-" + name).encode())

    def test_interrupted_promotion_restores_all_already_replaced_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = candidate(root)
            destination = root / "dist"
            destination.mkdir()
            for name in FILES:
                (destination / name).write_bytes(("old-" + name).encode())
            import os
            original = os.replace
            count = 0

            def fail_second(source, target):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("simulated interrupted promotion")
                return original(source, target)

            with patch("scripts.build_windows.os.replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    promote(folder, destination, root / ".scratch/backup", root)
            for name in FILES:
                self.assertEqual((destination / name).read_bytes(), ("old-" + name).encode())


if __name__ == "__main__":
    unittest.main()
