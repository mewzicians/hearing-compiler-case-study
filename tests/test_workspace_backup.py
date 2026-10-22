from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from hearing_compiler.audio import audio_path, receive_audio
from hearing_compiler.pipeline import InputError
from hearing_compiler.store import Store
from hearing_compiler.workspace_backup import create_backup, restore_backup, verify_backup
from audio_test_helpers import wav_bytes
from test_pipeline import fixture


class WorkspaceBackupTests(unittest.TestCase):
    def populate(self, root):
        store = Store(root)
        doc = fixture("sample-hearing")
        job_id = store.import_one(doc)["id"]
        store.review(job_id, 0, [{"path": "/testimonies/0/name", "value": "Saved backup correction", "review": "NEEDS_REVIEW"}])
        audio = wav_bytes(2)
        asset = receive_audio(root, io.BytesIO(audio), len(audio), "fictional.wav")
        store.attach_audio(job_id, asset)
        return store, job_id, audio

    def test_full_round_trip_restores_source_recording_corrected_values_and_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, job_id, audio = self.populate(root / "source")
            archive = create_backup(source)
            manifest, state = verify_backup(archive)
            self.assertTrue(manifest["recording_included"])
            self.assertEqual(state, source.data)
            restored = Store(root / "restored")
            result = restore_backup(restored, archive)
            self.assertEqual(result["restored"], 1)
            saved = Store(root / "restored").get(job_id)
            self.assertEqual(saved["draft"], source.get(job_id)["draft"])
            self.assertEqual(saved["input"], source.get(job_id)["input"])
            self.assertEqual(audio_path(restored, job_id)[0].read_bytes(), audio)
            self.assertIn("Saved backup correction", saved["markdown"])

    def test_restore_refuses_existing_reviews_without_modifying_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, job_id, _ = self.populate(root / "source")
            archive = create_backup(source)
            before = copy.deepcopy(source.data)
            with self.assertRaisesRegex(InputError, "empty workspace"):
                restore_backup(source, archive)
            self.assertEqual(source.data, before)
            self.assertEqual(Store(source.directory).data, before)

    def test_corrupt_audio_hash_unsafe_members_or_incomplete_archives_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, _, _ = self.populate(root / "source")
            archive = create_backup(source)
            with zipfile.ZipFile(archive) as original:
                contents = {name: original.read(name) for name in original.namelist()}
            variants = {
                "corrupt": {**contents, next(name for name in contents if name.startswith("audio/")): b"wrong"},
                "path": {**contents, "../outside.txt": b"outside"},
                "missing": {name: data for name, data in contents.items() if not name.startswith("audio/")},
                "note-json": {"state.json": b'{"kind":"hearing-review-archive"}'},
            }
            for name, files in variants.items():
                target = root / (name + ".zip")
                with zipfile.ZipFile(target, "w") as output:
                    for member, data in files.items():
                        output.writestr(member, data)
                with self.subTest(name=name), self.assertRaises(InputError):
                    verify_backup(target)
                self.assertFalse((root / "outside.txt").exists())

    def test_changed_managed_recording_cannot_be_backed_up_as_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, job_id, audio = self.populate(root)
            target = root / source.media_asset(job_id)["path"]
            changed = bytearray(audio)
            changed[-1] ^= 1
            target.write_bytes(changed)
            with self.assertRaisesRegex(InputError, "recording changed"):
                create_backup(source)
            self.assertEqual(list(root.glob(".backup-*.zip")), [])

    def test_failed_state_commit_preserves_empty_destination_and_all_source_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, _, _ = self.populate(root / "source")
            archive = create_backup(source)
            target = Store(root / "target")
            before = copy.deepcopy(target.data)
            with patch.object(target, "_save", side_effect=PermissionError("injected")):
                with self.assertRaises(PermissionError):
                    restore_backup(target, archive)
            self.assertEqual(target.data, before)
            self.assertEqual(source.data["jobs"].keys(), verify_backup(archive)[1]["jobs"].keys())
            # Previously promoted verified audio can be reused on a safe retry.
            self.assertEqual(restore_backup(target, archive)["restored"], 1)

    def test_interrupted_stage_snapshot_retains_cached_source_and_retry_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, job_id, _ = self.populate(root / "source")
            source.data["jobs"][job_id]["processing"] = "Analyzing"
            source._save()
            archive = create_backup(source)
            target = Store(root / "target")
            restore_backup(target, archive)
            saved = target.get(job_id)
            self.assertEqual(saved["processing"], "Failed")
            self.assertEqual(saved["failed_stage"], "extraction")
            self.assertEqual(saved["input"], source.get(job_id)["input"])

    def rewrite(self, archive, change):
        with zipfile.ZipFile(archive) as original:
            contents = {name: original.read(name) for name in original.namelist()}
        change(contents)
        with zipfile.ZipFile(archive, "w") as output:
            for member, data in contents.items():
                output.writestr(member, data)

    def test_archive_changes_after_verification_never_promote_unverified_bytes(self):
        for replace_manifest in (False, True):
            with self.subTest(replace_manifest=replace_manifest), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source, _, _ = self.populate(root / "source")
                archive = create_backup(source)
                target = Store(root / "target")
                before = copy.deepcopy(target.data)
                verify = verify_backup
                def changed_after_verification(path):
                    verified = verify(path)
                    def mutate(contents):
                        name = next(n for n in contents if n.startswith("audio/"))
                        changed = bytearray(contents[name])
                        changed[-1] ^= 1
                        contents[name] = bytes(changed)
                        if replace_manifest:
                            manifest = json.loads(contents["manifest.json"])
                            manifest["files"][name]["sha256"] = hashlib.sha256(contents[name]).hexdigest()
                            contents["manifest.json"] = json.dumps(manifest).encode()
                    self.rewrite(path, mutate)
                    return verified
                with patch("hearing_compiler.workspace_backup.verify_backup", side_effect=changed_after_verification):
                    with self.assertRaisesRegex(InputError, "changed after verification"):
                        restore_backup(target, archive)
                self.assertEqual(target.data, before)
                self.assertFalse(target.path.exists())
                self.assertFalse((target.directory / "audio").exists())
                self.assertEqual(list(target.directory.glob(".restore-*")), [])

    def test_hash_valid_malformed_jobs_rejected_by_restore_and_set_aside_on_load(self):
        mutations = {
            "missing-hearing": lambda j: j.pop("hearing"),
            "wrong-hearing": lambda j: j.update(hearing={"id": "wrong"}),
            "fingerprint": lambda j: j.update(fingerprint=True),
            "input": lambda j: j.pop("input"),
            "bad-review": lambda j: j["draft"]["testimonies"][0]["name"].update(review="CERTIFIED"),
            "bad-audio": lambda j: j["audio"].update(bytes=True),
            "bad-revision": lambda j: j.update(revision=True),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source, job_id, _ = self.populate(root / "source")
                archive = create_backup(source)
                def malformed(contents):
                    state = json.loads(contents["state.json"])
                    mutation(state["jobs"][job_id])
                    contents["state.json"] = json.dumps(state).encode()
                    manifest = json.loads(contents["manifest.json"])
                    manifest["files"]["state.json"] = {"bytes": len(contents["state.json"]),
                                                        "sha256": hashlib.sha256(contents["state.json"]).hexdigest()}
                    contents["manifest.json"] = json.dumps(manifest).encode()
                self.rewrite(archive, malformed)
                target = Store(root / "target")
                before = copy.deepcopy(target.data)
                with self.assertRaises(InputError):
                    restore_backup(target, archive)
                self.assertEqual(target.data, before)
                self.assertFalse(target.path.exists())
                with zipfile.ZipFile(archive) as contents:
                    bad_state = contents.read("state.json")
                target.path.write_bytes(bad_state)
                reopened = Store(target.directory)
                self.assertEqual(reopened.list(), [])
                self.assertEqual(reopened.data["set_aside"][0]["job"], json.loads(bad_state)["jobs"][job_id])
                copies = list((target.directory / "backups").glob("before-set-aside-*.json"))
                self.assertEqual([path.read_bytes() for path in copies], [bad_state])

    def test_set_aside_hearing_keeps_its_recording_through_backup_and_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, job_id, audio = self.populate(root / "source")
            other = source.import_one(fixture("no-questions"))["id"]
            state = json.loads(source.path.read_text(encoding="utf-8"))
            state["jobs"][job_id]["hearing"]["title"] = "Edited outside the app"
            source.path.write_text(json.dumps(state), encoding="utf-8")
            reopened = Store(root / "source")
            self.assertEqual([item["id"] for item in reopened.list()], [other])
            restored = Store(root / "restored")
            restore_backup(restored, create_backup(reopened))
            self.assertEqual(restored.data["set_aside"], reopened.data["set_aside"])
            asset = reopened.data["set_aside"][0]["job"]["audio"]
            self.assertEqual((restored.directory / asset["path"]).read_bytes(), audio)
            self.assertEqual(len(list((restored.directory / "backups").glob("before-set-aside-*.json"))), 1)
            # A workspace holding only set-aside work is not empty.
            lone = root / "lone"
            lone.mkdir()
            (lone / "state.json").write_text(json.dumps(
                {"schema_version": 1, "jobs": {job_id: reopened.data["set_aside"][0]["job"]}}), encoding="utf-8")
            lone_store = Store(lone)
            self.assertEqual(lone_store.list(), [])
            with self.assertRaisesRegex(InputError, "empty workspace"):
                restore_backup(lone_store, create_backup(reopened))
            self.assertEqual(len(lone_store.data["set_aside"]), 1)

    @unittest.skipUnless(os.name == "nt", "Actual Windows junction regression")
    def test_backup_refuses_internal_recording_junction_before_resolving_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, _, _ = self.populate(root)
            original, actual = root / "audio", root / "actual-audio"
            os.replace(original, actual)
            environment = {**os.environ, "HEARING_AUDIT_LINK": str(original), "HEARING_AUDIT_TARGET": str(actual)}
            completed = subprocess.run(["powershell", "-NoProfile", "-Command",
                                        "New-Item -ItemType Junction -Path $env:HEARING_AUDIT_LINK -Target $env:HEARING_AUDIT_TARGET | Out-Null"],
                                       env=environment, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            try:
                with self.assertRaisesRegex(InputError, "links or reparse"):
                    create_backup(source)
                self.assertEqual(list(root.glob(".backup-*.zip")), [])
            finally:
                # Remove only this known junction, never recurse through it.
                os.rmdir(original)
                os.replace(actual, original)

    def test_malformed_rich_units_rejected_but_failed_source_input_is_retained(self):
        from hearing_compiler.rich_review import from_packets
        from test_rich_review import joint_packet
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            doc, packet = joint_packet()
            source = Store(root / "source", extractor=lambda d: from_packets(d, [packet], {}))
            job_id = source.import_one(doc)["id"]
            source.data["jobs"][job_id]["draft"]["units"][0]["testifiers"] = "invalid"
            source._save()
            archive = create_backup(source)
            with self.assertRaises(InputError):
                verify_backup(archive)
            failed = Store(root / "failed")
            failed.import_one(fixture("broken-order"))
            target = Store(root / "target")
            restore_backup(target, create_backup(failed))
            self.assertEqual(target.get("broken-order")["input"], failed.get("broken-order")["input"])
