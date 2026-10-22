"""Separate transcription providers and normalization of documented mw JSON."""

from __future__ import annotations

import copy
import json
import math
import signal
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from .pipeline import InputError, fingerprint, validate


class TranscriptionProvider(Protocol):
    def transcribe(self, source: Path, hearing: dict) -> dict: ...


class MockProvider:
    def transcribe(self, source: Path, hearing: dict | None = None) -> dict:
        document = json.loads(source.read_text(encoding="utf-8"))
        if hearing is not None:
            document["hearing"] = hearing
        return validate(document)


def normalize_macwhisper(export: dict, hearing: dict, synthetic: bool = True) -> dict:
    """MacWhisper official CLI JSON uses milliseconds, not Whisper's seconds."""
    if not isinstance(export, dict) or not isinstance(export.get("segments"), list):
        raise InputError("Choose a MacWhisper JSON export with a segments array.")
    if not isinstance(hearing, dict) or not isinstance(synthetic, bool):
        raise InputError("Provide hearing metadata and an explicit synthetic/public choice.")
    metadata = copy.deepcopy(hearing)
    if not metadata.get("id"):
        metadata["id"] = "mw-" + fingerprint({"hearing": hearing, "export": export, "synthetic": synthetic})[:16]
    segments = []
    for index, segment in enumerate(export["segments"]):
        if not isinstance(segment, dict):
            raise InputError(f"MacWhisper segment {index + 1} is not an object.")
        for key in ("start", "end"):
            value = segment.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise InputError(f"MacWhisper segment {index + 1}: {key} must be milliseconds as a finite number.")
        speaker = segment.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise InputError(f"MacWhisper segment {index + 1}: speaker must be text or absent.")
        text = segment.get("text")
        if text is None or isinstance(text, str) and not text.strip():
            # Whisper can emit blank segments; source_export still retains them.
            continue
        normalized = {
            "id": str(segment.get("id") or f"mw{index + 1:04}"),
            "start": segment["start"] / 1000,
            "end": segment["end"] / 1000,
            "text": text,
            "speaker": speaker.strip() if speaker and speaker.strip() else f"Unattributed turn {index + 1}",
            "speaker_unavailable": not bool(speaker and speaker.strip()),
        }
        if "words" in segment:
            normalized["source_word_timings_ms"] = copy.deepcopy(segment["words"])
        segments.append(normalized)
    document = {
        "schema_version": 1,
        "synthetic": synthetic,
        "source_scope": "synthetic" if synthetic else "public_hearing",
        "source_format": "macwhisper-cli-json-milliseconds",
        "hearing": metadata,
        "segments": segments,
        "source_export": copy.deepcopy(export),
    }
    return validate(document)


def parse_output(output: str, output_format: str, filename: str, hearing: dict) -> dict:
    """Normalize saved mw output, so a parsing failure never needs retranscription."""
    if output_format == "srt":
        from .subtitle_import import normalize_subtitles
        return normalize_subtitles(output, hearing, filename, synthetic=False)
    try:
        export = json.loads(output)
    except ValueError as error:
        raise InputError("MacWhisper did not return structured JSON. Verify mw transcribe --format json on the Mac.") from error
    return normalize_macwhisper(export, hearing, synthetic=False)


class MacWhisperProvider:
    def __init__(self, model_id: str, executable: str = "mw", timeout: int = 10800, cancel_event=None,
                 output_format: str = "json"):
        # Require explicit model selection; MacWhisper can also use hosted engines.
        if not model_id.startswith(("whisper-cpp:", "whisperkit:", "parakeet-pro:", "apple:")):
            raise InputError("Choose an explicitly supported local MacWhisper model id; inspect mw models list on the Mac.")
        self.model_id = model_id
        self.executable = executable
        self.timeout = timeout
        self.cancel_event = cancel_event
        if output_format not in ("json", "srt"):
            raise InputError("Choose SRT or MacWhisper JSON for automatic transcription.")
        self.output_format = output_format

    def _invoke(self, source: Path):
        if self.cancel_event is None:
            return subprocess.run(self.command(source), capture_output=True, text=True,
                                  encoding="utf-8", timeout=self.timeout, check=False)
        import time
        process = subprocess.Popen(self.command(source), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8")
        deadline = time.monotonic() + self.timeout
        while True:
            if self.cancel_event.is_set() or time.monotonic() > deadline:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise InputError("MacWhisper transcription was interrupted. Retry this hearing when ready.")
            try:
                stdout, stderr = process.communicate(timeout=.25)
                return subprocess.CompletedProcess(self.command(source), process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue

    def command(self, source: Path) -> list[str]:
        return [self.executable, "transcribe", str(source.resolve()),
                "--speakers" if self.output_format == "json" else "--no-speakers",
                "--format", self.output_format, "--model", self.model_id]

    def transcribe(self, source: Path, hearing: dict) -> dict:
        return parse_output(self.run(source), self.output_format, f"{source.stem}.{self.output_format}", hearing)

    def run(self, source: Path) -> str:
        """Raw mw output, before any normalization that could reject it."""
        if sys.platform != "darwin":
            raise InputError("MacWhisper transcription runs on macOS. Import its TXT, SRT, VTT or JSON export here.")
        if not source.is_file():
            raise InputError("The public hearing recording was not found.")
        try:
            result = self._invoke(source)
        except FileNotFoundError as error:
            raise InputError("mw was not found. Install the command-line tool from MacWhisper Settings > Advanced.") from error
        except subprocess.TimeoutExpired as error:
            raise InputError("MacWhisper transcription timed out. This hearing can be retried independently.") from error
        if result.returncode:
            raise InputError(f"MacWhisper failed: {result.stderr.strip()[:1000] or 'inspect mw transcribe --help on the Mac'}")
        return result.stdout
