"""Generate only the three approved fictional speech fixtures using local Windows SAPI."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
FILENAMES = {"explicit-support.mp3", "multi-bill-conditional.mp3", "legislator-role-change.mp3"}
RATE = 16000
GAP_FRAMES = RATE // 2


def main():
    if sys.platform != "win32":
        raise RuntimeError("Regeneration uses installed Windows David/Zira voices. The committed MP3s can be tested on other platforms.")
    import lameenc

    scripts = ROOT / "fixtures/audio/scripts.json"
    packet = json.loads(scripts.read_text(encoding="utf-8"))
    recordings = packet.get("recordings", [])
    if len(recordings) != 3 or {r.get("filename") for r in recordings} != FILENAMES:
        raise ValueError("Only the three approved synthetic speech recordings may be generated.")
    ids = set()
    for recording in recordings:
        if recording.get("synthetic") is not True:
            raise ValueError("Speech scripts must explicitly be synthetic.")
        for turn in recording["turns"]:
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,90}", turn["id"]) or turn["id"] in ids:
                raise ValueError("Speech turn IDs must be safe and unique.")
            ids.add(turn["id"])
    work = ROOT / ".scratch/speech-fixtures" / uuid.uuid4().hex[:12]
    work.mkdir(parents=True)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "scripts/synthesize_speech.ps1"),
         "-ScriptInput", str(scripts), "-OutputDirectory", str(work)],
        check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    voices = json.loads((work / "voices.json").read_text(encoding="utf-8-sig"))
    provenance = {
        "fixture_version": 1, "synthetic": True, "source": "local Windows SAPI",
        "ground_truth": "Authored scripts and measured generated clip lengths, not ASR/MacWhisper output.",
        "scripts_sha256": hashlib.sha256(scripts.read_bytes()).hexdigest(),
        "sample_rate": RATE, "channels": 1, "pcm_bits": 16, "mp3_kbps": 64,
        "inter_turn_silence_seconds": .5, "voices": voices,
        "manual_listening": "not performed by this automated generator",
        "macwhisper_transcription": "not run", "recordings": [],
    }
    total = 0
    for recording in recordings:
        pcm = bytearray(bytes(GAP_FRAMES * 2))
        offset = GAP_FRAMES
        segments = []
        for turn in recording["turns"]:
            path = work / (turn["id"] + ".wav")
            with wave.open(str(path), "rb") as clip:
                if (clip.getframerate(), clip.getnchannels(), clip.getsampwidth(), clip.getcomptype()) != (RATE, 1, 2, "NONE"):
                    raise ValueError(f"Unexpected generated PCM format: {turn['id']}")
                count = clip.getnframes()
                frames = clip.readframes(count)
            if count == 0 or not any(frames):
                raise ValueError(f"Empty/silent speech output: {turn['id']}")
            segments.append({"id": turn["id"], "speaker": turn["speaker"], "text": turn["text"],
                             "start": round(offset / RATE, 6), "end": round((offset + count) / RATE, 6)})
            pcm.extend(frames)
            pcm.extend(bytes(GAP_FRAMES * 2))
            offset += count + GAP_FRAMES
        duration = offset / RATE
        if not 45 <= duration <= 135:
            raise ValueError(f"Speech fixture duration outside short-test range: {recording['filename']} ({duration:.2f}s)")
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(64)
        encoder.set_in_sample_rate(RATE)
        encoder.set_channels(1)
        encoder.set_quality(2)
        media = encoder.encode(bytes(pcm)) + encoder.flush()
        if len(media) < 1000:
            raise ValueError("MP3 encoding failed; no placeholder is permitted.")
        (work / recording["filename"]).write_bytes(media)
        total += len(media)
        transcript = {
            "schema_version": 1, "synthetic": True,
            "source_format": "synthetic-scripted-sapi-ground-truth",
            "description": recording["ground_truth"],
            "hearing": {"id": recording["id"], "title": recording["title"], "date": "2026-10-06",
                        "committee": "Example Committee", "source_file": recording["filename"]},
            "segments": segments,
        }
        transcript_file = Path(recording["filename"]).stem + ".json"
        (work / transcript_file).write_text(json.dumps(transcript, indent=2) + "\n", encoding="utf-8", newline="\n")
        provenance["recordings"].append({
            "filename": recording["filename"], "bytes": len(media),
            "sha256": hashlib.sha256(media).hexdigest(), "pcm_duration_seconds": round(duration, 6),
            "transcript": transcript_file,
            "transcript_sha256": hashlib.sha256((work / transcript_file).read_bytes()).hexdigest(),
        })
    if total >= 3_000_000:
        raise ValueError("Combined synthetic MP3s must remain below 3 MB.")
    provenance["total_mp3_bytes"] = total
    (work / "manifest.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n")
    # Promote only a complete generated set, using individually named files.
    destination = ROOT / "fixtures/audio"
    for name in sorted(FILENAMES | {Path(n).stem + ".json" for n in FILENAMES} | {"manifest.json"}):
        temporary = destination / (name + ".tmp")
        temporary.write_bytes((work / name).read_bytes())
        os.replace(temporary, destination / name)
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: speech fixtures not generated: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
