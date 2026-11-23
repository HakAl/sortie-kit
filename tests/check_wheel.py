"""Compare a built wheel with the package directory of this tree.

Usage: python3 tests/check_wheel.py WHEEL.whl
The wheel must hold each file under src/sortie_kit that no run or build left there, with
the bytes it has in this tree, and beside them the files the build generates in the one
NAME-VERSION.dist-info directory its file name gives. No member more, none fewer, none
twice, and regular files only.
"""
from pathlib import Path
import stat
import sys
import zipfile

GENERATED = ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt", "licenses/LICENSE")
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "sortie_kit"


def left_behind(path):
    """Left by a run or a build, never part of the package."""
    return "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo") or path.name == ".DS_Store"


def problems(wheel, package=PACKAGE):
    wheel, package = Path(wheel), Path(package)
    parts = wheel.name[:-len(".whl")].split("-") if wheel.name.endswith(".whl") else []
    if len(parts) not in (5, 6) or not all(parts):
        return ["not a wheel's file name: " + wheel.name]
    info = parts[0] + "-" + parts[1] + ".dist-info/"
    files = {package.name + "/" + path.relative_to(package).as_posix(): path
             for path in sorted(package.rglob("*")) if path.is_file() and not left_behind(path.relative_to(package))}
    expected = set(files) | {info + name for name in GENERATED}
    found = []
    with zipfile.ZipFile(wheel) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        found += ["in the wheel twice: " + name for name in sorted(set(names)) if names.count(name) > 1]
        found += ["in the wheel and not expected: " + name for name in sorted(set(names) - expected)]
        found += ["expected and not in the wheel: " + name for name in sorted(expected - set(names))]
        for member in members:
            if member.filename not in expected:
                continue
            kind = stat.S_IFMT(member.external_attr >> 16)  # 0 when the archiver recorded no file type
            if member.is_dir() or kind not in (0, stat.S_IFREG):
                found.append("not a regular file: " + member.filename)
            elif member.filename in files and archive.read(member) != files[member.filename].read_bytes():
                found.append("not the bytes of the tree: " + member.filename)
    return found


def main(argv):
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    found = problems(argv[0])
    if found:
        print("\n".join(found), file=sys.stderr)
        return 1
    with zipfile.ZipFile(argv[0]) as archive:
        count = len(archive.infolist())
    print("PASS: " + Path(argv[0]).name + " holds the " + str(count - len(GENERATED)) + " package files of this tree, the "
          + str(len(GENERATED)) + " generated files and nothing else")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
