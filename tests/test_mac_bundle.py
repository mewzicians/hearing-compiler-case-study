"""Reject the reproduced resource/source substitutions, with a valid bundle."""
import copy
import tempfile
import unittest
from pathlib import Path

from scripts.build_macos import bundle_inventory, certify_app, digest, source_files


class MacBundleTests(unittest.TestCase):
    def test_nested_runtime_modules_are_in_build_manifest(self):
        files = source_files()
        for name in ("__init__", "schema", "sources", "validator"):
            self.assertIn(f"hearing_compiler/model_contract/{name}.py", files)

    def test_full_bundle_and_source_identity_with_unchanged_positive(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "Compiler.app"
            exe = app / "Contents/MacOS/Compiler"
            asset = app / "Contents/Resources/theme.js"
            exe.parent.mkdir(parents=True)
            asset.parent.mkdir(parents=True)
            exe.write_bytes(b"synthetic executable")
            asset.write_text("original")
            source = {"commit": "selected", "dirty": False, "snapshot_sha256": "source"}
            sources = {"runtime.py": "source-hash"}
            inventory = bundle_inventory(app)
            report = {"report_version": 1, "run_id": "run", "run_status": "COMPLETED",
                      "ended_utc": "now", "result_status": "PASS", "status": "PASS", "failures": [],
                      "artifacts": {"executable": {"sha256": digest(exe)}},
                      "source": source, "build_source_files": sources, "bundle_files": inventory}
            certify_app(report, "run", exe, app, source, sources, inventory)
            wrong = copy.deepcopy(report)
            wrong["source"]["commit"] = "different"
            with self.assertRaisesRegex(RuntimeError, "identity"):
                certify_app(wrong, "run", exe, app, source, sources, inventory)
            asset.write_text("substituted")
            with self.assertRaisesRegex(RuntimeError, "identity"):
                certify_app(report, "run", exe, app, source, sources, inventory)
            asset.write_text("original")
            certify_app(report, "run", exe, app, source, sources, inventory)


if __name__ == "__main__":
    unittest.main()
