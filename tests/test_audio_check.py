"""Cutting clips from a recording, and the flag-only audio check that listens to them."""
from __future__ import annotations

import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
import wave

from audio_test_helpers import wav_bytes
from hearing_compiler import audio_check, clips, extraction
from hearing_compiler.pipeline import InputError
from hearing_compiler.store import Store
from test_gemini_extraction import KEY, Client, Keys, drafted, hearing, morgan

FRAME = bytes([0xFF, 0xFB, 0x50, 0xC4]) + bytes(204)  # MPEG-1 Layer III, 64 kbit/s, 44.1 kHz: 208 bytes, 26 ms.
SECONDS_PER_FRAME = 1152 / 44100
LINES = ("Next, House 1111, an act about clean rivers.", "I'm Alex Morgan with Example Legal Services.",
         "The bill funds 40 monitoring stations in every county.", "Thank you. Seeing no questions, thank you.")


def counted(spoken="The bill funds 40 monitoring stations in every county"):
    return morgan("H.1111", bill_check=None, end_cue=4, arguments=[
        {"text": "Funds 40 monitoring stations.", "quote": spoken, "cues": [3]},
        {"text": "Rivers need cleaning.", "quote": "I'm Alex Morgan with Example Legal Services", "cues": [2]}])


class ClipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_mp3_is_cut_at_frame_boundaries_past_tags_and_junk(self):
        tag = b"ID3\x04\x00\x00" + bytes([0, 0, 0, 10]) + bytes(10)
        data = tag + FRAME * 500 + b"junk" + FRAME * 500
        self.assertEqual(len(clips.mp3_frames(data)[0]), 1000)
        # Gaps of junk (over a megabyte in all) and a lone false sync don't lose or invent frames.
        gappy = FRAME * 100 + (bytes(300_000) + FRAME[:4] + bytes(50) + FRAME * 100) * 5
        self.assertEqual(len(clips.mp3_frames(gappy)[0]), 600)
        path = self.folder / "hearing.mp3"
        path.write_bytes(data)
        audio, mime = clips.clip(path, 2.0, 4.0)
        self.assertEqual(mime, "audio/mp3")
        self.assertEqual(len(audio) % len(FRAME), 0)
        self.assertAlmostEqual(len(audio) / len(FRAME) * SECONDS_PER_FRAME, 2.0, delta=SECONDS_PER_FRAME * 2)
        self.assertTrue(audio.startswith(FRAME[:4]))
        long = clips.clip(path, 0, 120)[0]  # At most MAX_SECONDS, and never past the end.
        self.assertLessEqual(len(long) / len(FRAME) * SECONDS_PER_FRAME, clips.MAX_SECONDS)
        self.assertIsNone(clips.clip(path, 60, 70))

    def test_an_encoders_mp3_is_cut_by_its_own_frames(self):
        try:
            from audio_test_helpers import mp3_bytes
            data = mp3_bytes(30)  # LAME, 8 kHz mono: MPEG-2.5 frames after a Xing/Info header.
        except ImportError:
            self.skipTest("lameenc isn't installed")
        offsets, _, starts = clips.mp3_frames(data)
        self.assertAlmostEqual(starts[-1], 30, delta=0.5)
        path = self.folder / "encoded.mp3"
        path.write_bytes(data)
        audio, _ = clips.clip(path, 10, 15)
        self.assertEqual(audio[:2], bytes([0xFF, 0xE3]))  # Starts on a frame header (MPEG-2.5).
        clips.forget()

    def test_wav_is_cut_and_other_formats_are_left_alone(self):
        path = self.folder / "hearing.wav"
        path.write_bytes(wav_bytes(20))
        audio, mime = clips.clip(path, 5, 8)
        self.assertEqual(mime, "audio/wav")
        self.assertTrue(audio.startswith(b"RIFF"))
        (self.folder / "hearing.ogg").write_bytes(b"OggS" + bytes(100))
        self.assertIsNone(clips.clip(self.folder / "hearing.ogg", 5, 8))

    @unittest.skipUnless(shutil.which("afconvert"), "afconvert is part of macOS (CI runs this on a Mac)")
    def test_an_m4a_from_quicktime_is_cut_through_a_temporary_wav(self):
        source = self.folder / "source.wav"
        source.write_bytes(wav_bytes(20))
        m4a = self.folder / "hearing.m4a"
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(source), str(m4a)], check=True)
        audio, mime = clips.clip(m4a, 5, 8)
        self.assertEqual(mime, "audio/wav")
        with wave.open(io.BytesIO(audio)) as heard:
            self.assertAlmostEqual(heard.getnframes() / heard.getframerate(), 3, delta=0.2)
        temporary = next(v for v in clips._cache.values() if isinstance(v, Path))
        clips.forget()
        self.assertFalse(temporary.exists())


class AudioCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.recording = self.folder / "hearing.wav"
        self.recording.write_bytes(wav_bytes(60))

    def tearDown(self):
        self.temporary.cleanup()

    def test_names_and_figures_nobody_flagged_are_heard_and_a_difference_is_flagged_never_changed(self):
        draft = drafted(hearing(*LINES), [counted()])
        unit = draft["units"][0]
        chosen = [path for path, _, _ in audio_check.candidates(draft)]
        self.assertEqual(chosen, ["/units/0/testifiers/0/name", "/units/0/arguments/0/text"])  # Not the figure-free one.
        client = Client()
        client.audio_reply = lambda parts: {"items": [{"clip": 2, "verdict": "different", "heard": "14 stations"},
                                                      {"clip": 1, "verdict": "same", "heard": ""},
                                                      {"clip": 9, "verdict": "different", "heard": "out of range"}]}
        summary = audio_check.run(draft, self.recording, KEY, "gemini-3.8-flash", client, cache_dir=self.folder / "c")
        self.assertEqual((summary["items"], summary["flags"], summary["skipped"]), (2, 1, 0))
        (parts,) = client.audio
        self.assertEqual([("text" in p, "inline_data" in p) for p in parts],
                         [(True, False), (True, False), (False, True), (True, False), (False, True)])
        self.assertIn('Clip 2 (Argument): the draft says "Funds 40 monitoring stations."', parts[3]["text"])
        flag = unit["arguments"][0]["text"]["flag"]
        self.assertTrue(flag.startswith("Audio check: Gemini hears “14 stations” in the recording here."), flag)
        self.assertEqual(unit["arguments"][0]["text"]["value"], "Funds 40 monitoring stations.")
        self.assertNotEqual(unit["arguments"][0]["text"]["review"], "APPROVED")
        self.assertFalse(unit["testifiers"][0]["name"].get("flag"))
        again = Client()
        audio_check.run(drafted(hearing(*LINES), [counted()]), self.recording, KEY, "gemini-3.8-flash", again,
                        cache_dir=self.folder / "c")
        self.assertEqual(again.audio, [])  # The same clips and items: the saved reply is reused.

    def test_a_clip_holds_only_the_lines_near_the_figure_and_big_clips_go_in_separate_requests(self):
        lines = ("Next, House 1111.", "I'm Alex Morgan with Example Legal Services.", "Some words about rivers.",
                 "More words.", "The bill funds 40 monitoring stations in every county.", "Thank you.")
        document = hearing(*lines)
        document["segments"][4].update(start=300, end=309)  # The figure's line, minutes after the others.
        document["segments"][5].update(start=310, end=319)
        entry = morgan("H.1111", bill_check=None, end_cue=6, testifiers=[], arguments=[
            {"text": "Funds 40 monitoring stations.", "quote": "Some words about rivers", "cues": [3, 5]}])
        draft = drafted(document, [entry])
        draft["units"][0]["arguments"][0]["text"]["evidence"] = ["c3", "c5"]
        start, end, said = audio_check._window(draft, draft["units"][0]["arguments"][0]["text"], "Argument")
        self.assertEqual((start, end, said), (298.5, 310.5, "The bill funds 40 monitoring stations in every county."))
        recording = self.folder / "long.wav"
        recording.write_bytes(wav_bytes(330))
        two = drafted(hearing(*LINES), [counted()])
        client = Client()
        with unittest.mock.patch.object(audio_check, "REQUEST_BYTES", 10):
            audio_check.run(two, recording, KEY, "gemini-3.8-flash", client)
        self.assertEqual(len(client.audio), 2)  # One testimony, two clips over the limit: two requests.

    def test_whatever_wasnt_heard_is_said_and_a_failed_check_can_be_rebuilt(self):
        keys = Keys()
        keys.set(KEY)
        store = Store(self.folder / "state")
        service = extraction.ExtractionService(store, threading.Event(), keys, Client())
        ogg = self.folder / "hearing.ogg"
        ogg.write_bytes(b"OggS" + bytes(100))
        draft = drafted(hearing(*LINES), [counted()])
        service.check_audio(draft, ogg)
        self.assertTrue([i for i in draft["issues"] if "couldn't cut clips" in i["value"] and i["flag"]])

        def busy(parts):
            raise extraction.gemini.GeminiError("Gemini is busy.")
        service.client.audio_reply = busy
        draft = drafted(hearing(*LINES), [counted()])
        service.check_audio(draft, self.recording)
        self.assertTrue([i for i in draft["issues"] if "didn't hear every name and figure" in i["value"]])
        job = {"id": "j", "processing": "Complete", "input": {"x": 1}, "draft": draft, "revision": 1}
        draft["extraction_provenance"]["drafting"] = extraction.DRAFTING
        draft["second_check"] = {"failed": []}
        self.assertNotIn("latest fixes", store._rebuild_refusal(job) or "")  # Rebuild may try the check again.

    def test_the_recording_path_stays_out_of_the_transcripts_fingerprint(self):
        keys = Keys()
        keys.set(KEY)
        store = Store(self.folder / "state")
        service = extraction.ExtractionService(store, threading.Event(), keys, Client())
        service.settings = lambda: {"provider": "gemini", "model": "gemini-3.8-flash"}
        document = hearing(*LINES)
        plain = service(document)["extraction_provenance"]["input_sha256"]
        heard = service({**document, "recording_path": str(self.recording)})
        self.assertEqual(heard["extraction_provenance"]["input_sha256"], plain)
        self.assertIn("audio_check", heard)

    def test_a_recording_that_cant_be_cut_is_skipped_and_closing_the_app_stops_the_check(self):
        draft = drafted(hearing(*LINES), [counted()])
        ogg = self.folder / "hearing.ogg"
        ogg.write_bytes(b"OggS" + bytes(100))
        client = Client()
        summary = audio_check.run(draft, ogg, KEY, "gemini-3.8-flash", client)
        self.assertEqual((summary["items"], summary["skipped"], client.audio), (0, 2, []))

        def closing(seconds):
            raise InputError("Drafting stopped because the app is closing.")
        with self.assertRaisesRegex(InputError, "closing"):
            audio_check.run(drafted(hearing(*LINES), [counted()]), self.recording, KEY, "gemini-3.8-flash", Client(),
                            wait=closing)

    def test_the_service_runs_it_only_with_a_recording_and_a_key_and_survives_a_failure(self):
        keys = Keys()
        service = extraction.ExtractionService(Store(self.folder / "state"), threading.Event(), keys, Client())
        draft = drafted(hearing(*LINES), [counted()])
        service.check_audio(draft, self.recording)
        self.assertNotIn("audio_check", draft)  # No key saved.
        keys.set(KEY)

        def broken(parts):
            raise RuntimeError("unexpected")
        service.client.audio_reply = broken
        service.check_audio(draft, self.recording)
        self.assertEqual(draft["audio_check"]["failed_testimonies"], 1)
        self.assertFalse(draft["units"][0]["arguments"][0]["text"].get("flag"))


if __name__ == "__main__":
    unittest.main()
