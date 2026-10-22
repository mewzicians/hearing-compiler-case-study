"""Managed local recordings, background transcription and seekable media."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .pipeline import InputError, extract
from .providers import MacWhisperProvider, parse_output
from .store import set_stage

AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4"}
MAX_AUDIO_BYTES = 1024 * 1024 * 1024
LOCAL_MODEL = re.compile(r"(?:whisper-cpp|whisperkit|parakeet-pro|apple):[^\s]+")


def file_change_time(path: Path):
    """Windows stat ctime is creation time, not write/change identity."""
    if sys.platform != "win32":
        return path.stat().st_ctime_ns
    import ctypes
    import msvcrt
    from ctypes import wintypes
    class BasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int64) for name in
                    ("creation", "access", "write", "change")] + [("attributes", wintypes.DWORD)]
    get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    get_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    get_info.restype = wintypes.BOOL
    info = BasicInfo()
    with path.open("rb") as handle:
        if not get_info(msvcrt.get_osfhandle(handle.fileno()), 0, ctypes.byref(info), ctypes.sizeof(info)):
            return None  # Unsupported filesystem: hash each use rather than trust a weak stamp.
    return info.change


def receive_audio(directory: Path, stream, length: int, filename: str) -> dict:
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    extension = Path(filename).suffix.lower()
    if extension not in AUDIO_TYPES:
        raise InputError("Choose an MP3, WAV or M4A recording.")
    if not 0 < length <= MAX_AUDIO_BYTES:
        raise InputError("Choose a nonempty recording smaller than 1 GB.")
    destination = directory / "audio"
    destination.mkdir(parents=True, exist_ok=True)
    temporary = destination / f"upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    remaining = length
    header = b""
    try:
        with temporary.open("wb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise InputError("The upload was interrupted. Choose this file again.")
                if not header:
                    header = chunk[:16]
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
        valid = (
            extension == ".wav" and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
            or extension == ".mp3" and (header[:3] == b"ID3" or len(header) >= 2 and header[0] == 255 and header[1] & 224 == 224)
            or extension == ".m4a" and header[4:8] == b"ftyp"
        )
        if not valid:
            raise InputError("This file does not have the expected audio header. Choose the original recording.")
        sha256 = digest.hexdigest()
        target = destination / f"{sha256}{extension}"
        if target.exists():
            existing_digest = hashlib.sha256()
            with target.open("rb") as existing:
                for block in iter(lambda: existing.read(1024 * 1024), b""):
                    existing_digest.update(block)
            if existing_digest.hexdigest() == sha256:
                temporary.unlink()
            else:
                temporary.replace(target)
        else:
            temporary.replace(target)
        return {"path": f"audio/{target.name}", "sha256": sha256, "bytes": length,
                "content_type": AUDIO_TYPES[extension], "filename": filename}
    finally:
        if temporary.exists():
            temporary.unlink()


def audio_path(store, job_id: str) -> tuple[Path, dict]:
    asset = store.media_asset(job_id)
    if not asset:
        raise InputError("No recording is attached to this hearing.")
    path = (store.directory / asset["path"]).resolve()
    if not path.is_relative_to((store.directory / "audio").resolve()) or not path.is_file():
        raise InputError("The saved recording is missing. Restore the workspace's audio directory.")
    if path.stat().st_size != asset["bytes"]:
        raise InputError("The saved recording has changed. Restore its original file.")
    # Reuse verification for unchanged range requests, but invalidate it on any
    # identity/metadata change. The expected digest is part of the cache key.
    def stamp():
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, file_change_time(path))
    before = stamp()
    key = (str(path), asset["sha256"])
    with store.lock:
        verified = before[-1] is not None and store.verified_media.get(key) == before
    if not verified:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != asset["sha256"] or stamp() != before:
            raise InputError("The saved recording differs from its original SHA256. Reattach or restore the exact original file.")
        with store.lock:
            store.verified_media[key] = before
    return path, asset


def byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()):
        raise InputError("Unsupported media range.")
    left, right = match.groups()
    if left:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
    else:
        suffix = int(right)
        if suffix <= 0:
            raise InputError("Invalid media range.")
        start, end = max(0, size - suffix), size - 1
    if start >= size or start > end:
        raise InputError("Media range is outside the recording.")
    return start, end, True


class KeepAwake:
    """While hearings are processing, macOS's caffeinate keeps the Mac from going to sleep when idle: a 4-hour
    hearing transcribes for about an hour. It can't keep a Mac with its lid closed awake, and it ends with the
    app (-w), so it never outlives it. Elsewhere it does nothing."""

    def __init__(self, popen=subprocess.Popen):
        self.popen, self.process = popen, None
        fallback = "/usr/bin/caffeinate" if Path("/usr/bin/caffeinate").is_file() else None
        self.tool = (shutil.which("caffeinate") or fallback) if sys.platform == "darwin" else None

    def hold(self, busy: bool):
        if busy and self.process is None and self.tool:
            try:
                self.process = self.popen([self.tool, "-i", "-w", str(os.getpid())],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as error:
                logging.warning("Could not keep the Mac awake while processing: %s", error)
        elif not busy and self.process is not None:
            process, self.process = self.process, None
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                logging.warning("caffeinate didn't stop; it ends with the app")


class AudioQueue:
    def __init__(self, store, provider=None, stop_event=None):
        self.store = store
        self.provider = provider
        self.stop_event = stop_event or threading.Event()
        self.awake = KeepAwake()
        self.wake = threading.Event()
        self.worker = threading.Thread(target=self._run, name="hearing-audio-worker", daemon=True)
        self.worker.start()

    @staticmethod
    def executable() -> str | None:
        if sys.platform != "darwin":
            return None
        located = shutil.which("mw")
        if located:
            return located
        return "/usr/local/bin/mw" if Path("/usr/local/bin/mw").is_file() else None

    def available(self) -> bool:
        return bool(self.provider or self.executable() and self.store.settings().get("model_id"))

    def info(self) -> dict:
        settings = self.store.settings()
        return {"platform": "macOS" if sys.platform == "darwin" else "Windows" if sys.platform == "win32" else sys.platform,
                "installed": bool(self.executable()), "ready": self.available(),
                "model_id": settings.get("model_id", ""),
                "output_format": settings.get("output_format", "json" if settings.get("model_id") else "srt")}

    def models(self) -> dict:
        executable = self.executable()
        if not executable:
            raise InputError("MacWhisper's mw tool is available on macOS. Import a MacWhisper JSON transcript on this computer.")
        try:
            version = subprocess.run([executable, "version"], capture_output=True, text=True, timeout=15)
            listed = subprocess.run([executable, "models", "list"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InputError("Cannot reach MacWhisper. Open it, check Settings > Advanced > Command-Line Tool, then try again.") from error
        if version.returncode or listed.returncode:
            raise InputError("MacWhisper could not list its models. Open the app and inspect mw models list on the Mac.")
        return {"version": version.stdout.strip(), "models": list(dict.fromkeys(LOCAL_MODEL.findall(listed.stdout)))}

    def configure(self, model_id: str, output_format: str = "json"):
        if output_format not in ("json", "srt"):
            raise InputError("Choose SRT or MacWhisper JSON.")
        models = self.models()
        if model_id not in models["models"]:
            raise InputError("Choose a local model installed in MacWhisper.")
        self.store.set_model(model_id, output_format)
        self.wake.set()

    def retry(self, job_id: str):
        result = self.store.retry_audio(job_id, self.available())
        self.wake.set()
        return result

    def close(self):
        self.stop_event.set()
        self.wake.set()
        self.worker.join(timeout=7)
        self.awake.hold(False)
        if self.worker.is_alive():
            raise InputError("Audio processing is still stopping. The saved workspace remains locked until the process exits.")

    def _transcribe(self, job: dict) -> dict:
        if self.provider is not None and not hasattr(self.provider, "run"):
            path, _ = audio_path(self.store, job["id"])
            return self.provider.transcribe(path, job["hearing"])
        settings = self.store.settings()
        output_format = settings.get("output_format", "json")
        saved = self.store.saved_transcription_output(job["id"], output_format, settings["model_id"])
        if saved is None:
            path, _ = audio_path(self.store, job["id"])
            provider = self.provider or MacWhisperProvider(
                settings["model_id"], executable=self.executable(),
                cancel_event=self.stop_event, output_format=output_format)
            saved = provider.run(path), f"{path.stem}.{output_format}"
            self.store.save_transcription_output(job["id"], *saved, output_format, settings["model_id"])
        output, filename = saved
        try:
            return parse_output(output, output_format, filename, job["hearing"])
        except InputError as error:
            raise InputError(f"MacWhisper finished, but its saved output could not be read: {error} "
                             "Retry rereads the saved output without transcribing again. Attach a corrected "
                             "transcript, or choose another MacWhisper model to transcribe again.") from error

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.store.flush_failures()
                job = self.store.claim_audio(self.available())
            except Exception:
                logging.exception("Cannot save queue progress; waiting for workspace storage to recover")
                self.wake.wait(1)
                self.wake.clear()
                continue
            if job is None:
                self.awake.hold(False)  # Nothing to do: the Mac may sleep as usual.
                self.wake.wait(.5)
                self.wake.clear()
                continue
            self.awake.hold(True)
            stage = "transcription" if job.get("input") is None else "extraction"
            try:
                if job.get("input") is None:
                    document = self._transcribe(job)
                    self.store.cache_transcript(job["id"], document, from_worker=True)
                    if job.get("hearing_url"):  # As in the saved input, so the first draft uses the agenda.
                        document["hearing_url"] = job["hearing_url"]
                else:
                    document = job["input"]
                if self.stop_event.is_set():
                    raise InputError("Processing was interrupted. Retry this hearing when the app reopens.")
                stage = "extraction"
                # Persist the stage before extraction without holding a lock during work.
                with self.store.lock:
                    set_stage(self.store.data["jobs"][job["id"]], "Analyzing")
                    self.store._save()
                asset = self.store.media_asset(job["id"])
                if asset:  # For the audio check, which listens to short clips of it.
                    document = {**document, "recording_path": str(self.store.directory / asset["path"])}
                draft = (self.store.extractor or extract)(document)
                self.store.finish_audio(job["id"], draft)
            except Exception as error:
                message = str(error) if isinstance(error, (InputError, OSError)) else "Processing failed. Retry this hearing; inspect the server log if it repeats."
                try:
                    self.store.fail_audio(job["id"], stage, message)
                except Exception as storage_error:
                    self.store.defer_failure(job["id"], stage, message, storage_error)
                    logging.exception("Could not save failure for hearing %s", job["id"])
                if not isinstance(error, InputError):
                    logging.exception("Audio processing failed for hearing %s", job["id"])
