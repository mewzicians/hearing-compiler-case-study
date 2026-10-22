"""Read the code signatures in a Mac release ZIP on any computer, without unpacking it.

Lists every Mach-O file's CodeDirectory flags, Team ID and entitlements. Exits 1
if any has the hardened runtime without a Team ID: library validation then loads
only code with the program's own Team ID, so on a normal Mac the app can't load
its own Python and never opens. GitHub's Mac runners have System Integrity
Protection off and start such an app anyway, so only a check like this sees it.

    python -I scripts/mac_signature_check.py HearingCompilerPrototype-Mac-AppleSilicon.zip
"""
from __future__ import annotations

from collections import Counter
import struct
import sys
import zipfile

RUNTIME = 0x10000
FLAG_NAMES = {0x2: "adhoc", 0x10000: "runtime", 0x20000: "linker-signed"}
LC_CODE_SIGNATURE = 0x1D
SUPERBLOB, CODE_DIRECTORY = 0xFADE0CC0, 0xFADE0C02
ENTITLEMENTS = {0xFADE7171, 0xFADE7172}  # XML and DER blobs
THIN = b"\xcf\xfa\xed\xfe"  # 64-bit little-endian Mach-O, as on Apple Silicon
FAT = {b"\xca\xfe\xba\xbe": (">5I", 20), b"\xca\xfe\xba\xbf": (">2I2Q2I", 32)}  # fat_arch, fat_arch_64


def images(data: bytes) -> list[bytes]:
    """The 64-bit Mach-O images in a thin or universal binary."""
    if data[:4] == THIN:
        return [data]
    layout, size = FAT[data[:4]]
    count = struct.unpack_from(">I", data, 4)[0]
    if count > 30:  # Java class files share the universal magic number.
        return []
    slices = [struct.unpack_from(layout, data, 8 + i * size)[2:4] for i in range(count)]
    return [data[start:start + length] for start, length in slices if data[start:start + 4] == THIN]


def signature(image: bytes) -> dict:
    """Flags (None if unsigned), Team ID and entitlements of one image's embedded signature."""
    found = {"flags": None, "team": None, "entitlements": False, "error": None}
    commands, offset = struct.unpack_from("<I", image, 16)[0], 32
    for _ in range(commands):
        command, size = struct.unpack_from("<2I", image, offset)
        if command == LC_CODE_SIGNATURE:
            start, length = struct.unpack_from("<2I", image, offset + 8)
            blob = image[start:start + length]
            magic, _, count = struct.unpack_from(">3I", blob)
            if magic != SUPERBLOB:
                raise ValueError(f"unexpected signature format {magic:#x}")
            for i in range(count):
                at = struct.unpack_from(">I", blob, 16 + 8 * i)[0]
                kind = struct.unpack_from(">I", blob, at)[0]
                if kind in ENTITLEMENTS:
                    found["entitlements"] = True
                elif kind == CODE_DIRECTORY:
                    version, flags = struct.unpack_from(">2I", blob, at + 8)
                    found["flags"] = (found["flags"] or 0) | flags
                    team = struct.unpack_from(">I", blob, at + 48)[0] if version >= 0x20200 else 0
                    if team and not found["team"]:
                        found["team"] = blob[at + team:blob.index(b"\0", at + team)].decode("ascii", "replace")
            if found["flags"] is None:
                raise ValueError("the signature has no code directory")
            return found
        offset += size
    return found


def inspect(path) -> list[tuple[str, dict]]:
    """(member name, signature) for every Mach-O image in the ZIP."""
    found = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            # Folders, links and the resource-fork copies ditto keeps under __MACOSX are not programs.
            if (member.is_dir() or member.filename.startswith("__MACOSX/")
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                continue
            with archive.open(member) as handle:
                if handle.read(4) not in (THIN, *FAT):
                    continue
            try:
                found += [(member.filename, signature(image)) for image in images(archive.read(member))]
            except (struct.error, ValueError) as error:
                found.append((member.filename, {"flags": None, "team": None, "entitlements": False,
                                                "error": str(error) or type(error).__name__}))
    return found


def label(sig: dict) -> str:
    if sig["error"]:
        return f"unreadable ({sig['error']})"
    if sig["flags"] is None:
        return "unsigned"
    names = [name for bit, name in FLAG_NAMES.items() if sig["flags"] & bit]
    return f"flags={sig['flags']:#x}({','.join(names) or 'none'})"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__.strip())
        return 2
    found = inspect(argv[0])
    for name, sig in found:
        print(f"{label(sig)}  team={sig['team'] or 'none'}  entitlements={'yes' if sig['entitlements'] else 'no'}  {name}")
    print(f"\n{len(found)} Mach-O files:")
    for (text, team), count in Counter((label(s), s["team"] or "none") for _, s in found).most_common():
        print(f"  {count} with {text}, team {team}")
    unreadable = [name for name, sig in found if sig["error"]]
    hardened = [name for name, sig in found if (sig["flags"] or 0) & RUNTIME and not sig["team"]]
    if not found:
        print("FAIL: no Mach-O files in this ZIP. Is it the Mac release?")
    if unreadable:
        print(f"FAIL: {len(unreadable)} signature(s) could not be read, so this check can't vouch for them.")
    if hardened:
        print(f"FAIL: {len(hardened)} Mach-O file(s) have the hardened runtime without a Team ID. A normal Mac "
              "would refuse to load the app's own libraries, so the app would not open. Sign without --options runtime.")
    if not found or unreadable or hardened:
        return 1
    print("OK: no Mach-O file has the hardened runtime without a Team ID.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
