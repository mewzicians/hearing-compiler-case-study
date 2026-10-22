#!/bin/sh
# Source-checkout fallback; the standalone .app does not need this environment.
cd -- "$(dirname -- "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
    printf '%s\n' 'One-time setup is needed. See docs/MAC_APP_GUIDE.md.' \
        'Create .venv with Python 3, then install requirements.txt.'
    printf '%s' 'Press Enter to close: '
    read -r ignored
    exit 1
fi
exec .venv/bin/python -m hearing_compiler serve --port 0 --open
