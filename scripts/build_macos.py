"""Build a native Apple Silicon .app and ZIP; audit before exposing deliverables."""
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hardened_without_team(shown):
    """True when `codesign -d --verbose=2` output shows the hardened runtime but no Team ID."""
    flags = re.search(r"\bflags=0x([0-9a-fA-F]+)", shown)
    team = re.search(r"^TeamIdentifier=(.+)$", shown, re.MULTILINE)
    return bool(flags and int(flags.group(1), 16) & 0x10000) and (not team or team.group(1).strip() == "not set")


def sign_release(app, identity):
    """Sign the app and all nested code with the pinned identity, without the hardened runtime.

    The hardened runtime turns on library validation, which loads only code with the program's
    Team ID. A self-signed certificate has none, so the app could not load its own Python."""
    if identity:
        subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--timestamp", "-s", identity, str(app)], check=True)
    framework = app / "Contents/Frameworks/Python.framework"
    for code in [app] + ([framework] if framework.exists() else []):
        shown = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=2", str(code)],
                               capture_output=True, text=True, check=True).stderr
        if hardened_without_team(shown):
            raise RuntimeError(f"{code.name} has the hardened runtime but no Team ID, so it could not "
                               "load its own libraries on a Mac. Sign it without --options runtime.")


def source_files():
    paths = [ROOT / "launch_macos.py", ROOT / "requirements.txt",
             ROOT / "requirements-build.txt", ROOT / "scripts/build_macos.py",
             ROOT / "scripts/macos_package_check.py", ROOT / "scripts/audit_reporting.py",
             ROOT / "docs/MAC_QUICK_START.txt"]
    paths += sorted((ROOT / "hearing_compiler").rglob("*.py"))
    paths += sorted(p for folder in ("web",) for p in (ROOT / folder).rglob("*") if p.is_file())
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in paths}


def bundle_inventory(app):
    """Bind every regular member and contained link without traversing links."""
    app = app.resolve()
    result = {}
    for path in sorted(app.rglob("*")):
        name = path.relative_to(app).as_posix()
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(app):
                raise ValueError("Bundle link escapes the app.")
            result[name] = {"link": os.readlink(path)}
        elif path.is_file():
            result[name] = {"sha256": digest(path), "mode": path.stat().st_mode & 0o777}
    return result


def certify_app(report, run_id, executable, app, source, sources, inventory):
    from scripts.audit_reporting import certify
    if (not certify(report, run_id, digest(executable))
            or report.get("source") != source
            or report.get("build_source_files") != sources
            or report.get("bundle_files") != inventory
            or bundle_inventory(app) != inventory):
        raise RuntimeError("Package, full bundle or source identity does not match the audited build.")


def main():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("Build on an Apple Silicon macOS host with arm64 Python.")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise RuntimeError("Commit and review changes before building.")
    from PyInstaller.__main__ import run
    from scripts.audit_reporting import source_identity

    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source = source_identity()
    sources = source_files()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    staging = ROOT / ".scratch/macos-build" / run_id
    candidate = staging / "candidate"
    candidate.mkdir(parents=True)
    from hearing_compiler.version import write_stamp
    # Release builds are signed with the pinned identity that installed apps require for updates.
    identity = os.environ.get("MAC_SIGNING_IDENTITY", "").strip()
    stamp = write_stamp(staging, revision, os.environ.get("GITHUB_RUN_NUMBER"))
    # No --codesign-identity: PyInstaller would add the hardened runtime. It signs ad hoc;
    # sign_release then applies the release identity once.
    run(["--noconfirm", "--clean", "--onedir", "--windowed", "--name", "Hearing Compiler",
         "--target-architecture", "arm64", "--osx-bundle-identifier", "org.hearingcompiler.prototype",
         "--distpath", str(candidate), "--workpath", str(staging / "work"),
         "--specpath", str(staging), "--paths", str(ROOT), "--collect-all", "docx",
         "--add-data", f"{ROOT / 'web'}:web",
         "--add-data", f"{stamp}:hearing_compiler", str(ROOT / "launch_macos.py")])
    app = candidate / "Hearing Compiler.app"
    executable = app / "Contents/MacOS/Hearing Compiler"
    sign_release(app, identity)
    inventory = bundle_inventory(app)
    package_id = str(uuid.uuid4())
    subprocess.run([sys.executable, str(ROOT / "scripts/macos_package_check.py"),
                    "--executable", str(executable), "--report-dir", str(staging / "package-audit"),
                    "--run-id", package_id], cwd=ROOT, check=True)
    report = json.loads((staging / "package-audit/report.json").read_text())
    certify_app(report, package_id, executable, app, source, sources, inventory)
    if source_files() != sources or source_identity() != source:
        raise RuntimeError("Package or source identities changed; no ZIP will be delivered.")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise RuntimeError("Source changed during verification.")
    release = staging / "HearingCompilerPrototype-Mac-AppleSilicon"
    release.mkdir()
    shutil.copytree(app, release / app.name, symlinks=True)
    certify_app(report, package_id, release / app.name / "Contents/MacOS/Hearing Compiler",
                release / app.name, source, sources, inventory)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(release / app.name)], check=True)
    if identity:
        from hearing_compiler.updater import SIGNER_SHA1
        if identity.upper() != SIGNER_SHA1:
            raise RuntimeError("MAC_SIGNING_IDENTITY differs from the certificate installed apps trust.")
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                        f'-R=certificate leaf = H"{SIGNER_SHA1}"', str(release / app.name)], check=True)
    shutil.copy2(ROOT / "docs/MAC_QUICK_START.txt", release / "README.txt")
    info = {"source_commit": revision, "source": source, "source_files": sources,
            "bundle_files": inventory, "python": sys.version,
            "macos": platform.mac_ver()[0], "architecture": platform.machine(),
            "dependencies": {name: importlib.metadata.version(name) for name in ("pyinstaller", "python-docx")},
            "executable_sha256": digest(executable), "package_run_id": package_id,
            "signing": ("Self-signed release identity " + identity + ", no hardened runtime" if identity
                        else "PyInstaller ad-hoc") + "; no Developer ID signing or notarization",
            "limits": "CI synthetic package checks; recipient-device and real-hearing checks remain separate."}
    (release / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    archive = staging / "HearingCompilerPrototype-Mac-AppleSilicon.zip"
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                    str(release), str(archive)], check=True)
    decoded = staging / "verified-zip"
    subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(decoded)], check=True)
    decoded_app = decoded / release.name / app.name
    certify_app(report, package_id, decoded_app / "Contents/MacOS/Hearing Compiler",
                decoded_app, source, sources, inventory)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(decoded_app)], check=True)
    delivery = ROOT / "dist/macOS"
    delivery.mkdir(parents=True, exist_ok=True)
    # Preserve earlier downloads; each workflow run has isolated filesystem storage.
    if (delivery / archive.name).exists():
        raise RuntimeError("Mac ZIP already exists; preserve it before a new build.")
    shutil.copy2(archive, delivery / archive.name)
    info["zip_sha256"] = digest(archive)
    (delivery / "build-report.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
