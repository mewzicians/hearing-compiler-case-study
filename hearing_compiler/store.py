"""Atomic local JSON persistence, duplicate reuse and independent job failures."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .pipeline import InputError, correct, extract, fields, fingerprint, markdown, prepare_review, review_summary

TIMED = ("Waiting", "Transcribing", "Analyzing")  # Stages the progress timer counts.
REBUILD_MISS = ("This hearing's saved AI reply couldn't be matched, so rebuilding it would need a new paid draft. "
                "Nothing was changed.")


class Conflict(InputError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_stage(job: dict, processing: str):
    """Change a job's stage; entering a timed stage records when it began."""
    if processing not in TIMED:
        job.pop("stage_started_at", None)
    elif job.get("processing") != processing or not job.get("stage_started_at"):
        job["stage_started_at"] = _now()
    job["processing"] = processing


def untouched(draft: dict) -> bool:
    """Nobody has reviewed this draft: no saved review step, approval or correction."""
    return not draft.get("review_log") and not any(item["review"] == "APPROVED" or item.get("corrected")
                                                   for _, item in fields(draft))


class SavedReplyOnly:
    """The Gemini client for rebuilding: only a saved reply may be used, never a new (paid) call."""

    @staticmethod
    def call_gemini(*_, **__):
        raise InputError(REBUILD_MISS)


class Store:
    def __init__(self, directory: Path, extractor=None):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.lock = threading.RLock()
        self.verified_media = {}
        self.pending_failures = {}
        self.extractor = extractor
        self.on_queued = None
        self.rebuilding = set()
        self._summaries = {}
        self.data = {"schema_version": 1, "jobs": {}}
        if self.path.exists():
            try:
                original = self.path.read_bytes()
                self.data = json.loads(original.decode("utf-8"))
                if (self.data.get("schema_version") != 1 or not isinstance(self.data.get("jobs"), dict)
                        or not isinstance(self.data.get("set_aside", []), list)):
                    raise ValueError("unsupported state format")
            except (ValueError, AttributeError) as error:
                raise InputError(f"Cannot read saved state at {self.path}. Preserve it and choose another --state directory: {error}") from error
            self._set_aside_unreadable(original)
            self._restore_readable()
        interrupted = False
        for job in self.data["jobs"].values():
            if job["processing"] not in ("Complete", "Failed", "Waiting", "Awaiting transcript"):
                job["failed_stage"] = "transcription" if job["processing"] == "Transcribing" else "extraction"
                set_stage(job, "Failed")
                job["error"] = "Processing was interrupted. Retry this hearing."
                interrupted = True
        self.reset_stage_timers()
        if interrupted:
            self._save()

    def reset_stage_timers(self):
        """Hearings left waiting by an earlier session (or a restored backup) start their wait now."""
        with self.lock:
            for job in self.data["jobs"].values():
                if job["processing"] in TIMED:
                    job["stage_started_at"] = _now()

    def _set_aside_unreadable(self, original: bytes):
        """One malformed saved hearing must not lock away the rest of the workspace."""
        from .saved_state import validate_saved_state
        unreadable = {}
        for job_id, job in self.data["jobs"].items():
            try:
                validate_saved_state({"schema_version": 1, "jobs": {job_id: job}})
            except InputError as error:
                unreadable[job_id] = str(error)
        if not unreadable:
            return
        backups = self.directory / "backups"
        backups.mkdir(exist_ok=True)
        with (backups / f"before-set-aside-{uuid.uuid4().hex}.json").open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        now = datetime.now(timezone.utc).isoformat()
        for job_id, reason in unreadable.items():
            self.data.setdefault("set_aside", []).append(
                {"id": job_id, "reason": reason, "set_aside_at": now, "job": self.data["jobs"].pop(job_id)})
        logging.warning("Set aside %d unreadable saved hearing(s); the original state is preserved in %s",
                        len(unreadable), backups)
        self._save()

    def _restore_readable(self):
        """Set-aside hearings return once they validate again (after a fix, or a newer build)."""
        from .saved_state import validate_saved_state
        kept, restored = [], 0
        for entry in self.data.get("set_aside", []):
            job = entry.get("job") if isinstance(entry, dict) else None
            job_id = job.get("id") if isinstance(job, dict) else None
            try:
                if not isinstance(job_id, str) or job_id in self.data["jobs"]:
                    raise InputError("not restorable")
                validate_saved_state({"schema_version": 1, "jobs": {job_id: job}})
            except InputError:
                kept.append(entry)
                continue
            self.data["jobs"][job_id] = job
            restored += 1
        if restored:
            self.data["set_aside"] = kept
            logging.info("Restored %d previously set-aside hearing(s)", restored)
            self._save()

    def _save(self):
        temporary = self.directory / f".state-{uuid.uuid4().hex}.tmp"
        failed = False
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(self.data, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            failed = True
            raise
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    if not failed:
                        raise
                    logging.exception("Temporary state cleanup failed after the primary save error")

    def _draft_facts(self, job: dict) -> tuple[dict, bool]:
        """Review summary and untouched(), cached per revision: the UI asks for every hearing once a second."""
        draft = job.get("draft")
        if not draft:
            return {"status": "PENDING", "fields": 0, "approved": 0, "unresolved": 0}, False
        key = (job["revision"], id(draft))
        cached = self._summaries.get(job["id"])
        if cached is None or cached[0] != key:
            cached = (key, review_summary(draft), untouched(draft))
            self._summaries[job["id"]] = cached
        return copy.deepcopy(cached[1]), cached[2]

    def _rebuild_refusal(self, job: dict) -> str | None:
        """Why POST /api/rebuild would refuse this hearing, or None when it would be accepted."""
        draft = job.get("draft") or {}
        if job["processing"] in TIMED:
            return "This hearing is still processing. Wait for its draft before rebuilding."
        if job["id"] in self.rebuilding:
            return "This hearing is already being rebuilt."
        if (job["processing"] != "Complete" or not job.get("input") or draft.get("provider") != "gemini-draft-v1"
                or not isinstance((draft.get("extraction_provenance") or {}).get("model"), str)):
            return "Only a finished Gemini draft can be rebuilt from its saved AI reply."
        from .extraction import DRAFTING
        checked, heard = draft.get("second_check"), draft.get("audio_check")
        if ((draft.get("extraction_provenance") or {}).get("drafting") == DRAFTING
                and not (isinstance(checked, dict) and checked.get("failed"))
                and not (isinstance(heard, dict) and heard.get("failed_testimonies"))):
            return "This draft already has the app's latest fixes."  # A check that didn't fully run can be retried.
        if not self._draft_facts(job)[1]:
            return "This draft already has your review work, so it won't be replaced."
        return None

    def _progress(self, job: dict) -> dict:
        """Progress-timer data: when the current stage began, and the recording's length as far as the
        app knows it (the transcript's last cue end)."""
        end = ((job.get("input") or {}).get("segments") or [{}])[-1].get("end")
        return {"stage_started_at": job.get("stage_started_at") if job["processing"] in TIMED else None,
                "duration_seconds": end if isinstance(end, (int, float)) and not isinstance(end, bool) else None,
                "can_rebuild": self._rebuild_refusal(job) is None}

    def _summary(self, job: dict) -> dict:
        draft = job.get("draft")
        return {
            "id": job["id"],
            "hearing": copy.deepcopy(job["hearing"]),
            "processing": job["processing"],
            "error": (f"Failure could not be saved; storage recovery is pending: {self.pending_failures[job['id']]['storage_error']}"
                      if job["id"] in self.pending_failures else job.get("error")),
            "revision": job["revision"],
            "kind": "audio" if job.get("audio") else "transcript",
            "audio": bool(job.get("audio")),
            "failed_stage": job.get("failed_stage"),
            "review": self._draft_facts(job)[0],
            "draft_method": None if not draft else "gemini" if draft.get("provider") == "gemini-draft-v1"
                            else "local" if draft.get("draft_format") else "rules",
            **self._progress(job),
        }

    def list(self) -> list[dict]:
        with self.lock:
            result = [self._summary(job) for job in self.data["jobs"].values()]
            return sorted(result, key=lambda item: (item["id"] != "sample-hearing", item["hearing"].get("date", ""), item["id"]))

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.data["jobs"]:
                raise KeyError(job_id)
            job = copy.deepcopy(self.data["jobs"][job_id])
            job.update(self._progress(self.data["jobs"][job_id]))
            if job_id in self.pending_failures:
                job["persistence_error"] = copy.deepcopy(self.pending_failures[job_id])
                job["error"] = self._summary(job)["error"]
            if job.get("draft"):
                # Prepare a review view without rewriting old work at startup.
                job["draft"] = prepare_review(job["draft"])
                job["review"] = review_summary(job["draft"])
                job["markdown"] = markdown(job["draft"])
                if job["draft"].get("draft_format") == "hearing-review-units/v1":
                    from .rich_review import notion
                    job["notion"] = notion(job["draft"])
            if job.get("audio"):
                job["audio_url"] = f"/api/audio/{job_id}"
            return job

    def export_snapshot(self, job_id: str, revision: int | None = None) -> dict:
        """One consistent saved review, independent of any open tab's edits.

        Recording bytes are kept separately in the local workspace. The archive
        retains their identity, transcript, corrections, removed-item history,
        and review state, without exporting machine-local asset paths.
        """
        with self.lock:
            job = self.get(job_id)
            if job["processing"] != "Complete" or not job.get("draft"):
                raise InputError("Only a completed draft can be exported. Processing completion does not mean human approval.")
            if revision is not None and (
                isinstance(revision, bool) or not isinstance(revision, int)
                or revision != job["revision"]
            ):
                raise Conflict("The saved review changed in another tab. Reload before exporting.")
            for runtime in ("audio_url", "stage_started_at", "duration_seconds", "can_rebuild"):
                job.pop(runtime, None)
            if job.get("audio"):
                job["audio"] = {key: job["audio"][key] for key in
                                ("filename", "sha256", "bytes", "content_type")
                                if key in job["audio"]}
            return {
                "schema_version": 1,
                "kind": "hearing-review-archive",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "recording_included": False,
                "job": job,
            }

    def settings(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data.get("transcription", {"model_id": ""}))

    def extraction_settings(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data.get("extraction", {"provider": "rules"}))

    def set_extraction(self, settings):
        with self.lock:
            previous = copy.deepcopy(self.data)
            self.data["extraction"] = copy.deepcopy(settings)
            try:
                self._save()
            except Exception:
                self.data = previous
                raise

    def model_attempts(self, job_id):
        with self.lock:
            return copy.deepcopy(self.data["jobs"].get(job_id, {}).get("model_attempts", []))

    def record_model_attempt(self, job_id, record):
        with self.lock:
            job = self.data["jobs"][job_id]
            if job["processing"] != "Analyzing":
                raise Conflict("This source is no longer extracting; its saved work is preserved.")
            previous = copy.deepcopy(job)
            job.setdefault("model_attempts", []).append(copy.deepcopy(record))
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise

    def media_asset(self, job_id: str) -> dict:
        with self.lock:
            return copy.deepcopy(self.data["jobs"][job_id].get("audio"))

    def set_model(self, model_id: str, output_format: str = "json"):
        if output_format not in ("json", "srt"):
            raise InputError("Choose SRT or MacWhisper JSON.")
        with self.lock:
            previous = copy.deepcopy(self.data)
            self.data["transcription"] = {"model_id": model_id, "output_format": output_format}
            for job in self.data["jobs"].values():
                if job.get("audio") and job["processing"] == "Awaiting transcript":
                    set_stage(job, "Waiting")
            try:
                self._save()
            except Exception:
                self.data = previous
                raise

    def enqueue_audio(self, metadata: dict, audio: dict, can_transcribe: bool) -> dict:
        for key in ("title", "date", "committee", "source_file"):
            if not isinstance(metadata.get(key), str) or not metadata[key].strip():
                raise InputError(f"Hearing {key} is required.")
        if metadata.get("source_scope") != "public_hearing":
            raise InputError("Only explicitly declared public hearing recordings are accepted.")
        from .agenda import check_url
        hearing_url = check_url(metadata.get("hearing_url"))
        with self.lock:
            existing = next((job for job in self.data["jobs"].values()
                             if job.get("audio", {}).get("sha256") == audio["sha256"]), None)
            if existing:
                return {**self._summary(existing), "reused": True}
            job_id = "audio-" + audio["sha256"][:24]
            if job_id in self.data["jobs"]:
                raise Conflict("This hearing id already owns saved transcript work. Attach the recording to that hearing instead.")
            job = {
                "id": job_id,
                "hearing": {key: metadata[key].strip() for key in ("title", "date", "committee", "source_file")},
                "audio": copy.deepcopy(audio),
                "input": None,
                "fingerprint": audio["sha256"],
                "processing": "Waiting" if can_transcribe else "Awaiting transcript",
                "revision": 0,
                "draft": None,
                "error": None,
                "failed_stage": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            job["hearing"]["id"] = job_id
            set_stage(job, job["processing"])
            if hearing_url:
                job["hearing_url"] = hearing_url
            self.data["jobs"][job_id] = job
            try:
                self._save()
            except Exception:
                del self.data["jobs"][job_id]
                raise
            return {**self._summary(job), "reused": False}

    def claim_audio(self, can_transcribe: bool) -> dict | None:
        with self.lock:
            for job in self.data["jobs"].values():
                # Recordings and transcript-only imports both wait here for their turn.
                if job["processing"] != "Waiting" or not (job.get("audio") or job.get("input")):
                    continue
                if job.get("input") is None and not can_transcribe:
                    previous = copy.deepcopy(job)
                    set_stage(job, "Awaiting transcript")
                    try:
                        self._save()
                    except Exception:
                        self.data["jobs"][job["id"]] = previous
                        raise
                    continue
                previous = copy.deepcopy(job)
                set_stage(job, "Analyzing" if job.get("input") else "Transcribing")
                job["error"] = None
                job["failed_stage"] = None
                try:
                    self._save()
                except Exception:
                    self.data["jobs"][job["id"]] = previous
                    raise
                return copy.deepcopy(job)
        return None

    def cache_transcript(self, job_id: str, document: dict, from_worker: bool = False):
        from .pipeline import validate
        document = validate(document)
        with self.lock:
            job = self.data["jobs"][job_id]
            if not job.get("audio"):
                raise InputError("Choose a recording before attaching its transcript.")
            if job["processing"] == "Complete":
                raise Conflict("This hearing already has a completed draft. Its review work is preserved.")
            if job["processing"] in ("Transcribing", "Analyzing") and not from_worker:
                raise Conflict("This hearing is processing. Wait before attaching another transcript.")
            document["hearing"] = copy.deepcopy(job["hearing"])
            if job.get("hearing_url"):
                document["hearing_url"] = job["hearing_url"]
            previous = copy.deepcopy(job)
            job["input"] = document
            set_stage(job, "Analyzing" if from_worker else "Waiting")
            job["error"] = None
            job["failed_stage"] = None
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise

    def save_transcription_output(self, job_id: str, output: str, filename: str, output_format: str, model_id: str):
        """Keep raw mw output before normalization, so retry never repeats a long transcription."""
        data = output.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        relative = f"transcription-output/{digest}.{output_format}"
        target = self.directory / relative
        if not target.is_file():
            target.parent.mkdir(exist_ok=True)
            temporary = target.parent / f".{uuid.uuid4().hex}.tmp"
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        with self.lock:
            job = self.data["jobs"][job_id]
            previous = copy.deepcopy(job)
            job["transcription_output"] = {"path": relative, "sha256": digest, "bytes": len(data), "filename": filename,
                                           "format": output_format, "model_id": model_id}
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise

    def saved_transcription_output(self, job_id: str, output_format: str, model_id: str) -> tuple[str, str] | None:
        """Saved output from the same model and format, or None to transcribe again."""
        with self.lock:
            record = copy.deepcopy(self.data["jobs"][job_id].get("transcription_output"))
        if (not isinstance(record, dict) or record.get("format") != output_format or record.get("model_id") != model_id
                or not isinstance(record.get("filename"), str)
                or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or record.get("path") != f"transcription-output/{record['sha256']}.{output_format}"):
            return None
        try:
            data = (self.directory / record["path"]).read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            return None
        return data.decode("utf-8"), record["filename"]

    def finish_audio(self, job_id: str, draft: dict):
        with self.lock:
            job = self.data["jobs"][job_id]
            previous = copy.deepcopy(job)
            job["draft"] = copy.deepcopy(draft)
            # The saved input keeps its first filename; an attached recording renamed the hearing since.
            job["draft"]["hearing"] = copy.deepcopy(job["hearing"])
            set_stage(job, "Complete")
            job["revision"] += 1
            job["error"] = None
            job["failed_stage"] = None
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise

    def fail_audio(self, job_id: str, stage: str, message: str):
        message = str(message).encode("utf-8", errors="backslashreplace").decode("utf-8")
        with self.lock:
            job = self.data["jobs"][job_id]
            previous = copy.deepcopy(job)
            set_stage(job, "Failed")
            job["failed_stage"] = stage
            job["error"] = message
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise
            self.pending_failures.pop(job_id, None)

    def defer_failure(self, job_id: str, stage: str, message: str, storage_error: Exception):
        with self.lock:
            self.pending_failures[job_id] = {
                "stage": stage,
                "message": str(message).encode("utf-8", errors="backslashreplace").decode("utf-8"),
                "storage_error": str(storage_error).encode("utf-8", errors="backslashreplace").decode("utf-8"),
                "persisted": False}

    def flush_failures(self):
        with self.lock:
            for job_id, pending in list(self.pending_failures.items()):
                self.fail_audio(job_id, pending["stage"], pending["message"])

    def retry_audio(self, job_id: str, can_transcribe: bool) -> dict:
        with self.lock:
            job = self.data["jobs"][job_id]
            if not job.get("audio") or job["processing"] != "Failed":
                raise Conflict("Only failed audio jobs can be retried.")
            previous = copy.deepcopy(job)
            set_stage(job, "Waiting" if job.get("input") or can_transcribe else "Awaiting transcript")
            job["error"] = None
            job["failed_stage"] = None
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise
            return self._summary(job)

    def archive_legacy_samples(self):
        """Remove only former built-in fixtures, keeping a recoverable local backup."""
        legacy_sources = {
            "sample-hearing": "sample_hearing_2026-03-18.mp3",
            "multiple-bills": "sample_housing_2026-03-19.mp3",
            "uncertain-testimony": "sample_consumer_2026-03-20.mp3",
            "interrupted-followup": "sample_followup_2026-03-21.mp3",
            "mixed-position": "sample_policy_2026-03-22.mp3",
            "no-questions": "sample_education_2026-03-23.mp3",
            "broken-order": "sample_broken_2026-03-24.mp3",
        }
        with self.lock:
            if self.data.get("legacy_samples_archived"):
                return
            obsolete = [job_id for job_id, job in self.data["jobs"].items()
                        if not job.get("audio") and job_id in legacy_sources
                        and (job.get("input") or {}).get("synthetic") is True
                        and job["hearing"].get("source_file") == legacy_sources[job_id]]
            previous = copy.deepcopy(self.data)
            if obsolete:
                backups = self.directory / "backups"
                backups.mkdir(exist_ok=True)
                backup = backups / f"before-sample-removal-{uuid.uuid4().hex}.json"
                backup.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
                for job_id in obsolete:
                    del self.data["jobs"][job_id]
            self.data["legacy_samples_archived"] = True
            try:
                self._save()
            except Exception:
                self.data = previous
                raise

    def discard_unused_audio(self, audio: dict):
        """Delete a just-received recording that no saved hearing ended up using."""
        with self.lock:
            used = {(job.get("audio") or {}).get("path") for job in self.data["jobs"].values()}
            used |= {((entry.get("job") or {}).get("audio") or {}).get("path")
                     for entry in self.data.get("set_aside", []) if isinstance(entry, dict)}
            path = self.directory / audio["path"]
            if audio["path"] not in used and path.is_file() and path.parent == self.directory / "audio":
                path.unlink()

    def attach_audio(self, job_id: str, audio: dict) -> dict:
        with self.lock:
            job = self.data["jobs"][job_id]
            if job.get("audio"):
                raise Conflict("This hearing already has a source recording.")
            previous = copy.deepcopy(job)
            job["audio"] = copy.deepcopy(audio)
            job["hearing"]["source_file"] = audio["filename"]
            if job.get("draft"):
                job["draft"]["hearing"] = copy.deepcopy(job["hearing"])
            job["revision"] += 1
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise
            return self._summary(job)

    def import_one(self, document: dict, replace_failed: str | None = None) -> dict:
        if not isinstance(document, dict):
            raise InputError("Each transcript must be a JSON object.")
        try:
            digest = fingerprint(document)
        except (ValueError, TypeError) as error:
            raise InputError("Transcript must contain ordinary finite JSON values.") from error
        with self.lock:
            existing = self.data["jobs"]
            metadata = document.get("hearing", {})
            if not isinstance(metadata, dict):
                metadata = {}
            if replace_failed:
                if replace_failed not in existing:
                    raise KeyError(replace_failed)
                if existing[replace_failed]["processing"] != "Failed":
                    raise Conflict("Only failed hearings can be retried. Completed work is preserved.")
                job_id = replace_failed
                if metadata.get("id") != job_id:
                    raise InputError("A replacement transcript must use the failed hearing's id.")
            else:
                reused = next((job for job in existing.values() if job["fingerprint"] == digest), None)
                if reused:
                    return {**self._summary(reused), "reused": True}
                requested = metadata.get("id")
                job_id = requested if isinstance(requested, str) and requested else f"failed-{digest[:12]}"
                if job_id in existing:
                    raise Conflict("This hearing id already has different input. Use a new id, or retry the failed hearing.")
            # Slow (hosted) drafting runs in the background queue instead of this request.
            background = bool(getattr(self.extractor, "background", False))
            job = {
                "id": job_id,
                "hearing": {k: str(metadata.get(k, "")) for k in ("id", "title", "date", "committee", "source_file")},
                "input": copy.deepcopy(document),
                "fingerprint": digest,
                "processing": "Waiting" if background else "Analyzing",
                "revision": existing.get(job_id, {}).get("revision", -1) + 1,
                "draft": None,
                "error": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            set_stage(job, job["processing"])
            if replace_failed:
                job["model_attempts"] = copy.deepcopy(existing[job_id].get("model_attempts", []))
            previous = copy.deepcopy(existing.get(job_id))
            existing[job_id] = job
            try:
                # Original intake must survive process exit inside a slow,
                # replaceable extractor. No network/model work holds this lock.
                self._save()
            except Exception:
                if previous is None:
                    del existing[job_id]
                else:
                    existing[job_id] = previous
                raise
            if background:
                if self.on_queued:
                    self.on_queued()
                return {**self._summary(job), "reused": False}
        try:
            draft = (self.extractor or extract)(copy.deepcopy(document))
            processing, error = "Complete", None
        except InputError as failure:
            draft, processing, error = None, "Failed", str(failure).encode("utf-8", errors="backslashreplace").decode("utf-8")
        except Exception:
            draft, processing, error = None, "Failed", "Unexpected extraction failure. Inspect the server log before retrying."
            logging.exception("Extraction failed for hearing %s", job_id)
        with self.lock:
            # Another failed mutation may have restored a deep copy of the
            # registry while extraction ran. Commit into its current owner.
            job = self.data["jobs"].get(job_id)
            if job is None or job["fingerprint"] != digest or job["processing"] != "Analyzing":
                raise Conflict("The source job changed while extraction was running. Its current work is preserved.")
            previous = copy.deepcopy(job)
            job.update(draft=draft, error=error, failed_stage="extraction" if error else None)
            set_stage(job, processing)
            try:
                self._save()
            except Exception as storage_error:
                self.data["jobs"][job_id] = previous
                self.defer_failure(job_id, "extraction",
                                   "Extraction finished, but its result could not be saved. Retry this hearing when storage is available.",
                                   storage_error)
                raise
            return {**self._summary(job), "reused": False}

    def import_batch(self, documents: list[dict]) -> list[dict]:
        if not isinstance(documents, list) or not 1 <= len(documents) <= 30:
            raise InputError("Import between 1 and 30 transcript objects at a time.")
        result = []
        for document in documents:
            try:
                result.append(self.import_one(document))
            except (InputError, KeyError) as error:
                result.append({"processing": "Failed", "error": str(error), "hearing": document.get("hearing", {}) if isinstance(document, dict) else {}})
        return result

    def retry(self, job_id: str, replacement: dict | None = None) -> dict:
        with self.lock:
            if job_id not in self.data["jobs"]:
                raise KeyError(job_id)
            document = copy.deepcopy(replacement if replacement is not None else self.data["jobs"][job_id]["input"])
        return self.import_one(document, replace_failed=job_id)

    def redraft(self, job_id: str) -> dict:
        """Replace an untouched draft with a new one from the current draft method (e.g. Gemini)."""
        with self.lock:
            if job_id not in self.data["jobs"]:
                raise KeyError(job_id)
            job = self.data["jobs"][job_id]
            if job["processing"] != "Complete" or not job.get("input") or not job.get("draft"):
                raise Conflict("Only a finished draft with a saved transcript can be redrafted.")
            if not untouched(job["draft"]):
                raise Conflict("This draft already has your review work, so it won't be replaced.")
            if not getattr(self.extractor, "background", False):
                raise InputError("Turn on Gemini in AI extraction first.")
            previous = copy.deepcopy(job)
            job.update(error=None, failed_stage=None)
            set_stage(job, "Waiting")
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise
        if self.on_queued:
            self.on_queued()
        return self._summary(job)

    def rebuild(self, job_id: str) -> dict:
        """Redo an unreviewed Gemini draft with the current drafting code from its saved reply.

        Drafting never calls Gemini: a reply that can't be matched fails with nothing changed. The second check
        reuses its saved replies, so it calls Gemini only for testimonies whose draft text changed.
        """
        from . import agenda
        from .extraction import gemini_draft
        with self.lock:
            if job_id not in self.data["jobs"]:
                raise KeyError(job_id)
            job = self.data["jobs"][job_id]
            refusal = self._rebuild_refusal(job)
            if refusal:
                raise Conflict(refusal)
            document, revision = copy.deepcopy(job["input"]), job["revision"]
            model, agenda_url, kept, ranges, committee = (job["draft"]["extraction_provenance"].get(k) for k in
                                                          ("model", "agenda_url", "agenda", "parts", "committee"))
            self.rebuilding.add(job_id)
        try:
            # The saved reply answers a prompt holding the agenda as drafting fetched it. Drafts from build 22 keep
            # that agenda, so a later change to the page can't block a rebuild; older drafts fetch it the same way.
            loaded = None
            if agenda_url and kept and all("sponsor" in bill for bill in kept):
                loaded = {"bills": kept, "url": agenda_url, "committee": committee}
            elif agenda_url:
                try:
                    loaded = agenda.fetch(agenda_url)
                except InputError as error:
                    raise InputError(f"Rebuilding needs the hearing page's agenda to find the saved AI reply, and it "
                                     f"couldn't be loaded ({error}). Nothing was changed; try again later.") from None

            def fetch(url):
                if loaded is None:
                    raise InputError("the first draft was made without it, and a rebuild keeps that")
                return loaded

            # A long hearing keeps the cuts it was drafted with, so each part's saved reply is found.
            draft = gemini_draft(document, "", model, SavedReplyOnly(), cache_dir=self.directory / "extraction-output",
                                 fetch_agenda=fetch, rebuilding=True, ranges=ranges)
            # The name check and the second check. Saved check replies are reused; only changed testimonies are
            # asked again.
            if getattr(self.extractor, "finish", None) is not None:
                asset = self.media_asset(job_id)
                draft = self.extractor.finish(draft, model=model,
                                              recording=str(self.directory / asset["path"]) if asset else None)
            elif getattr(self.extractor, "second_check", None) is not None:
                draft = self.extractor.second_check(draft, model=model)
            with self.lock:
                job = self.data["jobs"].get(job_id)
                if job is None or job["revision"] != revision or job["processing"] != "Complete":
                    raise Conflict("This hearing changed while it was being rebuilt, so nothing was replaced. Reload it.")
                previous = copy.deepcopy(job)
                draft["hearing"] = copy.deepcopy(job["hearing"])
                job.update(draft=draft, revision=revision + 1, updated_at=_now())
                try:
                    self._save()
                except Exception:
                    self.data["jobs"][job_id] = previous
                    raise
        finally:
            with self.lock:
                self.rebuilding.discard(job_id)
        return self.get(job_id)

    def review(self, job_id: str, revision: int, changes: list[dict] | None = None,
               operations: list[dict] | None = None, request_id: str | None = None) -> dict:
        with self.lock:
            if job_id not in self.data["jobs"]:
                raise KeyError(job_id)
            job = self.data["jobs"][job_id]
            request_hash = None
            if request_id is not None:
                if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", request_id):
                    raise InputError("Use a valid review request identifier.")
                try:
                    request_hash = fingerprint({"revision": revision, "changes": changes, "operations": operations})
                    request_hash.encode("utf-8")
                except (ValueError, UnicodeError, TypeError) as error:
                    raise InputError("Review corrections must contain valid UTF-8 JSON text.") from error
                saved = job.get("review_requests", {}).get(request_id)
                if saved:
                    if saved["fingerprint"] != request_hash:
                        raise Conflict("This request was already saved with different content. Reload and open a new correction.")
                    return self.get(job_id)
            if job["processing"] != "Complete":
                raise InputError("Only completed drafts can be reviewed.")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision != job["revision"]:
                raise Conflict("The draft changed in another tab. Reload it before saving your correction.")
            previous = copy.deepcopy(job)
            job["draft"] = correct(job["draft"], changes, operations)
            job["revision"] += 1
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            if request_id is not None:
                requests = job.setdefault("review_requests", {})
                requests[request_id] = {"fingerprint": request_hash, "revision": job["revision"]}
                while len(requests) > 100:
                    del requests[next(iter(requests))]
            try:
                self._save()
            except Exception:
                self.data["jobs"][job_id] = previous
                raise
            return self.get(job_id)
