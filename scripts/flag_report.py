"""How her ⚠ fared: precision per kind of flag, and the shadow rules' would-clear results, from saved state.

    python scripts/flag_report.py path/to/saved-state
    python scripts/flag_report.py path/to/state.json

Reads a workspace's state.json (from issue #3's saved files). Local only; nothing is sent anywhere. A flag counts as
"fixed" when she changed the item's value, and as "kept" when she approved it unchanged. Kept doesn't prove the item
was right: she approves unflagged errors too, which is what the recall spot-check is for. See docs/PROJECT_PLAN.md,
"Quality targets".
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hearing_compiler import extraction, roster  # noqa: E402
from hearing_compiler.rich_review import AGENDA_TITLE, NOT_ON_AGENDA, TITLE_CHANGED  # noqa: E402
MULTI_BILL = extraction.MULTI_BILL

KINDS = (  # (kind, test on the flag's lead sentence); the first match wins, code-made flags first.
    ("second check", lambda f, p: f.startswith("Second check:")),
    ("audio check", lambda f, p: f.startswith("Audio check:")),
    ("legislator name check", lambda f, p: "member list" in f),
    ("bill from the first draft", lambda f, p: f.startswith(extraction.REDRAFT_BILL[:30])),
    ("joined across a cut", lambda f, p: f.startswith(extraction.JOINED[:30])),
    ("questioner inferred", lambda f, p: "who asked" in f.lower() or f.startswith(extraction.ASKED_NAME[:30])),
    ("respondent inferred", lambda f, p: f.startswith(extraction.ANSWERED[:30])),
    ("bill not on agenda", lambda f, p: f.startswith(NOT_ON_AGENDA[:30])),
    ("several bills", lambda f, p: f.startswith(MULTI_BILL[:30]) or "also mentions" in f),
    ("title from agenda", lambda f, p: f.startswith(AGENDA_TITLE[:30]) or f.startswith(TITLE_CHANGED[:30])),
    ("quote not found", lambda f, p: "quote not found" in f or "quote wasn't found" in f),
    ("figure not in quote", lambda f, p: "figure" in f.lower()),
    ("name spelling", lambda f, p: p.endswith("/name") and roster.SPELLING.search(f)),
)


def kind(path: str, flag: str) -> str:
    if path.startswith("/issues/"):
        return "hearing-wide check"
    for name, test in KINDS:
        if test(flag, path):
            return name
    field = re.sub(r"/\d+", "", path).strip("/").split("/")[-1]
    return f"other ({field})"


def outcomes(draft: dict) -> dict:
    """path -> (field as drafted, field after her last change) for every field she touched."""
    found = {}
    for entry in draft.get("review_log") or []:
        path = entry.get("path")
        if isinstance(path, str) and isinstance(entry.get("before"), dict) and isinstance(entry.get("after"), dict):
            drafted = found[path][0] if path in found else entry["before"]
            found[path] = (drafted, entry["after"])
    return found


def report(job: dict) -> list[str]:
    draft = job.get("draft") or {}
    touched = outcomes(draft)
    kinds, shadow = defaultdict(lambda: [0, 0]), {"right": 0, "wrong": 0}
    for path, (drafted, after) in touched.items():
        changed = drafted.get("value") != after.get("value")
        if drafted.get("flag"):
            counts = kinds[kind(path, drafted["flag"])]
            counts[0] += 1
            counts[1] += changed
        if drafted.get("would_clear"):
            shadow["wrong" if changed else "right"] += 1
    flagged = sum(n for n, _ in kinds.values())
    fixed = sum(f for _, f in kinds.values())
    ends = [s.get("end") for s in draft.get("segments") or [] if isinstance(s.get("end"), (int, float))]
    hours = max(ends) / 3600 if ends else None
    marked = (draft.get("roster") or {}).get("would_clear")
    lines = [f"{job.get('hearing', {}).get('title', job.get('id'))} ({job.get('processing')})",
             f"  reviewed ⚠: {flagged}, fixed {fixed}" + (f" ({fixed / flagged:.0%} precision)" if flagged else "")
             + (f"; {flagged / hours:.0f} ⚠ per hearing hour" if hours and flagged else "")]
    for name, (count, changed) in sorted(kinds.items(), key=lambda item: -item[1][0]):
        lines.append(f"    {name:28} {count:4} ⚠  {changed:3} fixed  {changed / count:4.0%}")
    if marked is not None:
        lines.append(f"  shadow rule ({roster.SHADOW_RULE}): marked {marked}; reviewed {shadow['right'] + shadow['wrong']}: "
                     f"{shadow['right']} kept, {shadow['wrong']} EDITED (a wrong clear)")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("state", help="a state.json, or the folder holding it")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # "⚠" on a Windows console.
    path = Path(args.state)
    path = path / "state.json" if path.is_dir() else path
    state = json.loads(path.read_text(encoding="utf-8"))
    jobs = sorted(state.get("jobs", {}).values(), key=lambda j: (j.get("hearing") or {}).get("title", ""))
    for job in jobs:
        if job.get("draft"):
            print("\n".join(report(job)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
