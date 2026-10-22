"""App version shown in the window. Packaged builds add a build stamp."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import subprocess

VERSION = "0.1.0"
STAMP = Path(__file__).resolve().parent / "build-stamp.json"


@lru_cache(maxsize=1)
def info() -> dict:
    try:
        stamp = json.loads(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stamp = {}
    commit, build = stamp.get("commit"), stamp.get("build")
    if not stamp:
        try:
            commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=STAMP.parent,
                                    capture_output=True, text=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            commit = None
    label = f"v{VERSION}" + (f" · build {build}" if build else "") + (f" · {commit}" if commit else "")
    return {"version": VERSION, "build": build, "commit": commit, "packaged": bool(stamp),
            "label": label if stamp else f"{label} (source)"}


def write_stamp(folder: Path, commit: str, build: str | None) -> Path:
    """Build scripts bundle this file next to the package modules."""
    path = folder / "build-stamp.json"
    path.write_text(json.dumps({"commit": commit[:7], "build": int(build) if build else None}) + "\n",
                    encoding="utf-8")
    return path
