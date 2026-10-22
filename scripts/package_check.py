"""Audit the built executable with Python and developer tools absent from PATH."""

import argparse
import hashlib
import io
import json
import os
import queue
import subprocess
import threading
import time
import uuid
import zipfile
from xml.etree import ElementTree
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from scripts.audit_reporting import audit_main
EXECUTABLE = ROOT / "dist/HearingCompilerPrototype.exe"


def body(run, argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, default=EXECUTABLE)
    parser.add_argument("--report-dir", type=Path, default=ROOT / ".scratch/package-audit")
    parser.add_argument("--run-id", type=str)
    args = parser.parse_args(argv)
    executable = args.executable.resolve()
    if args.run_id:
        uuid.UUID(args.run_id)
    run.relocate(args.report_dir / "report.json")
    if not executable.is_file():
        parser.error(f"Executable not found: {executable}")
    audit = args.report_dir.resolve() / "work" / run.data["run_id"]
    audit.mkdir(parents=True, exist_ok=True)
    state = audit / f"state-{uuid.uuid4().hex[:8]}"
    environment = os.environ.copy()
    system_root = environment.get("SystemRoot", "C:/Windows")
    environment["PATH"] = os.pathsep.join([system_root, str(Path(system_root) / "System32")])
    environment.pop("PYTHONPATH", None)
    checks = []
    run.data["checks"] = checks
    artifact = run.identity("artifacts", "executable", executable)
    run.identity("inputs", "transcript", ROOT / "fixtures/transcripts/sample-hearing.json")
    run.data.update(executable=str(executable), executable_sha256=artifact["sha256"],
                    executable_bytes=artifact["bytes"], python_on_path=False)
    processes = []

    def start():
        process = subprocess.Popen(
            [str(executable), "serve", "--port", "0", "--state", str(state)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=environment, cwd=audit,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        processes.append(process)
        output = queue.Queue()

        def reader():
            for line in process.stdout:
                output.put(line)

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.monotonic() + 30
        lines = []
        while time.monotonic() < deadline:
            try:
                line = output.get(timeout=.1)
                lines.append(line.strip())
                if line.startswith("Hearing Compiler: "):
                    return process, line.split(": ", 1)[1].strip()
            except queue.Empty:
                if process.poll() is not None:
                    raise RuntimeError(f"Executable exited: {lines}")
        process.terminate()
        raise RuntimeError(f"Executable startup timed out: {lines}")

    def fetch(address, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Review-Token"] = token
        with urlopen(Request(address + path, data=json.dumps(body).encode() if body is not None else None, headers=headers), timeout=10) as response:
            raw = response.read()
            binary_export = path.startswith("/api/export/") and "format=docx" in path
            return json.loads(raw) if path.startswith("/api") and not binary_export else raw

    process = None
    def cleanup():
        for child in processes:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                   capture_output=True, check=True)
                    child.wait(timeout=10)
            if child.stdout:
                child.stdout.close()
    run.cleanup(cleanup)
    try:
        process, address = start()
        checks.append("Executable starts without Python or developer tools on PATH")
        for path, content in [("/", b"Hearing Compiler"), ("/app.js", b"saveChanges"),
                              ("/styles.css", b'--teal'), ("/theme.js", b"hearing-compiler-theme")]:
            assert content in fetch(address, path), path
        hearings = fetch(address, "/api/hearings")
        assert hearings == []
        checks.append("Bundled interface opens an empty workspace with no sample hearings")
        token = fetch(address, "/api/session")["token"]
        transcript = json.loads((ROOT / "fixtures/transcripts/sample-hearing.json").read_text(encoding="utf-8"))
        transcript["hearing"]["id"] = "package-check-import"
        fetch(address, "/api/import", {"documents": [transcript]}, token)
        job = fetch(address, "/api/hearings/package-check-import")
        saved = fetch(address, "/api/review", {
            "id": job["id"], "revision": job["revision"],
            "changes": [{"path": "/questions/0/asker", "value": "Rep. Avery", "review": "APPROVED"}],
        }, token)
        assert "Rep. Avery" in saved["markdown"]
        repaired_argument = "A missing argument added through the delivered application."
        saved = fetch(address, "/api/review", {
            "id": job["id"], "revision": saved["revision"],
            "operations": [{"op": "add_argument", "testimony": 0, "statement": 0,
                            "value": repaired_argument, "evidence": ["s02"]}],
        }, token)
        payload = fetch(address, f'/api/export/{job["id"]}?format=docx&revision={saved["revision"]}')
        with zipfile.ZipFile(io.BytesIO(payload)) as document:
            note = ElementTree.fromstring(document.read("word/document.xml"))
            text = "\n".join(note.itertext())
        assert repaired_argument in text and "Rep. Avery" in text
        assert "[draft; review needed]" in text
        checks.append("Bundled Word dependency exports repaired content without installed Python")
        archive = fetch(address, f'/api/export/{job["id"]}?format=json&revision={saved["revision"]}')
        assert archive["job"]["draft"] == saved["draft"]
        assert archive["recording_included"] is False
        checks.append("Delivered app exports transcript, review state and structural history as saved data")
        fetch(address, "/api/shutdown", {}, token)
        process.wait(timeout=10)
        process, address = start()
        restored = fetch(address, "/api/hearings/package-check-import")
        assert restored["draft"]["questions"][0]["asker"]["value"] == "Rep. Avery"
        assert repaired_argument in restored["markdown"]
        assert restored["draft"] == saved["draft"]
        checks.append("Executable restart restores the saved correction")
        assert len(fetch(address, "/api/hearings")) == 1
        checks.append("Restart preserves imported work without injecting demo rows")
        token = fetch(address, "/api/session")["token"]
        scripted_text = "I am Mira Vale. I support H.9990.\nThe renewal notice should show the due date."
        text_job = fetch(address, "/api/text", {
            "text": scripted_text, "filename": "package-text.txt", "synthetic": True,
            "hearing": {"id": "package-text-check", "title": "Fictional packaged text check",
                        "date": "2026-10-06", "committee": "Example Committee",
                        "source_file": "package-text.txt"},
        }, token)
        assert text_job["processing"] == "Complete"
        text_saved = fetch(address, "/api/hearings/package-text-check")
        assert text_saved["input"]["source_text"] == scripted_text
        assert all(s["start"] is None and s["end"] is None for s in text_saved["draft"]["segments"])
        assert "Text line 1" in text_saved["markdown"] and "00:00:00" not in text_saved["markdown"]
        text_archive = fetch(address, "/api/export/package-text-check?format=json")
        assert text_archive["job"]["input"]["source_text"] == scripted_text
        checks.append("Bundled TXT route preserves original text, null timing and line-based evidence without installed Python")
        subtitle = "1\n00:00:01,250 --> 00:00:05,000\nI am Lila Birch. I support H.9988.\n"
        subtitle_job = fetch(address, "/api/text", {
            "text": subtitle, "filename": "package-subtitle.srt", "synthetic": True,
            "hearing": {"id": "package-subtitle", "title": "Fictional packaged subtitle check",
                        "date": "2026-10-07", "committee": "Example Committee",
                        "source_file": "package-subtitle.srt"},
        }, token)
        assert subtitle_job["processing"] == "Complete"
        assert fetch(address, "/api/hearings/package-subtitle")["draft"]["segments"][0]["start"] == 1.25
        assert fetch(address, "/rich.js").decode("utf-8").startswith('"use strict";')
        assert b"reviewFocusInventory" in fetch(address, "/review_focus.js")
        checks.append("Bundled timed subtitle adapter and rich review assets are available without Python on PATH")
        # Local Ollama drafting was removed on October 13; Gemini needs a real key, so only
        # the packaged settings surface is checked here, without any network call.
        drafting = fetch(address, "/api/extraction")
        assert drafting["provider"] == "rules" and drafting["models"] == ["gemini-3.8-flash"]
        assert "api_key" not in drafting and drafting["key_saved"] in (True, False)
        checks.append("Packaged drafting settings offer the rule baseline and Gemini without exposing any key")
        from http.cookies import SimpleCookie
        prepare_request = Request(address + "/api/workspace/backup-prepare", data=b"{}",
                                  headers={"X-Review-Token": token, "Content-Type": "application/json"})
        with urlopen(prepare_request, timeout=30) as response:
            prepared = json.loads(response.read())
            cookie = SimpleCookie(response.headers["Set-Cookie"])["hearing_backup_download"]
            assert cookie["httponly"] and cookie["samesite"] == "Strict"
            assert prepared["download_url"] == "/api/workspace/backup-download"
        native_request = Request(address + prepared["download_url"],
                                 headers={"Cookie": f"hearing_backup_download={cookie.value}"})
        with urlopen(native_request, timeout=30) as response:
            native_zip = response.read()
            assert "attachment" in response.headers["Content-Disposition"]
            assert len(native_zip) == int(response.headers["Content-Length"])
        with zipfile.ZipFile(io.BytesIO(native_zip)) as archive:
            native_state = json.loads(archive.read("state.json"))
        assert native_state["jobs"]["eval-01"]["draft"] == rich_saved["draft"]
        checks.append("Packaged native workspace download authorizes one verified ZIP without browser blob buffering")
        # Full backup commands must use only the bundled executable. Rehearse
        # them after shutdown so the package owns the saved workspace lease.
        fetch(address, "/api/shutdown", {}, token)
        process.wait(timeout=10)
        archive_file = audit / "full-package-backup.zip"
        restored_state = audit / "restored-package-state"
        for arguments in (
            ["backup", "--state", str(state), "--output", str(archive_file)],
            ["restore", str(archive_file), "--state", str(restored_state)],
        ):
            completed = subprocess.run([str(executable), *arguments], cwd=audit, env=environment,
                                       capture_output=True, text=True, timeout=60,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert completed.returncode == 0, completed.stderr
        restored_json = json.loads((restored_state / "state.json").read_text(encoding="utf-8"))
        assert restored_json["jobs"]["eval-01"]["draft"] == rich_saved["draft"]
        assert restored_json["jobs"]["package-check-import"]["draft"] == saved["draft"]
        checks.append("Bundled full-backup and empty-workspace restore commands preserve legacy and rich review without Python")
        assert hashlib.sha256(executable.read_bytes()).hexdigest() == artifact["sha256"], "Executable changed during audit."
    finally:
        if process and process.poll() is None:
            try:
                token = fetch(address, "/api/session")["token"]
                fetch(address, "/api/shutdown", {}, token)
                process.wait(timeout=10)
            except Exception as error:
                run.failure(error, "graceful-shutdown")
                raise


def main(argv=None):
    return audit_main("package", body, argv, latest=ROOT / ".scratch/package-audit/report.json")


if __name__ == "__main__":
    raise SystemExit(main())
