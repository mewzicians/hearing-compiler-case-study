"""Short clips of a hearing recording, cut in plain Python for the audio check.

MP3 is cut at frame boundaries (each MPEG audio frame has its own header), and WAV with the standard wave
module. M4A (QuickTime's "Export As → Audio Only") is first turned into a temporary 16 kHz mono WAV with macOS's
own afconvert; elsewhere, or for other formats, nothing is cut and those items aren't audio-checked.
"""
from __future__ import annotations

from array import array
import bisect
import io
import mmap
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import wave

# Layer III bit rates (kbit/s) for MPEG-1, and for MPEG-2 and 2.5.
BITRATES = {1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
            2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)}
SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
MAX_SECONDS = 40.0
MAX_JUNK = 1 << 20  # A megabyte without a frame in a row: not MP3 audio after all.
_cache: dict = {}
_lock = threading.Lock()


def _id3_size(data) -> int:
    if bytes(data[:3]) != b"ID3" or len(data) < 10:
        return 0
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    return 10 + size + (10 if data[5] & 0x10 else 0)


def _frame(data, position):
    """(length, seconds) of a Layer III frame header at position, or None."""
    if position + 4 > len(data):
        return None
    header = int.from_bytes(data[position:position + 4], "big")
    version, layer = (header >> 19) & 3, (header >> 17) & 3
    rate, sampling, padding = (header >> 12) & 15, (header >> 10) & 3, (header >> 9) & 1
    if (header >> 21) != 0x7FF or version == 1 or layer != 1 or sampling == 3:
        return None
    bitrate = BITRATES[1 if version == 3 else 2][rate] * 1000
    if not bitrate:  # Free-format (0) and the forbidden value (15) aren't cut.
        return None
    sample_rate = SAMPLE_RATES[version][sampling]
    length = (144 if version == 3 else 72) * bitrate // sample_rate + padding
    return (length, (1152 if version == 3 else 576) / sample_rate) if length >= 4 else None


def mp3_frames(data) -> tuple[array, array, array]:
    """(offsets, lengths, start seconds) of each Layer III frame. A header counts when it starts where the last
    frame ended, or when another header (or the end) follows it, so a false sync inside a tag or picture isn't
    taken for audio."""
    offsets, lengths, starts = array("q"), array("q"), array("d")
    position, at, junk, expected = _id3_size(data), 0.0, 0, None
    while position + 4 <= len(data) and junk <= MAX_JUNK:
        frame = _frame(data, position)
        following = position + frame[0] if frame else None
        if frame and (position == expected or following >= len(data) - 4 or _frame(data, following)):
            offsets.append(position)
            lengths.append(frame[0])
            starts.append(at)
            at, position, junk, expected = at + frame[1], following, 0, following
        else:
            position, junk = position + 1, junk + 1
    return offsets, lengths, starts


def _mp3(path: Path, start: float, end: float) -> bytes | None:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _lock:  # Each recording is indexed once; a clip then reads only its own bytes.
        index = _cache.get(key)
        if index is None:
            with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
                index = _cache[key] = mp3_frames(data)
    offsets, lengths, starts = index
    first, last = bisect.bisect_left(starts, start), bisect.bisect_left(starts, end)
    if first >= last:
        return None
    with path.open("rb") as source:
        source.seek(offsets[first])
        return source.read(offsets[last - 1] + lengths[last - 1] - offsets[first])


def _as_wav(path: Path) -> Path | None:
    """A temporary 16 kHz mono WAV of an M4A, made once per recording by macOS's afconvert."""
    tool = shutil.which("afconvert") or ("/usr/bin/afconvert" if Path("/usr/bin/afconvert").is_file() else None)
    if tool is None:
        return None
    stat = path.stat()
    key = ("wav", str(path), stat.st_size, stat.st_mtime_ns)
    with _lock:
        if key not in _cache:
            target = Path(tempfile.mkdtemp(prefix="hearing-clips-")) / "recording.wav"
            try:
                subprocess.run([tool, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(path), str(target)],
                               check=True, capture_output=True, timeout=1800)
            except (OSError, subprocess.SubprocessError):
                shutil.rmtree(target.parent, ignore_errors=True)
                _cache[key] = None
            else:
                _cache[key] = target
        return _cache[key]


def forget():
    """Drops the frame indexes and temporary WAVs once a hearing's clips are cut."""
    with _lock:
        for value in _cache.values():
            if isinstance(value, Path):
                shutil.rmtree(value.parent, ignore_errors=True)
        _cache.clear()


def _wav(path: Path, start: float, end: float) -> bytes | None:
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        first = max(0, int(start * rate))
        if first >= source.getnframes():
            return None
        source.setpos(first)
        frames = source.readframes(int((end - start) * rate))
        out = io.BytesIO()
        with wave.open(out, "wb") as clip:
            clip.setnchannels(source.getnchannels())
            clip.setsampwidth(source.getsampwidth())
            clip.setframerate(rate)
            clip.writeframes(frames)
    return out.getvalue()


def clip(path, start: float, end: float) -> tuple[bytes, str] | None:
    """(audio bytes, MIME type) from start to end seconds (at most MAX_SECONDS), or None when the recording's
    format can't be cut here or the times are outside it."""
    path = Path(path)
    start = max(0.0, float(start))
    end = min(float(end), start + MAX_SECONDS)
    if end <= start or not path.is_file():
        return None
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            data = _mp3(path, start, end)
            return (data, "audio/mp3") if data else None
        if suffix == ".wav":
            data = _wav(path, start, end)
            return (data, "audio/wav") if data else None
        if suffix in (".m4a", ".aac"):
            converted = _as_wav(path)
            data = _wav(converted, start, end) if converted else None
            return (data, "audio/wav") if data else None
    except (OSError, EOFError, ValueError, wave.Error):
        return None
    return None
