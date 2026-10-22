"""Synthetic native Mac bundle audit in a disposable workspace."""
import argparse
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.audit_reporting import audit_main
from scripts.build_macos import bundle_inventory, source_files


def body(run, argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        raise RuntimeError("Native package verification requires macOS.")
    from docx import Document
    from docx.oxml.ns import qn
    executable = args.executable.resolve()
    run.relocate(args.report_dir / "report.json")
    run.identity("artifacts", "executable", executable)
    work = args.report_dir / "work" / run.data["run_id"]
    work.mkdir(parents=True, exist_ok=False)
    run.data["output_directory"] = str(work)
    run.data["limits"] = "Native CI package/CLI checks; Finder UI, Gatekeeper and recipient pilot pending."
    app = executable.parent.parent.parent
    inventory = bundle_inventory(app)
    sources = source_files()
    run.data["build_source_files"] = sources
    def verify_final_bundle():
        if bundle_inventory(app) != inventory or source_files() != sources:
            raise ValueError("Bundle or build source changed during audit/cleanup.")
        run.data["bundle_files"] = inventory
    # Registered first so this executes after all server cleanup callbacks.
    run.cleanup(verify_final_bundle)
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert plist["CFBundleExecutable"] == executable.name
    assert subprocess.check_output(["/usr/bin/lipo", "-archs", str(executable)], text=True).strip() == "arm64"
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    assert not [p for p in app.rglob("*") if p.name.lower().endswith((".mp3", ".wav", ".m4a"))
                or p.name in ("fixtures", "tests")]
    run.data["checks"].append("Apple Silicon app metadata, valid code signature and excluded-fixture inventory verified")
    environment = os.environ.copy()
    empty_path = work / "empty-path"
    empty_path.mkdir()
    environment["PATH"] = str(empty_path)
    environment.pop("PYTHONPATH", None)
    run.data["python_on_path"] = False
    state = work / "state"

    def fetch(address, path, value=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Review-Token"] = token
        data = None if value is None else json.dumps(value).encode()
        with urlopen(Request(address + path, data=data, headers=headers), timeout=10) as response:
            return response.read()

    def start():
        # --windowed binaries still support explicit developer CLI invocation.
        log_path = work / f"startup-{len(list(work.glob('startup-*')))}.log"
        log = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen([str(executable), "serve", "--state", str(state), "--port", "0"],
                                   cwd=work, env=environment, stdout=log, stderr=subprocess.STDOUT)
        address = None
        def stop():
            try:
                if process.poll() is None:
                    if address:
                        token = json.loads(fetch(address, "/api/session"))["token"]
                        fetch(address, "/api/shutdown", {}, token)
                    else:
                        process.terminate()
                    process.wait(timeout=15)
            finally:
                log.close()
        run.cleanup(stop)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for line in log_path.read_text().splitlines():
                if line.startswith("Hearing Compiler: "):
                    address = line.split(": ", 1)[1]
                    return address, process
            if process.poll() is not None:
                raise RuntimeError("Mac executable exited: " + log_path.read_text())
            time.sleep(.1)
        raise RuntimeError("Mac executable startup timed out.")

    address, process = start()
    assert json.loads(fetch(address, "/api/hearings")) == []
    for name in ("theme.js", "rich.js", "review_focus.js"):
        assert fetch(address, "/" + name) == (ROOT / "web" / name).read_bytes()
    run.data["checks"].append("Bundled executable starts empty without external Python and serves reviewed UI assets")
    token = json.loads(fetch(address, "/api/session"))["token"]
    document = json.loads((ROOT / "fixtures/transcripts/sample-hearing.json").read_text())
    document["hearing"]["id"] = "mac-package"
    fetch(address, "/api/import", {"documents": [document]}, token)
    job = json.loads(fetch(address, "/api/hearings/mac-package"))
    saved = json.loads(fetch(address, "/api/review", {
        "id": job["id"], "revision": job["revision"],
        "changes": [{"path": "/questions/0/asker", "value": "Rep. Native Check", "review": "APPROVED"}]}, token))
    word = fetch(address, "/api/export/mac-package?format=docx")
    (work / "export.docx").write_bytes(word)
    doc = Document(io.BytesIO(word))
    assert "Rep. Native Check" in "\n".join(p.text for p in doc.paragraphs)
    assert doc.styles["Title"]._element.get_or_add_pPr().find(qn("w:pBdr")) is None
    archive = json.loads(fetch(address, "/api/export/mac-package?format=json"))
    assert archive["job"]["draft"] == saved["draft"]
    run.data["checks"].append("Bundled Word dependency and JSON exports preserve saved review")
    fetch(address, "/api/shutdown", {}, token)
    process.wait(timeout=15)
    address, _ = start()
    restored = json.loads(fetch(address, "/api/hearings/mac-package"))
    assert restored["revision"] == saved["revision"]
    assert "Rep. Native Check" in restored["markdown"]
    run.data["checks"].append("Native shutdown and restart retain the exact saved correction")


if __name__ == "__main__":
    raise SystemExit(audit_main("macos-package", body, latest=ROOT / ".scratch/macos-package/report.json"))
