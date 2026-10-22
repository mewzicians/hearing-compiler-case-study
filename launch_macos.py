"""Finder entry point, with visible startup errors and the existing CLI retained."""
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import sys
import traceback

from hearing_compiler.__main__ import main


def run():
    if len(sys.argv) > 1:
        # The native package audit invokes this windowed binary with redirected
        # descriptors. PyInstaller may clear its Python-level stream objects.
        if sys.stdout is None:
            sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
        if sys.stderr is None:
            sys.stderr = open(2, "w", encoding="utf-8", closefd=False)
        return main()
    folder = Path.home() / "Library/Application Support/HearingCompilerPrototype"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "launcher.log").open("a", encoding="utf-8") as log:
            with redirect_stdout(log), redirect_stderr(log):
                sys.argv.extend(["serve", "--port", "0", "--open"])
                try:
                    main()
                except SystemExit as error:
                    if error.code:
                        raise RuntimeError("Startup failed. Check launcher.log in the saved-work folder.") from error
                except Exception:
                    traceback.print_exc(file=log)
                    log.flush()
                    raise
    except Exception as error:
        # argv passes text as data, never interpolated AppleScript.
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             'on run argv\n display alert "Hearing Compiler could not open" message (item 1 of argv)\nend run',
             str(error)[:1000]], check=False)
        raise SystemExit(1) from error


if __name__ == "__main__":
    run()
