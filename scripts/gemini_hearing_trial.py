"""One-off whole-hearing draft through the Gemini API, with source-citation checks.

Developer trial for public hearing text only. The drafting logic lives in
hearing_compiler/gemini.py, which the app uses too. Standard library only: in
Google Colab, upload that file next to this one. The API key is read from
GEMINI_API_KEY and is never printed or saved.

    python gemini_hearing_trial.py run --srt hearing.srt --agenda agenda.json --model gemini-3.8-flash --out results
    python gemini_hearing_trial.py check results/<run-folder> --srt hearing.srt   # recheck a saved draft
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time  # noqa: F401  (tests patch time.sleep through this module)
import urllib.request  # noqa: F401  (tests patch urlopen through this module)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from hearing_compiler import gemini as core
except ImportError:  # Colab: gemini.py uploaded next to this script.
    import gemini as core

RETRY_WAITS = core.RETRY_WAITS
clock, citable, items = core.clock, core.citable, core.items
mentioned_bills, merge_fragments, resolve = core.mentioned_bills, core.merge_fragments, core.resolve


def call_gemini(*args, **kwargs):
    try:
        return core.call_gemini(*args, **kwargs)
    except core.GeminiError as error:
        raise SystemExit(str(error)) from None


def check(draft: dict, cues: list[dict], agenda: dict) -> dict:
    return core.check(draft, cues, [b["bill"] for b in agenda["agenda"]])


def parse_srt(text: str) -> list[dict]:
    """Cue number, start/end seconds and text, numbered by position in the file."""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.lstrip("\ufeff").strip()):
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        match = re.match(r"\s*(\d+):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{3})", lines[timing])
        if not match:
            raise ValueError(f"Unreadable SRT timing line: {lines[timing]!r}")
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, match.groups())
        body = " ".join(line.strip() for line in lines[timing + 1:]).strip()
        if body:
            cues.append({"n": len(cues) + 1, "start": h1 * 3600 + m1 * 60 + s1 + ms1 / 1000,
                         "end": h2 * 3600 + m2 * 60 + s2 + ms2 / 1000, "text": body})
    if not cues:
        raise ValueError("The SRT contains no cues.")
    return cues


def render(draft: dict, cues: list[dict], agenda: dict, model: str) -> str:
    by_number = {c["n"]: c for c in cues}
    titles = {b["bill"]: b["title"] for b in agenda["agenda"]}

    def cite(item_cues, flag=None):
        valid = [n for n in item_cues or [] if n in by_number]
        stamp = f" [{clock(by_number[valid[0]]['start'])}]" if valid else ""
        return stamp + (f" ⚠ check: {flag}" if flag else "")

    hearing = agenda["hearing"]
    out = [f"# {hearing['title']}", "", f"{hearing['date']} · {hearing['committee']}", "",
           f"Tags: Hearing Notes; {hearing['committee']}", "",
           f"**AI draft ({model}) — not reviewed. Check every ⚠ item against the audio before use.**", ""]
    order, grouped = [], {}
    for entry in draft.get("entries") or []:
        key = entry.get("bill") or "Unknown bill"
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(entry)
    for key in order:
        first = grouped[key][0]
        title = titles.get(key) or first.get("bill_title") or ""
        out += [f"## Bill: {key}" + (f" — {title}" if title else "")
                + cite(first.get("bill_cues"), first.get("bill_check")), ""]
        for entry in grouped[key]:
            people = "; ".join(
                ", ".join(x for x in (p.get("name"), p.get("role"), p.get("organization")) if x)
                + cite(p.get("cues"), p.get("check")) for p in entry.get("testifiers") or [])
            out += [f"Testifiers: {people or 'Unknown ⚠ check: no testifier identified'}", "",
                    f"Position: {entry.get('position')}" + cite(entry.get("position_cues"), entry.get("position_check")),
                    "", "Main Arguments:", ""]
            out += [f"- {a.get('text')}" + cite(a.get("cues"), a.get("check")) for a in entry.get("arguments") or []]
            out += ["", "Legislator Questions:", ""]
            questions = entry.get("questions") or []
            out += [f"- {q.get('asker')}: {q.get('question')} — {q.get('respondent')}: {q.get('response') or 'No response noted'}"
                    + cite(q.get("cues"), q.get("check")) for q in questions]
            if not questions:
                out.append(f"- {entry.get('questions_note') or 'None noted ⚠ check: confirm no questions were asked'}")
            out += ["", "---", ""]
    return "\n".join(out)


def run(args) -> Path:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise SystemExit("Set GEMINI_API_KEY (in Colab: the key icon > Secrets, with notebook access on).")
    srt_text = Path(args.srt).read_text(encoding="utf-8")
    agenda = json.loads(Path(args.agenda).read_text(encoding="utf-8"))
    cues = parse_srt(srt_text)
    prompt = core.build_prompt(cues, agenda["hearing"], agenda["agenda"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = Path(args.out) / f"{args.model}-{stamp}"
    folder.mkdir(parents=True)
    print(f"{len(cues)} cues, {len(agenda['agenda'])} agenda bills; calling {args.model}...", flush=True)
    payload, seconds = call_gemini(prompt, args.model, key, args.max_output,
                                   None if args.thinking == "default" else args.thinking)
    (folder / "raw-response.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    text, finish = core.response_text(payload)
    meta = {"model": args.model, "model_version": payload.get("modelVersion"), "seconds": round(seconds, 1),
            "finish_reason": finish, "usage": payload.get("usageMetadata"), "created_utc": stamp,
            "srt_sha256": hashlib.sha256(srt_text.encode("utf-8")).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "cues": len(cues)}
    (folder / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (folder / "agenda.json").write_text(json.dumps(agenda, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        draft = json.loads(text)
    except ValueError:
        print(f"Response was not valid JSON (finish reason {finish}); saved raw-response.json.")
        return folder
    (folder / "model-draft.json").write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
    report(folder, draft, cues, agenda, args.model)
    print(f"{seconds:.0f} s, finish {finish}, tokens {payload.get('usageMetadata')}")
    return folder


def report(folder: Path, draft: dict, cues: list[dict], agenda: dict, model: str):
    """Rejoin split entries, resolve quotes to cues (when present), then check and render."""
    merged = merge_fragments(draft)
    has_quotes = any(holder.get(quote_key) for entry in draft.get("entries") or []
                     for _, holder, quote_key, _, _ in citable(entry))
    resolution = resolve(draft, cues) if has_quotes else None
    (folder / "draft.json").write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
    checks = check(draft, cues, agenda)
    checks["quote_resolution"] = resolution
    checks["split_entries_rejoined"] = merged
    (folder / "checks.json").write_text(json.dumps(checks, indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / "notes.md").write_text(render(draft, cues, agenda, model), encoding="utf-8")
    print(f"{folder.name}: {checks['entries']} entries, {len(checks['bills_in_draft'])} bills, "
          f"{checks['flags']} flags, {len(checks['problems'])} citation problems, "
          f"{len(checks['uncovered_spans_20s_or_more'])} uncovered spans, "
          f"{len(checks['spoken_agenda_bills_missing_from_draft'])} spoken bills missing"
          + (f", quotes found {resolution['found']}/{resolution['quotes']} "
             f"({resolution['moved']} citations corrected)" if resolution else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run")
    start.add_argument("--srt", required=True)
    start.add_argument("--agenda", required=True)
    start.add_argument("--model", default=core.DEFAULT_MODEL)
    start.add_argument("--out", default="results")
    start.add_argument("--max-output", type=int, default=65536)
    start.add_argument("--thinking", default="low", choices=("low", "medium", "high", "default"))
    recheck = commands.add_parser("check")
    recheck.add_argument("folder")
    recheck.add_argument("--srt", required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        run(args)
    else:
        folder = Path(args.folder)
        agenda = json.loads((folder / "agenda.json").read_text(encoding="utf-8"))
        original = folder / "model-draft.json"
        draft = json.loads((original if original.exists() else folder / "draft.json").read_text(encoding="utf-8"))
        model = json.loads((folder / "run.json").read_text(encoding="utf-8"))["model"]
        report(folder, draft, parse_srt(Path(args.srt).read_text(encoding="utf-8")), agenda, model)


if __name__ == "__main__":
    sys.exit(main())
