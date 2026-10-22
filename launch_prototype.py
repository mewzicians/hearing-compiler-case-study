"""Portable executable entry. With no arguments, open the local app."""

import sys

from hearing_compiler.__main__ import main

if __name__ == "__main__":
    automatic = len(sys.argv) == 1
    if automatic:
        sys.argv.extend(["serve", "--port", "0", "--open"])
    try:
        main()
    except SystemExit as error:
        if automatic and error.code:
            input("Press Enter to close. ")
        raise
