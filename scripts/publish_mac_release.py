"""Publish a verified Mac build to the public releases repository as a pre-release.

Run by CI after build_macos.py succeeds. Needs GH_TOKEN with contents write
access to RELEASES_REPO only. Installed apps follow only the *latest* release,
so nothing reaches users until a person promotes the build (promote command
below, or the "Release Mac app to installed Macs" workflow). Keeps the KEEP highest
build numbers and never deletes the current latest or the build just published.

    python scripts/publish_mac_release.py promote <build>
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hearing_compiler.updater import ZIP_NAME, parse_manifest
from hearing_compiler.version import VERSION
from scripts import mac_signature_check

KEEP = 10


def manifest(archive: Path, build: int, commit: str) -> dict:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return parse_manifest({"version": VERSION, "build": build, "commit": commit[:7], "tag": f"build-{build}",
                           "zip": ZIP_NAME, "sha256": digest, "bytes": archive.stat().st_size,
                           "published_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def gh(*args: str, capture: bool = False) -> str:
    result = subprocess.run(["gh", *args], check=True, text=True, capture_output=capture)
    return result.stdout if capture else ""


def releases(repo: str) -> list[dict]:
    return json.loads(gh("release", "list", "--repo", repo, "--limit", "100",
                         "--json", "tagName,isLatest,isPrerelease", capture=True))


def build_number(release: dict) -> int | None:
    tag = release["tagName"]
    return int(tag[6:]) if tag.startswith("build-") and tag[6:].isdigit() else None


def published_builds(listed: list[dict]) -> list[int]:
    return [b for b in map(build_number, listed) if b is not None]


def opens_on_a_mac(repo: str, build: int) -> None:
    """Refuse a build whose ZIP fails mac_signature_check (builds 17-20 do): a normal Mac wouldn't open it."""
    with tempfile.TemporaryDirectory() as folder:
        gh("release", "download", f"build-{build}", "--repo", repo, "--pattern", ZIP_NAME, "--dir", folder)
        if mac_signature_check.main([str(Path(folder) / ZIP_NAME)]):
            raise SystemExit(f"build-{build} failed the signature check above, so it was not released.")


def promote(repo: str, build: int | None = None) -> int:
    """Make a tested pre-release the latest; with no number, the newest one newer than latest."""
    listed = releases(repo)
    if build is None:
        current = max(published_builds([r for r in listed if r.get("isLatest")]) or [0])
        newer = [b for b in published_builds([r for r in listed if r.get("isPrerelease")]) if b > current]
        if not newer:
            raise SystemExit(f"Nothing newer than build {current} is waiting to be released.")
        build = max(newer)
    if f"build-{build}" not in {r["tagName"] for r in listed}:
        raise SystemExit(f"build-{build} is not published.")
    opens_on_a_mac(repo, build)
    gh("release", "edit", f"build-{build}", "--repo", repo, "--prerelease=false", "--latest")
    print(f"Promoted build-{build}; installed apps will update to it when next opened.")
    print(f"PROMOTED={build}")
    return build


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    repo = os.environ["RELEASES_REPO"]
    if argv[:1] == ["promote"]:
        promote(repo, int(argv[1]) if len(argv) > 1 and argv[1] else None)
        return
    archive = ROOT / "dist/macOS" / ZIP_NAME
    build = int(os.environ["GITHUB_RUN_NUMBER"])
    existing = releases(repo)
    # A reset run counter (renamed workflow, moved repo) would silently stop all updates.
    if published_builds(existing) and build <= max(published_builds(existing)):
        raise SystemExit(f"Build {build} is not newer than published build {max(published_builds(existing))}; "
                         "nothing was published. Fix the build numbering before releasing.")
    described = manifest(archive, build, os.environ["GITHUB_SHA"])
    folder = archive.parent / "publish"
    folder.mkdir(exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(described, indent=2) + "\n", encoding="utf-8")
    title = f"v{described['version']} · build {described['build']} · {described['commit']}"
    # Promotion keeps these notes, so they must stay true on the latest release; GitHub's badges show the state.
    notes = (f"Apple Silicon Mac build. To install, download {ZIP_NAME} under Assets (not Source code) and "
             f"follow the first-install steps at https://github.com/{repo}#first-install. Installed apps update "
             "themselves only to the release marked Latest.")
    gh("release", "create", described["tag"], str(archive), str(folder / "manifest.json"),
       "--repo", repo, "--title", title, "--notes", notes, "--prerelease")
    # GitHub dates a release by its tag's commit, which these releases share, so createdAt can't order them.
    listed = sorted((r for r in releases(repo) if build_number(r) is not None), key=build_number, reverse=True)
    for old in listed[KEEP:]:
        if not old.get("isLatest") and old["tagName"] != described["tag"]:
            gh("release", "delete", old["tagName"], "--repo", repo, "--cleanup-tag", "--yes")
    print(f"Published pre-release {title}. Promote it to send it to installed apps.")


if __name__ == "__main__":
    main()
