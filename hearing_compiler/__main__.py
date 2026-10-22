from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from .pipeline import InputError, correct, extract, markdown
from .server import ROOT, make_server
from .providers import MacWhisperProvider, normalize_macwhisper

RUNTIME_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
DEFAULT_STATE = (
    (Path.home() / "Library/Application Support/HearingCompilerPrototype" if sys.platform == "darwin"
     else Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "HearingCompilerPrototype")
    if getattr(sys, "frozen", False) else ROOT / ".local" / "prototype"
)


def main():
    parser = argparse.ArgumentParser(description="Synthetic hearing draft, review and copy prototype.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the local browser app.")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--state", type=Path, default=DEFAULT_STATE)
    serve.add_argument("--empty", action="store_true", help="Kept for compatibility; workspaces now start empty.")
    serve.add_argument("--open", action="store_true", help="Open the app in the default browser.")
    compile_ = commands.add_parser("compile", help="Compile synthetic transcript JSON to draft JSON and Markdown.")
    compile_.add_argument("transcript", type=Path)
    compile_.add_argument("--output", type=Path, default=RUNTIME_ROOT / ".local" / "exports")
    compile_.add_argument("--corrections", type=Path, help="A JSON list of {path, value, review} corrections.")
    normalize = commands.add_parser("normalize-macwhisper", help="Convert documented mw JSON milliseconds to transcript seconds.")
    normalize.add_argument("export", type=Path)
    normalize.add_argument("--metadata", type=Path, required=True, help="Hearing metadata JSON.")
    normalize.add_argument("--output", type=Path, default=RUNTIME_ROOT / ".local" / "macwhisper-transcript.json")
    normalize.add_argument("--public-hearing", action="store_true", help="Declare the input is a public hearing, rather than synthetic.")
    transcribe = commands.add_parser("transcribe-mac", help="Run mw on macOS with an explicit local model.")
    transcribe.add_argument("recording", type=Path)
    transcribe.add_argument("--metadata", type=Path, required=True)
    transcribe.add_argument("--model", required=True)
    transcribe.add_argument("--output", type=Path, default=RUNTIME_ROOT / ".local" / "macwhisper-transcript.json")
    backup = commands.add_parser("backup", help="Create and verify a full stopped-workspace backup with recordings.")
    backup.add_argument("--state", type=Path, default=DEFAULT_STATE)
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore", help="Restore a verified full backup into a new empty workspace.")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--state", type=Path, required=True)
    commands.add_parser("check-updates", help="Fetch the published release description over HTTPS (release check).")
    rehearse = commands.add_parser("self-update", help="CI rehearsal: install the newest build from a local mirror.")
    rehearse.add_argument("--releases", required=True, help="Loopback mirror, e.g. http://127.0.0.1:8000")
    rehearse.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    if args.command == "self-update":
        import re
        from . import updater
        from .store import Store
        if not re.fullmatch(r"http://127\.0\.0\.1:\d{2,5}", args.releases):
            parser.exit(2, "Error: self-update only accepts a loopback mirror such as http://127.0.0.1:8000\n")
        updater.RELEASES = args.releases
        updater.MANIFEST_URL = args.releases + "/latest/download/manifest.json"
        print(json.dumps(updater.install(Store(args.state))), flush=True)
        return
    if args.command == "check-updates":
        from .tls import context
        from .updater import latest
        print(json.dumps({"latest": latest(force=True), "trusted_roots": context().cert_store_stats()["x509_ca"]}))
        return
    try:
        if args.command in {"backup", "restore"}:
            from .server import WorkspaceLease
            from .store import Store
            from .workspace_backup import create_backup, verify_backup, restore_backup
            lease = WorkspaceLease(args.state)
            try:
                store = Store(args.state)
                if args.command == "backup":
                    if args.output.exists():
                        raise InputError("Choose a new backup filename; existing backups are preserved.")
                    temporary = create_backup(store)
                    staged_output = args.output.with_name(args.output.name + ".partial-" + __import__("uuid").uuid4().hex)
                    try:
                        verify_backup(temporary)
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        with temporary.open("rb") as source, staged_output.open("xb") as target:
                            import shutil
                            shutil.copyfileobj(source, target, 1024 * 1024)
                            target.flush()
                            os.fsync(target.fileno())
                        # Hard-link creation cannot overwrite an existing backup.
                        # Both paths are in the user-selected destination folder.
                        os.link(staged_output, args.output)
                    finally:
                        temporary.unlink(missing_ok=True)
                        staged_output.unlink(missing_ok=True)
                    print(f"Verified full workspace backup: {args.output.resolve()}")
                else:
                    result = restore_backup(store, args.archive)
                    print(f"Restored {result['restored']} hearing(s) into {args.state.resolve()}")
            finally:
                lease.close()
        elif args.command == "serve":
            import logging
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            server = make_server(args.state, args.port)
            from .version import info as version_info
            built = version_info()
            if built["packaged"] and built["build"]:
                # Tells a just-installed update that this version started; otherwise it is rolled back.
                from .updater import health_marker
                marker = health_marker(server.store.directory, built["build"])
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("ok\n", encoding="utf-8")
            print(f"Hearing Compiler: http://127.0.0.1:{server.server_port}", flush=True)
            print(f"Saved progress: {args.state.resolve()}", flush=True)
            print("Local hearing workspace. Press Ctrl+C to stop.", flush=True)
            if args.open:
                webbrowser.open(f"http://127.0.0.1:{server.server_port}")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                try:
                    server.server_close()
                except InputError as error:
                    # A long request (e.g. drafting) was still running; its job is marked for retry on next start.
                    print(f"Stopped while work was running: {error}", flush=True)
        elif args.command == "compile":
            draft = extract(json.loads(args.transcript.read_text(encoding="utf-8")))
            if args.corrections:
                draft = correct(draft, json.loads(args.corrections.read_text(encoding="utf-8")))
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
            (args.output / "hearing-notes.md").write_text(markdown(draft), encoding="utf-8")
            print(f"Wrote draft.json and hearing-notes.md to {args.output.resolve()}")
        else:
            metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
            if args.command == "normalize-macwhisper":
                document = normalize_macwhisper(json.loads(args.export.read_text(encoding="utf-8")),
                                               metadata, synthetic=not args.public_hearing)
            else:
                document = MacWhisperProvider(args.model).transcribe(args.recording, metadata)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Wrote normalized transcript to {args.output.resolve()}")
    except (InputError, OSError, ValueError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
