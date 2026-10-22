"""Self-update for the installed Mac app from the public releases repository.

Saved work lives in Application Support and is never touched. An update is
downloaded, hash-checked, signature-checked and staged first. A detached script
then swaps the app bundle only after this process exits, keeping the previous
bundle for rollback, and reopens the app.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from .pipeline import InputError
from .tls import context as tls_context
from .version import info as version_info

RELEASES = "https://github.com/mewzicians/hearing-compiler-releases/releases"
MANIFEST_URL = RELEASES + "/latest/download/manifest.json"
ZIP_NAME = "HearingCompilerPrototype-Mac-AppleSilicon.zip"
BUNDLE_ID = "org.hearingcompiler.prototype"
# SHA-1 of the release signing certificate. Updates signed by anyone else are refused.
SIGNER_SHA1 = "5985916D7FA9DCF67214E8538204703F1E1DC813"
MAX_ZIP_BYTES = 300 * 1024 * 1024
CHECK_SECONDS = 1800
BUSY = {"Transcribing", "Analyzing", "Waiting"}  # Never update with work queued.

_cache: dict = {}
_lock = threading.Lock()
_installing = threading.Lock()  # Held from a successful install until the app exits.


def current_app() -> Path | None:
    """The running .app bundle, or None for source and Windows runs."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    return next((p for p in Path(sys.executable).resolve().parents if p.suffix == ".app"), None)


def parse_manifest(data: dict) -> dict:
    if not (isinstance(data, dict) and type(data.get("build")) is int and data["build"] > 0
            and isinstance(data.get("version"), str) and re.fullmatch(r"\d+\.\d+\.\d+", data["version"])
            and isinstance(data.get("commit"), str) and re.fullmatch(r"[0-9a-f]{7,40}", data["commit"])
            and isinstance(data.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", data["sha256"])
            and type(data.get("bytes")) is int and 0 < data["bytes"] <= MAX_ZIP_BYTES
            and data.get("zip") == ZIP_NAME
            and isinstance(data.get("tag"), str) and re.fullmatch(r"build-\d+", data["tag"])):
        raise InputError("The published update description is malformed; no update was installed.")
    return data


def latest(force: bool = False) -> dict:
    with _lock:
        if not force and _cache.get("at", 0) > time.monotonic() - CHECK_SECONDS:
            return _cache["manifest"]
        with urllib.request.urlopen(MANIFEST_URL, timeout=15, context=tls_context()) as response:
            manifest = parse_manifest(json.loads(response.read(65536).decode("utf-8")))
        _cache.update(at=time.monotonic(), manifest=manifest)
        return manifest


def status(store, force: bool = False) -> dict:
    current = version_info()
    result = {"current": current["label"], "available": False, "can_install": False, "auto": False,
              "latest": None, "reason": None, "releases": RELEASES}
    app = current_app()
    if app is None:
        result["reason"] = "Automatic updates apply to the installed Mac app."
        return result
    try:
        manifest = latest(force)
    except InputError as error:
        result["reason"] = str(error)
        return result
    except (OSError, ValueError) as error:
        logging.warning("Update check failed: %r", error)
        result["reason"] = f"Could not check for updates ({getattr(error, 'reason', error)}). Saved work is unaffected."
        return result
    result["latest"] = f"v{manifest['version']} · build {manifest['build']} · {manifest['commit'][:7]}"
    if manifest["build"] <= (current["build"] or 0):
        return result
    result["available"] = True
    # Updating from the kept copy would delete that copy and put a false failure on the real app.
    if store.directory / "updates" in app.parents:
        result["reason"] = ("This is the previous version kept for rollback. Click Stop app, then open "
                            "Hearing Compiler from Applications.")
        return result
    if failed_marker(store.directory, manifest["build"]).exists():
        result["reason"] = (f"Build {manifest['build']} didn't start on this Mac, so the previous version was "
                            "put back. A fixed build will install automatically.")
        return result
    if blocked_marker(store.directory, manifest["build"]).exists():
        result["reason"] = ("macOS didn't let Hearing Compiler replace itself, so nothing changed. Install the new "
                            "version by hand (download it and choose Replace). Saved work is unaffected.")
        return result
    # Gatekeeper runs unmoved downloads from a read-only translocated copy.
    if "AppTranslocation" in app.parts or not os.access(app.parent, os.W_OK):
        result["reason"] = "Move Hearing Compiler into the Applications folder to install updates."
    elif any(job["processing"] in BUSY for job in store.list()):
        result["reason"] = "Hearings are still processing. The update will install the next time you open the app with nothing in progress."
    else:
        result["can_install"] = True
        # Install automatically once per build; a failed swap falls back to the button.
        result["auto"] = manifest["tag"] not in attempted(store)
    return result


def attempted(store) -> list[str]:
    try:
        return json.loads((store.directory / "updates/attempted.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def download(manifest: dict, target: Path):
    url = f"{RELEASES}/download/{manifest['tag']}/{ZIP_NAME}"
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=60, context=tls_context()) as response, target.open("xb") as output:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > manifest["bytes"]:
                raise InputError("The update download is larger than published; it was discarded.")
            digest.update(block)
            output.write(block)
    if size != manifest["bytes"] or digest.hexdigest() != manifest["sha256"]:
        raise InputError("The update download does not match its published fingerprint; it was discarded.")


def verify_bundle(app: Path, manifest: dict):
    """The staged app must be our signed bundle and carry the published build."""
    if not app.is_dir():
        raise InputError("The update package does not contain the app.")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict",
                    f'-R=certificate leaf = H"{SIGNER_SHA1}"', str(app)], check=True, capture_output=True, timeout=120)
    with (app / "Contents/Info.plist").open("rb") as handle:
        if plistlib.load(handle).get("CFBundleIdentifier") != BUNDLE_ID:
            raise InputError("The update package is not this app.")
    # PyInstaller places the stamp under Resources and links it from Frameworks.
    builds = {json.loads(p.read_text(encoding="utf-8")).get("build") for p in app.rglob("build-stamp.json")}
    if builds != {manifest["build"]}:
        raise InputError("The update package's build number differs from its published description.")


HEALTH_SECONDS = 90  # How long a new version gets to start before the old one is put back.


def health_marker(directory: Path, build: int) -> Path:
    """Written by a packaged app once it is serving, so an update can confirm it started."""
    return directory / "updates" / f"healthy-{build}"


def failed_marker(directory: Path, build: int) -> Path:
    return directory / "updates" / f"failed-{build}"


def blocked_marker(directory: Path, build: int) -> Path:
    """Written when macOS refused to move the installed app aside, so nothing was swapped."""
    return directory / "updates" / f"blocked-{build}"


def install_script(current: Path, staged: Path, previous: Path, pid: int, log: Path,
                   healthy: Path | None = None, failed: Path | None = None, blocked: Path | None = None) -> str:
    q = shlex.quote
    executable = current / "Contents" / "MacOS" / current.stem
    check = "" if healthy is None else f"""
rm -f {q(str(healthy))}
"""
    verify = "" if healthy is None else f"""
for i in $(seq 1 {HEALTH_SECONDS}); do
  if [ -e {q(str(healthy))} ]; then echo "update: new version started"; exit 0; fi
  sleep 1
done
echo "update: new version did not start; restoring the previous version"
pkill -f {q(str(executable))} 2>/dev/null
sleep 2
touch {q(str(failed))}
rm -rf {q(str(current))}.failed
if [ -d {q(str(previous))} ] && mv {q(str(current))} {q(str(current))}.failed && mv {q(str(previous))} {q(str(current))}; then
  echo "update: previous version restored"
  open {q(str(current))}
fi
"""
    # A refused first move changed nothing: reopen the same app, skipping the health check that would kill it.
    refused = "" if blocked is None else f"\n  touch {q(str(blocked))}"
    # Our download adds no quarantine; clear it only if present instead of rewriting every file's attributes.
    return f"""#!/bin/sh
exec >>{q(str(log))} 2>&1
echo "update: waiting for process {pid}"
for i in $(seq 1 240); do kill -0 {pid} 2>/dev/null || break; sleep 0.5; done
if kill -0 {pid} 2>/dev/null; then echo "update: app did not exit; nothing changed"; exit 1; fi{check}
rm -rf {q(str(previous))}
mkdir -p {q(str(previous.parent))}
if mv {q(str(current))} {q(str(previous))}; then
  if mv {q(str(staged))} {q(str(current))}; then
    echo "update: installed"
  else
    echo "update: install failed; restoring previous version"
    mv {q(str(previous))} {q(str(current))}
    open {q(str(current))}
    exit 1
  fi
else
  echo "update: could not move the current app aside; nothing changed"{refused}
  open {q(str(current))}
  exit 1
fi
if xattr -p com.apple.quarantine {q(str(current))} >/dev/null 2>&1; then
  xattr -dr com.apple.quarantine {q(str(current))} 2>/dev/null
fi
open {q(str(current))}{verify}"""


def install(store) -> dict:
    """Stage and verify the update, then start the swap. The caller stops the server.

    A reload during the download must not start a second install into the same folder."""
    if not _installing.acquire(blocking=False):
        raise InputError("The update is already downloading. Keep this tab open; the app restarts by itself.")
    try:
        return _stage_and_swap(store)
    except BaseException:
        _installing.release()
        raise


def _stage_and_swap(store) -> dict:
    state = status(store, force=True)
    if not state["can_install"]:
        raise InputError(state["reason"] or "This app is already up to date.")
    manifest, app = latest(), current_app()
    work = store.directory / "updates" / manifest["tag"]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        archive = work / ZIP_NAME
        download(manifest, archive)
        extracted = work / "extracted"
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(extracted)], check=True,
                       capture_output=True, timeout=300)
        staged = extracted / "HearingCompilerPrototype-Mac-AppleSilicon" / app.name
        verify_bundle(staged, manifest)
    except InputError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        shutil.rmtree(work, ignore_errors=True)
        raise InputError(f"The update could not be downloaded or verified, so nothing was changed: {error}") from error
    (store.directory / "updates/attempted.json").write_text(
        json.dumps(attempted(store) + [manifest["tag"]]), encoding="utf-8")
    script = work / "install.sh"
    script.write_text(install_script(app, staged, store.directory / "updates/previous" / app.name,
                                     os.getpid(), store.directory / "updates/update.log",
                                     health_marker(store.directory, manifest["build"]),
                                     failed_marker(store.directory, manifest["build"]),
                                     blocked_marker(store.directory, manifest["build"])), encoding="utf-8")
    subprocess.Popen(["/bin/sh", str(script)], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"installing": state["latest"]}
