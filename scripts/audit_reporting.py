"""Developer audit lifecycle: identity first, final certification after cleanup."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid
import argparse
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
WRITE_LOCK = threading.Lock()


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                             encoding="utf-8", newline="\n")
        # Windows may briefly hold a report open while another audit reads it.
        # Per-run names are independent; serialize local latest-view updates
        # and retry only the OS sharing/lock failures, never arbitrary errors.
        with WRITE_LOCK:
            for attempt in range(6):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError as error:
                    if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 5:
                        raise
                    time.sleep(.05 * (attempt + 1))
    finally:
        if temporary.exists():
            temporary.unlink()


def source_identity():
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                                    cwd=ROOT, text=True).splitlines()
    files = {name: digest(ROOT / name) for name in sorted(set(names)) if (ROOT / name).is_file()}
    return {"commit": revision, "dirty": bool(subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "snapshot_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


class AuditRun:
    def __init__(self, tool, latest, run_id=None):
        self.latest = Path(latest)
        self.data = {"report_version": 1, "run_id": run_id or str(uuid.uuid4()),
                     "started_utc": utc(), "ended_utc": None, "tool": tool, "tool_version": "2",
                     "versions": {"python": platform.python_version()},
                     "run_status": "RUNNING", "result_status": "NOT_EVALUATED", "status": "RUNNING",
                     "source": None, "inputs": {}, "outputs": {}, "artifacts": {},
                     "checks": [], "failures": []}
        self.cleanups = []
        self.storage_failed = False

    @property
    def path(self):
        return self.latest.parent / "runs" / (self.data["run_id"] + ".json")

    def write(self):
        try:
            atomic_json(self.path, self.data)
            atomic_json(self.latest, self.data)
        except Exception as error:
            self.storage_failed = True
            print(f"ERROR run={self.data['run_id']} report={self.latest}: "
                  f"{type(error).__name__}: {error}", file=sys.stderr)
            raise

    def relocate(self, latest):
        self.latest = Path(latest)
        self.write()

    def identity(self, group, name, path):
        path = Path(path).resolve()
        self.data[group][name] = {"path": str(path), "sha256": digest(path), "bytes": path.stat().st_size}
        return self.data[group][name]

    def cleanup(self, callback):
        self.cleanups.append(callback)

    def failure(self, error, stage):
        self.data["failures"].append({"stage": stage, "type": type(error).__name__, "message": str(error)})


def audit_main(tool, body, argv=None, *, latest, run_id=None):
    """body(run, argv) returns desired CLI exit; exceptions and cleanup are guarded."""
    values = list(sys.argv[1:] if argv is None else argv)
    if "--run-id" in values and values.index("--run-id") + 1 < len(values):
        proposed = values[values.index("--run-id") + 1]
        try:
            uuid.UUID(proposed)
            run_id = proposed
        except ValueError:
            pass  # Parser/body diagnoses the bad request under a safe allocated ID.
    if "--report-dir" in values and values.index("--report-dir") + 1 < len(values):
        latest = Path(values[values.index("--report-dir") + 1]) / "report.json"
    run = AuditRun(tool, latest, run_id)
    exit_code = 0
    try:
        if run.path.exists():
            rejected_id = run.data["run_id"]
            run.data["run_id"] = str(uuid.uuid4())
            raise ValueError(f"Run identity already exists: {rejected_id}; allocated fresh failure identity.")
        uuid.UUID(run.data["run_id"])
        run.write()  # Invalid arguments/imports/preflight cannot retain a current PASS.
        run.data["source"] = source_identity()
        exit_code = body(run, values) or 0
        if run.data["result_status"] == "NOT_EVALUATED":
            run.data["result_status"] = "PASS"
    except SystemExit as error:
        if error.code in (None, 0):
            run.data["run_status"] = "COMPLETED"
            # Parsing help is not an evaluated or certifying audit.
            run.data["invocation_outcome"] = "HELP" if any(
                value in {"-h", "--help"} for value in values) else "EXIT_ZERO"
            exit_code = 0
        else:
            run.failure(error, "primary")
            run.data["run_status"] = "ERROR"
            exit_code = 2
    except BaseException as error:
        run.failure(error, "primary")
        if isinstance(error, AssertionError):
            run.data["result_status"] = "FAIL"
        run.data["run_status"] = "COMPLETED" if isinstance(error, AssertionError) else "ERROR"
        exit_code = 1 if isinstance(error, AssertionError) else 2
    finally:
        for callback in reversed(run.cleanups):
            try:
                callback()
            except BaseException as error:
                run.failure(error, "cleanup")
                run.data["run_status"] = "ERROR"
                exit_code = 2
        try:
            folder = run.data.get("output_directory")
            if folder:
                for path in sorted(Path(folder).rglob("*")):
                    if path.is_file():
                        run.identity("outputs", path.relative_to(folder).as_posix(), path)
            if (run.data.get("require_expected_outputs") is True
                    and run.data["run_status"] != "ERROR"
                    and run.data["outputs"] != run.data.get("expected_outputs")):
                raise ValueError("Final output inventory differs from the expected run/artifact identities.")
        except Exception as error:
            run.failure(error, "output-identity")
            run.data["run_status"] = "ERROR"
            exit_code = 2
        if run.data["run_status"] == "RUNNING":
            run.data["run_status"] = "COMPLETED"
        if run.data["run_status"] == "ERROR":
            run.data["status"] = "ERROR"
        elif run.data.get("invocation_outcome") in {"HELP", "EXIT_ZERO"}:
            run.data["status"] = run.data["invocation_outcome"]
        elif run.data["result_status"] == "PASS":
            run.data["status"] = "PASS"
        elif tool == "extraction":
            run.data["status"] = "GAPS_FOUND"
        else:
            run.data["status"] = run.data["result_status"]
        run.data["ended_utc"] = utc()
        try:
            run.write()
        except Exception as error:
            run.failure(error, "reporting")
            run.data.update(run_status="ERROR", status="ERROR")
            exit_code = 2
            # A latest-view failure may leave per-run storage writable. Preserve
            # the final technical error there instead of leaving a PASS behind.
            try:
                atomic_json(run.path, run.data)
            except Exception as recovery:
                run.failure(recovery, "reporting-recovery")
        print(json.dumps({k: v for k, v in run.data.items() if k not in {"cases", "source_files"}},
                         ensure_ascii=False, indent=2))
    return exit_code


def certify(report, run_id, artifact_sha256):
    """A latest-file PASS alone never certifies an artifact."""
    return (report.get("report_version") == 1 and report.get("run_id") == run_id
            and report.get("status") == "PASS"
            and report.get("run_status") == "COMPLETED" and report.get("ended_utc")
            and report.get("result_status") == "PASS" and not report.get("failures")
            and report.get("artifacts", {}).get("executable", {}).get("sha256") == artifact_sha256)


def browser_setup(run, argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, default=run.latest.parent)
    parser.add_argument("--run-id", type=str)
    args = parser.parse_args(argv)
    if args.run_id:
        uuid.UUID(args.run_id)
    run.relocate(args.report_dir / "report.json")
    audit = args.report_dir / "work" / run.data["run_id"]
    audit.mkdir(parents=True, exist_ok=False)
    run.data["output_directory"] = str(audit.resolve())
    return audit
