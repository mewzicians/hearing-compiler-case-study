"""Generated tones for media tests only; no bundled demonstration recordings."""

import io
import json
import math
import struct
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def wav_bytes(seconds=30, frequency=440):
    output = io.BytesIO()
    rate = 8000
    frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * frequency * i / rate)))
                      for i in range(rate * seconds))
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return output.getvalue()


def mp3_bytes(seconds=30):
    """Optional browser audit dependency, never imported by the app."""
    import lameenc
    raw = wav_bytes(seconds)
    with wave.open(io.BytesIO(raw), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(32)
    encoder.set_in_sample_rate(8000)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return encoder.encode(frames) + encoder.flush()


def export():
    document = json.loads((ROOT / "fixtures/macwhisper/sample-export.json").read_text(encoding="utf-8"))
    for index, segment in enumerate(document["segments"]):
        segment["start"] = (3 + index * 6) * 1000
        segment["end"] = (5 + index * 6) * 1000
        segment.pop("words", None)
    return document


def metadata(name="hearing.wav"):
    return {"title": "Recorded hearing", "date": "2025-04-22", "committee": "Example Committee",
            "source_file": name, "source_scope": "public_hearing"}
