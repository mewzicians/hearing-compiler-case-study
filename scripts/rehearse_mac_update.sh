#!/bin/bash
# CI rehearsal: an installed older app updates itself to the candidate build from a
# local mirror of the releases page, and the new version must start. Uses the current
# public latest release when it supports rehearsal; otherwise the candidate stamped one
# build older. Usage: rehearse_mac_update.sh <candidate.zip> <build> <commit>
set -euo pipefail
ZIP="$1" BUILD="$2" COMMIT="$3"
WORK="${RUNNER_TEMP:-/tmp}/update-rehearsal"
STATE="$HOME/Library/Application Support/HearingCompilerPrototype"
APP="$WORK/Applications/Hearing Compiler.app"
LATEST="https://github.com/mewzicians/hearing-compiler-releases/releases/latest/download/HearingCompilerPrototype-Mac-AppleSilicon.zip"
rm -rf "$WORK" && mkdir -p "$WORK/Applications" "$WORK/mirror/latest/download" "$WORK/mirror/download/build-$BUILD"

install_from() {  # zip -> $APP
  rm -rf "$WORK/unpacked" "$APP"
  ditto -x -k "$1" "$WORK/unpacked"
  ditto "$WORK/unpacked/HearingCompilerPrototype-Mac-AppleSilicon/Hearing Compiler.app" "$APP"
}

if curl -fsSL "$LATEST" -o "$WORK/published.zip" && install_from "$WORK/published.zip" \
   && "$APP/Contents/MacOS/Hearing Compiler" self-update --help >/dev/null 2>&1; then
  echo "Rehearsing from the published latest release."
else
  echo "Published latest can't rehearse yet; using the candidate stamped as build $((BUILD - 1))."
  install_from "$ZIP"
  find "$APP" -name build-stamp.json -type f -print0 | while IFS= read -r -d '' stamp; do
    python3 -c 'import json, sys; p = sys.argv[1]; d = json.load(open(p)); d["build"] = int(sys.argv[2]) - 1; json.dump(d, open(p, "w"))' "$stamp" "$BUILD"
  done
  codesign --force --deep -s - "$APP"
fi

cp "$ZIP" "$WORK/mirror/download/build-$BUILD/"
python3 -c 'import json, sys; sys.path.insert(0, "."); from pathlib import Path; from scripts.publish_mac_release import manifest; print(json.dumps(manifest(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3])))' \
  "$ZIP" "$BUILD" "$COMMIT" > "$WORK/mirror/latest/download/manifest.json"
(cd "$WORK/mirror" && exec python3 -m http.server 8765 --bind 127.0.0.1 >"$WORK/mirror.log" 2>&1) &
MIRROR=$!
trap 'kill $MIRROR 2>/dev/null || true; pkill -f "$APP/Contents/MacOS/Hearing Compiler" || true; cat "$WORK/mirror.log" 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:8765/latest/download/manifest.json >/dev/null 2>&1 && break; sleep 1; done
echo "Mirror manifest: $(curl -fsS http://127.0.0.1:8765/latest/download/manifest.json)"

"$APP/Contents/MacOS/Hearing Compiler" self-update --releases http://127.0.0.1:8765 --state "$STATE"
for _ in $(seq 1 150); do
  grep -qE "new version started|did not start|nothing changed|install failed" "$STATE/updates/update.log" 2>/dev/null && break
  sleep 1
done
cat "$STATE/updates/update.log"
grep -q "new version started" "$STATE/updates/update.log"
python3 -c 'import glob, json, sys; builds = {json.load(open(p))["build"] for p in glob.glob(sys.argv[1] + "/**/build-stamp.json", recursive=True)}; print("Installed build:", builds); sys.exit(builds != {int(sys.argv[2])})' "$APP" "$BUILD"
echo "Self-update rehearsal passed."
