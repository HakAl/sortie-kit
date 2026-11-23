"""Compare a built source distribution with tests/sdist-files.txt.

Usage: python3 tests/check_sdist.py SDIST.tar.gz
Every member of the archive must be the one top directory or inside it, and a regular
file or a directory that holds an expected file. The build generates three files
(PKG-INFO, setup.cfg and SOURCES.txt in the egg-info directory beside the package), and
each must be there; the rest must be the list exactly. No member more, none fewer and
none twice.
"""
from pathlib import Path
import sys
import tarfile

GENERATED = ("PKG-INFO", "setup.cfg", "src/sortie_kit.egg-info/SOURCES.txt")
LISTED = Path(__file__).resolve().parent / "sdist-files.txt"


def problems(sdist, listed):
    files, directories, found = [], [], []
    with tarfile.open(sdist) as archive:
        members = [(member, member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name)
                   for member in archive.getmembers()]
    # The one top directory is the first part of the first name, and every member is it or is inside it.
    top = members[0][1].split("/", 1)[0] if members else ""
    for member, name in members:
        parts = name.split("/")
        if top in ("", ".", "..") or parts[0] != top or any(part in ("", ".", "..") for part in parts) \
                or (len(parts) < 2 and not member.isdir()):
            found.append("not one top directory with every member inside it: " + name)
        elif member.isdir():
            directories.append("/".join(parts[1:]))
        else:
            if not member.isfile():
                found.append("not a regular file: " + name)
            files.append("/".join(parts[1:]))
    expected = set(listed) | set(GENERATED)
    holding = {""} | {name.rsplit("/", count)[0] for name in expected for count in range(1, name.count("/") + 1)}
    found += ["in the archive twice: " + name for name in sorted(set(files)) if files.count(name) > 1]
    found += ["in the archive twice: " + name + "/" for name in sorted(set(directories)) if directories.count(name) > 1]
    found += ["a directory in the archive that holds no expected file: " + name
              for name in sorted(set(directories) - holding)]
    found += ["in the archive and not in the list: " + name for name in sorted(set(files) - expected)]
    found += ["in the list and not in the archive: " + name for name in sorted(set(listed) - set(files))]
    found += ["missing from the archive: " + name for name in GENERATED if name not in files]
    return found


def main(argv):
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    listed = LISTED.read_text().splitlines()
    found = problems(argv[0], listed)
    if found:
        print("\n".join(found), file=sys.stderr)
        return 1
    print("PASS: " + Path(argv[0]).name + " holds the " + str(len(listed)) + " listed files, the "
          + str(len(GENERATED)) + " generated files and nothing else")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
