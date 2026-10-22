"""Build and audit an isolated candidate before replacing the local Windows bundle."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.audit_reporting import certify
FILES = ("HearingCompilerPrototype.exe", "HearingCompilerPrototype-Windows.zip",
         "build-info.json", "build-report.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def within(path, workspace):
    path, workspace = path.resolve(), workspace.resolve()
    if not path.is_relative_to(workspace) or path == workspace:
        raise ValueError("Build paths must stay below the intended workspace.")
    return path


def source_snapshot():
    paths = ([ROOT / "launch_prototype.py", ROOT / "requirements.txt",
              ROOT / "scripts/build_windows.py", ROOT / "scripts/package_check.py",
              ROOT / "scripts/audit_reporting.py",
              ROOT / "docs/WINDOWS_QUICK_START.txt"]
             + sorted((ROOT / "hearing_compiler").rglob("*.py"))
             + sorted(p for p in (ROOT / "web").rglob("*") if p.is_file()))
    files = {p.relative_to(ROOT).as_posix(): digest(p) for p in paths}
    return files, hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def promote(candidate, destination, backup, workspace=ROOT):
    """Verify audit identity before any replacement; restore all earlier files on failure."""
    candidate, destination, backup = [within(p, workspace) for p in (candidate, destination, backup)]
    report = json.loads((candidate / "build-report.json").read_text(encoding="utf-8"))
    package_report = json.loads((candidate / "package-audit/report.json").read_text(encoding="utf-8"))
    if (report.get("status") != "PASS" or report.get("package_process_exit") != 0
            or not certify(package_report, report.get("package_run_id"), report.get("executable_sha256"))):
        raise ValueError("A failed or missing package audit cannot be promoted.")
    for name, expected in [
        (FILES[0], report["executable_sha256"]), (FILES[1], report["zip_sha256"]),
        (FILES[2], report["build_info_sha256"]),
    ]:
        if digest(candidate / name) != expected:
            raise ValueError("Candidate artifact changed after validation.")
    if package_report["executable_sha256"] != report["executable_sha256"]:
        raise ValueError("Package audit belongs to a different executable.")
    if report.get("working_tree_dirty") is not False:
        raise ValueError("Promotion requires a reviewed clean source checkpoint.")
    backup.mkdir(parents=True, exist_ok=False)
    destination.mkdir(parents=True, exist_ok=True)
    originals = {name: (destination / name).exists() for name in FILES}
    for name, existed in originals.items():
        if existed:
            shutil.copy2(destination / name, backup / name)
    changed = []
    try:
        for name in FILES:
            temporary = destination / (name + ".candidate.tmp")
            shutil.copy2(candidate / name, temporary)
            os.replace(temporary, destination / name)
            changed.append(name)
    except Exception:
        for name in changed:
            if originals[name]:
                shutil.copy2(backup / name, destination / name)
            else:
                (destination / name).unlink()
        raise
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-promote", action="store_true", help="Keep the verified candidate without changing dist.")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("This builder targets Windows.")
    from PyInstaller.__main__ import run
    from PyInstaller.archive.readers import CArchiveReader
    from docx import Document  # Fail before building if Word's runtime requirement is missing.
    del Document

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    staging = within(ROOT / ".scratch/windows-build" / run_id, ROOT)
    candidate = staging / "candidate"
    candidate.mkdir(parents=True)
    source_files, snapshot_hash = source_snapshot()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    if dirty:
        raise RuntimeError("Commit/review source changes before building; working tree must be clean.")
    from hearing_compiler.version import write_stamp
    stamp = write_stamp(staging, revision, os.environ.get("GITHUB_RUN_NUMBER"))
    run([
        "--noconfirm", "--clean", "--onefile", "--console", "--name", "HearingCompilerPrototype",
        "--distpath", str(candidate), "--workpath", str(staging / "work"), "--specpath", str(staging),
        "--paths", str(ROOT), "--collect-all", "docx", "--add-data", f"{ROOT / 'web'};web",
        "--add-data", f"{stamp};hearing_compiler",
        str(ROOT / "launch_prototype.py"),
    ])
    executable = candidate / FILES[0]
    members = list(CArchiveReader(str(executable)).toc)
    forbidden = [name for name in members if
                 name.replace("\\", "/").lower().startswith(("fixtures/", "tests/"))
                 or name.lower().endswith((".mp3", ".wav", ".m4a"))]
    if forbidden:
        raise RuntimeError(f"Developer media/fixtures were bundled: {forbidden}")
    info = {
        "source_commit": revision, "working_tree_dirty": dirty, "source_snapshot_sha256": snapshot_hash,
        "source_files": source_files, "python": sys.version.split()[0],
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "executable_sha256": digest(executable), "executable_bytes": executable.stat().st_size,
        "developer_fixtures_bundled": False,
    }
    (candidate / FILES[2]).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    with zipfile.ZipFile(candidate / FILES[1], "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(executable, FILES[0])
        bundle.write(ROOT / "docs/WINDOWS_QUICK_START.txt", "README.txt")
        bundle.write(candidate / FILES[2], FILES[2])
    with zipfile.ZipFile(candidate / FILES[1]) as bundle:
        if set(bundle.namelist()) != {FILES[0], "README.txt", FILES[2]} or bundle.read(FILES[0]) != executable.read_bytes():
            raise RuntimeError("ZIP inventory or executable identity differs from candidate.")
    package_run_id = str(uuid.uuid4())
    audit_process = subprocess.run(
        [sys.executable, str(ROOT / "scripts/package_check.py"), "--executable", str(executable),
         "--report-dir", str(candidate / "package-audit"), "--run-id", package_run_id], cwd=ROOT,
    )
    if audit_process.returncode != 0:
        raise RuntimeError("Package process failed; candidate cannot be promoted.")
    if source_snapshot()[1] != snapshot_hash:
        raise RuntimeError("Build inputs changed during verification; candidate will not be promoted.")
    package = json.loads((candidate / "package-audit/report.json").read_text(encoding="utf-8"))
    if not certify(package, package_run_id, digest(executable)):
        raise RuntimeError("Final package report does not certify this run and executable.")
    if bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()):
        raise RuntimeError("Source changed during verification.")
    report = {"status": "PASS", "candidate": candidate.relative_to(ROOT).as_posix(),
              "source_commit": revision, "working_tree_dirty": dirty, "source_snapshot_sha256": snapshot_hash,
              "executable_sha256": digest(executable), "executable_bytes": executable.stat().st_size,
              "zip_sha256": digest(candidate / FILES[1]), "zip_bytes": (candidate / FILES[1]).stat().st_size,
              "build_info_sha256": digest(candidate / FILES[2]),
              "package_run_id": package_run_id, "package_process_exit": audit_process.returncode,
              "package_checks": package["checks"], "fixture_inventory": "No developer fixtures or recordings bundled."}
    (candidate / FILES[3]).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not args.no_promote:
        saved = promote(candidate, ROOT / "dist", ROOT / ".scratch/windows-build-backups" / run_id)
        report["previous_bundle_backup"] = saved.relative_to(ROOT).as_posix()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
