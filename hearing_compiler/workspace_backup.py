"""Verified full saved-state/audio backup and empty-workspace restoration."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import stat
import uuid
from datetime import datetime, timezone
import zipfile

from .pipeline import InputError, prepare_review, validate
from .model_contract.sources import strict_json, SourceError
from .saved_state import validate_saved_state

KIND = "hearing-workspace-backup/v1"
MAX_BACKUP_BYTES = 128 * 1024 * 1024 * 1024


def _inside(root, path):
    root = root.resolve()
    logical = Path(os.path.abspath(path))
    if logical == root or not logical.is_relative_to(root):
        raise InputError("Backup paths must stay inside the selected workspace.")
    for parent in (logical, *logical.parents):
        if parent == root:
            break
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise InputError("Workspace backup does not follow links or reparse points.")
    path = logical.resolve()
    if path == root or not path.is_relative_to(root):
        raise InputError("Backup paths must stay inside the selected workspace.")
    return path


def _plain_file(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
            or not path.is_file()):
        raise InputError("Workspace backup does not follow links or reparse points.")


def create_backup(store, *, on_created=None):
    """A consistent snapshot; writes can resume after state/media are captured."""
    destination = _inside(store.directory, store.directory / f".backup-{uuid.uuid4().hex}.zip")
    if on_created is not None:
        on_created(destination)
    manifest = {"kind": KIND, "created_utc": datetime.now(timezone.utc).isoformat(),
                "state_schema": 1, "recording_included": True, "files": {}}
    try:
        with store.lock:
            # Snapshot under the lock; the slow copy below runs without blocking the app.
            state = json.dumps(store.data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            paths = set()
            for job in store.data["jobs"].values():
                if job.get("audio"):
                    relative = job["audio"]["path"]
                    path = _inside(store.directory, store.directory / relative)
                    _plain_file(path)
                    paths.add((relative, path, job["audio"]["sha256"]))
            # Set-aside hearings are unreadable, but their recordings are still original media.
            for entry in store.data.get("set_aside", []):
                job = entry.get("job") if isinstance(entry, dict) else None
                asset = job.get("audio") if isinstance(job, dict) else None
                relative = asset.get("path") if isinstance(asset, dict) else None
                if isinstance(relative, str) and re.fullmatch(r"audio/[0-9a-f]{64}\.(?:mp3|wav|m4a)", relative):
                    path = _inside(store.directory, store.directory / relative)
                    if path.is_file():
                        _plain_file(path)
                        paths.add((relative, path, relative[6:70]))
            # Saved MacWhisper output and Gemini replies spare a long or paid redo after restore.
            for folder, pattern in (("transcription-output", "*.json"), ("transcription-output", "*.srt"),
                                    ("extraction-output", "*.json")):
                cache = store.directory / folder
                if cache.is_dir() and not cache.is_symlink():
                    for path in cache.glob(pattern):
                        if _allowed_member(path.relative_to(store.directory).as_posix()):
                            _plain_file(path)
                            paths.add((path.relative_to(store.directory).as_posix(), path, None))
            # Legacy migration backups are saved-state evidence, not another
            # recursive workspace archive. Preserve their exact bytes.
            backups = store.directory / "backups"
            if backups.exists():
                if backups.is_symlink() or getattr(backups.lstat(), "st_file_attributes", 0) & 1024:
                    raise InputError("Do not back up a linked migration-backup directory.")
                for pattern in ("before-sample-removal-*.json", "before-set-aside-*.json"):
                    for path in backups.glob(pattern):
                        _plain_file(path)
                        paths.add((path.relative_to(store.directory).as_posix(), path, None))
        # Recordings are content-addressed and kept while referenced, so they can be read unlocked;
        # each is still hash- and stat-checked. Audio is already compressed, so it is stored as is.
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr("state.json", state)
            manifest["files"]["state.json"] = {"bytes": len(state), "sha256": hashlib.sha256(state).hexdigest()}
            for relative, path, expected in sorted(paths):
                digest, size = hashlib.sha256(), 0
                before = path.stat()
                member = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                member.compress_type = zipfile.ZIP_STORED if relative.startswith("audio/") else zipfile.ZIP_DEFLATED
                with path.open("rb") as source, archive.open(member, "w", force_zip64=True) as target:
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                        size += len(block)
                        target.write(block)
                after = path.stat()
                if (expected and digest.hexdigest() != expected
                        or (before.st_size, before.st_mtime_ns, before.st_ino) !=
                           (after.st_size, after.st_mtime_ns, after.st_ino)):
                    raise InputError("A recording changed during backup. Restore its original bytes and retry.")
                manifest["files"][relative] = {"bytes": size, "sha256": digest.hexdigest()}
            archive.writestr("manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
        return destination
    except Exception:
        if destination.exists():
            try:
                destination.unlink()
            except OSError:
                logging.exception("Backup creation cleanup failed after its primary error")
        raise


def _member_path(name):
    if (not isinstance(name, str) or "\\" in name or ":" in name or name.startswith("/")
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise InputError("The backup contains an unsafe member path.")
    path = PurePosixPath(name)
    if str(path) != name:
        raise InputError("The backup member name is not canonical.")
    return path


def _allowed_member(name):
    return (name in {"state.json", "manifest.json"}
            or re.fullmatch(r"audio/[0-9a-f]{64}\.(?:mp3|wav|m4a)", name)
            or re.fullmatch(r"backups/before-(?:sample-removal|set-aside)-[a-zA-Z0-9_-]+\.json", name)
            or re.fullmatch(r"transcription-output/[0-9a-f]{64}\.(?:json|srt)", name)
            or re.fullmatch(r"extraction-output/[0-9a-f]{64}\.json", name))


def _restore_target(root, name):
    logical = root / name
    for parent in (logical, *logical.parents):
        if parent == root:
            break
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if parent.is_symlink() or getattr(info, "st_file_attributes", 0) & 1024:
                raise InputError("Restore cannot follow a linked destination.")
    return _inside(root, logical)


def verify_backup(archive_path):
    """Checks exact member inventory, decoded hashes, source and recording refs."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)) or len(names) > 100000:
                raise InputError("The backup has duplicate or excessive members.")
            if sum(member.file_size for member in members) > MAX_BACKUP_BYTES:
                raise InputError("The backup exceeds the supported workspace size.")
            for member in members:
                _member_path(member.filename)
                if not _allowed_member(member.filename) or member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                    raise InputError("The backup contains a directory/link member.")
            if "manifest.json" not in names or "state.json" not in names:
                raise InputError("Choose a full workspace backup, not a note JSON archive.")
            if archive.getinfo("manifest.json").file_size > 16 * 1024 * 1024:
                raise InputError("Backup manifest is too large.")
            manifest = strict_json(archive.read("manifest.json"))
            if (not isinstance(manifest, dict) or manifest.get("kind") != KIND
                    or manifest.get("recording_included") is not True
                    or not isinstance(manifest.get("files"), dict)
                    or set(names) != set(manifest["files"]) | {"manifest.json"}):
                raise InputError("The backup manifest/member inventory differs.")
            for name, expected in manifest["files"].items():
                if (not isinstance(expected, dict) or set(expected) != {"bytes", "sha256"}
                        or not isinstance(expected["bytes"], int) or isinstance(expected["bytes"], bool)
                        or expected["bytes"] != archive.getinfo(name).file_size):
                    raise InputError("Backup member sizes differ from the manifest.")
                digest = hashlib.sha256()
                with archive.open(name) as source:
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                if digest.hexdigest() != expected["sha256"]:
                    raise InputError("Backup content does not match its recorded SHA256.")
            if archive.getinfo("state.json").file_size > 512 * 1024 * 1024:
                raise InputError("Backup state is too large for this prototype.")
            state = validate_saved_state(strict_json(archive.read("state.json")))
            if (not isinstance(state, dict) or state.get("schema_version") != 1
                    or not isinstance(state.get("jobs"), dict)):
                raise InputError("The backup has an unsupported saved-state format.")
            for job_id, job in state["jobs"].items():
                if (not isinstance(job, dict) or job.get("id") != job_id
                        or not isinstance(job.get("revision"), int) or isinstance(job.get("revision"), bool)
                        or job.get("processing") not in {"Complete", "Failed", "Waiting", "Awaiting transcript", "Analyzing", "Transcribing"}):
                    raise InputError("A saved hearing is malformed.")
                if job.get("audio"):
                    asset = job["audio"]
                    name = asset.get("path")
                    _member_path(name)
                    if not name.startswith("audio/") or name not in manifest["files"]:
                        raise InputError("A source recording is missing from the backup.")
                    expected = manifest["files"][name]
                    if asset.get("sha256") != expected["sha256"] or asset.get("bytes") != expected["bytes"]:
                        raise InputError("Recording identity differs from saved hearing metadata.")
                if job["processing"] == "Complete":
                    original = validate(job["input"])
                    draft = prepare_review(job["draft"])
                    if draft["segments"] != original["segments"]:
                        raise InputError("A saved draft changed its original transcript.")
            return manifest, state
    except (zipfile.BadZipFile, KeyError, SourceError, UnicodeError, TypeError, ValueError) as error:
        if isinstance(error, InputError):
            raise
        raise InputError(f"Cannot verify this workspace backup: {type(error).__name__}.") from error


def restore_backup(store, archive_path):
    """Restoration never replaces saved hearings. Use a fresh empty workspace."""
    manifest, restored = verify_backup(archive_path)
    staging = _inside(store.directory, store.directory / f".restore-{uuid.uuid4().hex}")
    staging.mkdir()
    failed = False
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if (set(archive.namelist()) != set(manifest["files"]) | {"manifest.json"}
                    or len(archive.namelist()) != len(set(archive.namelist()))
                    or strict_json(archive.read("manifest.json")) != manifest):
                raise InputError("The backup archive changed after verification.")
            for name in manifest["files"]:
                target = _inside(staging, staging / name)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open(name) as source, target.open("xb") as output:
                    while block := source.read(1024 * 1024):
                        size += len(block)
                        if size > manifest["files"][name]["bytes"]:
                            raise InputError("The backup archive changed after verification.")
                        digest.update(block)
                        output.write(block)
                if digest.hexdigest() != manifest["files"][name]["sha256"] or size != manifest["files"][name]["bytes"]:
                    raise InputError("The backup archive changed after verification.")
            if strict_json((staging / "state.json").read_bytes()) != restored:
                raise InputError("The backup state changed after verification.")
        with store.lock:
            if store.data["jobs"] or store.data.get("set_aside") or store.pending_failures:
                raise InputError("Restore into a new empty workspace. Existing hearings and corrections are preserved.")
            previous = store.data
            for name in manifest["files"]:
                if name == "state.json":
                    continue
                target = _restore_target(store.directory, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    _plain_file(target)
                    with target.open("rb") as source:
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
                    if digest != manifest["files"][name]["sha256"]:
                        raise InputError("A restore target already contains different data. Use another empty workspace.")
                    continue
                os.replace(staging / name, target)
            restored = json.loads(json.dumps(restored))
            for job in restored["jobs"].values():
                if job["processing"] in {"Analyzing", "Transcribing"}:
                    job["failed_stage"] = "transcription" if job["processing"] == "Transcribing" else "extraction"
                    job["processing"] = "Failed"
                    job["error"] = "Processing was interrupted at the backup snapshot. Retry this hearing."
            store.data = restored
            try:
                store._save()
            except Exception:
                store.data = previous
                raise
            store.verified_media.clear()
            return {"restored": len(restored["jobs"]), "recording_included": True}
    except Exception:
        failed = True
        raise
    finally:
        # This exclusive staging directory contains only verified regular files
        # generated here. Resolve it again before recursive cleanup.
        try:
            target = _inside(store.directory, staging)
            if target.exists():
                for path in target.rglob("*"):
                    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 1024:
                        raise InputError("Restore staging changed into a link; cleanup was withheld.")
                shutil.rmtree(target)
        except Exception:
            if not failed:
                raise
            logging.exception("Restore staging cleanup failed after the primary restore error")
