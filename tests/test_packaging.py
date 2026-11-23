"""What the source distribution holds: tests/sdist-files.txt against the tree it is read in.

The same test runs in a checkout and in an unpacked source distribution.
tests/check_sdist.py compares a built archive with the list, and tests/check_wheel.py
compares a built wheel with the package directory. Both are tried here on small
archives made by the test: every member of an archive is accounted for.
"""
import io
import json
from pathlib import Path
import re
import tarfile
import unittest
import warnings
import zipfile

import check_sdist
import check_wheel
from support import LocalTest

ROOT = Path(__file__).resolve().parents[1]
LISTED = (ROOT / "tests" / "sdist-files.txt").read_text().splitlines()
# Whole directories, and beside them these files and no others.
WHOLE = ("src", "tests")
SINGLE = {"MANIFEST.in", "pyproject.toml", "README.md", "AGENTS.md", "SECURITY.md", "LICENSE",
          "docs/agents.md", "docs/grants.md", "docs/lifecycle.md", "docs/receipts.md", "docs/reports.md",
          "docs/review.md", "docs/source-provenance.json"}


def generated(path):
    """Left by a run or a build, never part of the source."""
    return ("__pycache__" in path.parts or path.suffix in (".pyc", ".pyo") or path.name == ".DS_Store"
            or any(part.endswith(".egg-info") for part in path.parts))


class SourceDistributionListTests(unittest.TestCase):
    def test_the_list_is_sorted_and_names_each_file_once(self):
        self.assertEqual(LISTED, sorted(set(LISTED)))
        for name in LISTED:
            self.assertFalse(name.startswith(("/", ".")) or ".." in name.split("/") or name != name.strip(), name)

    def test_every_listed_file_is_in_the_tree(self):
        self.assertEqual([name for name in LISTED if not (ROOT / name).is_file()], [])

    def test_the_list_is_the_whole_directories_and_the_named_files(self):
        found = {path.relative_to(ROOT).as_posix() for top in WHOLE for path in (ROOT / top).rglob("*")
                 if path.is_file() and not generated(path.relative_to(ROOT))}
        self.assertEqual(sorted(found | SINGLE), LISTED)


class OneLicenceTests(unittest.TestCase):
    """The project has one licence, MIT, in the one file LICENSE."""

    def test_the_licence_file_is_the_mit_text_and_the_metadata_names_it_alone(self):
        import hashlib
        import tomllib
        # The whole text, by its sum: the MIT licence with the project's one copyright line.
        raw = (ROOT / "LICENSE").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), "c7c26e207c9747690d9395eae3088f9c560c06a1a61471d3ba5f768b907629d7")
        self.assertTrue(raw.startswith(b"MIT License\n\nCopyright (c) "), raw[:40])
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual((project["license"], project["license-files"]), ("MIT", ["LICENSE"]))
        self.assertEqual([name for name in project["classifiers"] if name.startswith("License ::")], [])
        provenance = json.loads((ROOT / "docs" / "source-provenance.json").read_text())
        self.assertEqual(provenance["license_files"], ["LICENSE"])

    def test_no_listed_file_names_another_licence_or_a_licence_file_that_is_gone(self):
        gone = re.compile("apa" + "che|tiger-" + "mit|licenses/" + "tiger", re.IGNORECASE)
        notice = re.compile(r"\b" + "NOT" + r"ICE\b")  # the file's name, in capitals; the licence's own word is not
        for name in LISTED:
            self.assertNotIn(name, ("NOT" + "ICE", "licenses/" + "tiger-" + "MIT.txt"))
            text = (ROOT / name).read_bytes().decode("utf-8", "replace")
            self.assertEqual((gone.findall(text), notice.findall(text)), ([], []), name)
        self.assertFalse((ROOT / ("NOT" + "ICE")).exists() or (ROOT / "licenses").exists())


class SourceDistributionArchiveTests(LocalTest):
    LISTED = ["README.md", "src/pkg/__init__.py"]
    RIGHT = ["PKG-INFO", "setup.cfg", "README.md", "src/pkg/__init__.py", "src/sortie_kit.egg-info/SOURCES.txt"]

    def archive(self, names, top="pkg-1.0/", directories=(), kind=None):
        """An archive of the named files under the top directory, of the directory members
        given by their whole names, and with the one file named by kind as a symbolic link."""
        path = self.root / "pkg-1.0.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name in directories:
                member = tarfile.TarInfo(name)
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            for name in names:
                member = tarfile.TarInfo(top + name)
                if name == kind:
                    member.type, member.linkname = tarfile.SYMTYPE, "elsewhere"
                    archive.addfile(member)
                    continue
                member.size = 1
                archive.addfile(member, io.BytesIO(b"x"))
        return path

    def test_the_listed_files_and_the_generated_ones_pass(self):
        self.assertEqual(check_sdist.problems(self.archive(self.RIGHT), self.LISTED), [])

    def test_a_file_more_in_the_egg_info_directory_is_refused(self):
        for more in ("src/sortie_kit.egg-info/extra.py", "src/sortie_kit.egg-info/PKG-INFO",
                     "src/sortie_kit.egg-info/deep/SOURCES.txt"):
            found = check_sdist.problems(self.archive(self.RIGHT + [more]), self.LISTED)
            self.assertEqual(found, ["in the archive and not in the list: " + more])

    def test_a_generated_name_twice_is_refused(self):
        for twice in ("PKG-INFO", "setup.cfg", "src/sortie_kit.egg-info/SOURCES.txt", "README.md"):
            found = check_sdist.problems(self.archive(self.RIGHT + [twice]), self.LISTED)
            self.assertEqual(found, ["in the archive twice: " + twice])

    def test_a_generated_file_that_is_missing_is_refused(self):
        for gone in ("PKG-INFO", "setup.cfg", "src/sortie_kit.egg-info/SOURCES.txt"):
            found = check_sdist.problems(self.archive([name for name in self.RIGHT if name != gone]), self.LISTED)
            self.assertEqual(found, ["missing from the archive: " + gone])

    def test_a_listed_file_more_or_fewer_is_refused(self):
        self.assertEqual(check_sdist.problems(self.archive(self.RIGHT + ["notes.txt"]), self.LISTED),
                         ["in the archive and not in the list: notes.txt"])
        self.assertEqual(check_sdist.problems(self.archive(self.RIGHT), self.LISTED + ["tests/t.py"]),
                         ["in the list and not in the archive: tests/t.py"])

    def test_the_directories_that_hold_the_files_may_be_members(self):
        held = ["pkg-1.0", "pkg-1.0/src", "pkg-1.0/src/pkg", "pkg-1.0/src/sortie_kit.egg-info"]
        self.assertEqual(check_sdist.problems(self.archive(self.RIGHT, directories=held), self.LISTED), [])
        self.assertEqual(check_sdist.problems(self.archive(self.RIGHT, directories=[d + "/" for d in held]), self.LISTED), [])

    def test_a_directory_that_holds_no_expected_file_or_is_a_member_twice_is_refused(self):
        for more in ("pkg-1.0/unexpected", "pkg-1.0/src/pkg/deeper", "pkg-1.0/README.md"):
            found = check_sdist.problems(self.archive(self.RIGHT, directories=[more]), self.LISTED)
            self.assertEqual(found, ["a directory in the archive that holds no expected file: " + more[8:]])
        found = check_sdist.problems(self.archive(self.RIGHT, directories=["pkg-1.0/src", "pkg-1.0/src/"]), self.LISTED)
        self.assertEqual(found, ["in the archive twice: src/"])

    def test_a_member_outside_the_one_top_directory_is_refused(self):
        outside = "not one top directory with every member inside it: "
        # A directory member is written first here, so the first name's first part is the top.
        for directory in ("other-top", "/pkg-1.0/src", "pkg-1.0/../escape", "pkg-1.0/src/../.."):
            found = check_sdist.problems(self.archive(self.RIGHT, directories=[directory]), self.LISTED)
            self.assertTrue(found and found[0].startswith(outside), (directory, found))
        for top in ("", "./", "../", "/pkg-1.0/", "pkg-1.0/../"):
            found = check_sdist.problems(self.archive(self.RIGHT, top=top), self.LISTED)
            self.assertTrue(found and found[0].startswith(outside), (top, found))
        found = check_sdist.problems(self.archive(self.RIGHT + ["../loose.txt"]), self.LISTED)
        self.assertEqual(found[:1], [outside + "pkg-1.0/../loose.txt"])

    def test_a_member_that_is_not_a_regular_file_is_refused(self):
        for name in ("README.md", "PKG-INFO", "src/sortie_kit.egg-info/SOURCES.txt"):
            found = check_sdist.problems(self.archive(self.RIGHT, kind=name), self.LISTED)
            self.assertEqual(found, ["not a regular file: pkg-1.0/" + name])


class WheelArchiveTests(LocalTest):
    INFO = "pkg_name-1.0.dist-info/"
    FILES = {"__init__.py": b"one\n", "cli.py": b"two\n", "data/templates/a.md": b"three\n"}

    def setUp(self):
        super().setUp()
        self.package = self.root / "src" / "sortie_kit"
        for name, body in self.FILES.items():
            (self.package / name).parent.mkdir(parents=True, exist_ok=True)
            (self.package / name).write_bytes(body)
        # Left by a run, never part of the package.
        (self.package / "__pycache__").mkdir()
        (self.package / "__pycache__" / "cli.cpython-313.pyc").write_bytes(b"compiled")
        (self.package / ".DS_Store").write_bytes(b"finder")
        self.right = [("sortie_kit/" + name, body) for name, body in self.FILES.items()]
        self.right += [(self.INFO + name, b"generated\n") for name in check_wheel.GENERATED]

    def wheel(self, members):
        path = self.root / "pkg_name-1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive, warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # zipfile warns of the name a test writes twice
            for name, body in members:
                archive.writestr(zipfile.ZipInfo(name), body)
        return path

    def test_the_package_files_and_the_generated_ones_pass(self):
        self.assertEqual(check_wheel.problems(self.wheel(self.right), self.package), [])
        self.assertEqual(len(check_wheel.GENERATED), 6)
        self.assertIn("licenses/LICENSE", check_wheel.GENERATED)

    def test_a_member_more_is_refused(self):
        for more in ("sortie_kit/extra.py", "sortie_kit/data/runtime/x.sh", "other/__init__.py", "loose.py",
                     self.INFO + "extra.txt", self.INFO + "licenses/OTHER", self.INFO + "licenses/" + "NOT" + "ICE",
                     self.INFO + "licenses/licenses/" + "tiger-" + "MIT.txt", "pkg_name-1.0.data/scripts/run",
                     "sortie_kit/__pycache__/cli.cpython-313.pyc", "sortie_kit/.DS_Store"):
            found = check_wheel.problems(self.wheel(self.right + [(more, b"more")]), self.package)
            self.assertEqual(found, ["in the wheel and not expected: " + more])

    def test_a_member_fewer_is_refused(self):
        for gone in ("sortie_kit/cli.py", "sortie_kit/data/templates/a.md", self.INFO + "RECORD",
                     self.INFO + "licenses/LICENSE"):
            found = check_wheel.problems(self.wheel([item for item in self.right if item[0] != gone]), self.package)
            self.assertEqual(found, ["expected and not in the wheel: " + gone])

    def test_a_member_twice_is_refused(self):
        for twice in ("sortie_kit/cli.py", self.INFO + "METADATA"):
            body = dict(self.right)[twice]
            found = check_wheel.problems(self.wheel(self.right + [(twice, body)]), self.package)
            self.assertEqual(found, ["in the wheel twice: " + twice])

    def test_a_package_file_with_other_bytes_is_refused(self):
        changed = [(name, b"other\n" if name == "sortie_kit/cli.py" else body) for name, body in self.right]
        self.assertEqual(check_wheel.problems(self.wheel(changed), self.package),
                         ["not the bytes of the tree: sortie_kit/cli.py"])

    def test_a_member_that_is_not_a_regular_file_or_has_an_unsafe_name_is_refused(self):
        link = zipfile.ZipInfo("sortie_kit/cli.py")
        link.external_attr = 0o120777 << 16
        path = self.root / "pkg_name-1.0-py3-none-any.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name, body in self.right:
                archive.writestr(link if name == link.filename else zipfile.ZipInfo(name), body)
        self.assertEqual(check_wheel.problems(path, self.package), ["not a regular file: sortie_kit/cli.py"])
        for name in ("sortie_kit/../cli.py", "/sortie_kit/cli.py", "sortie_kit/sub/"):
            found = check_wheel.problems(self.wheel(self.right + [(name, b"")]), self.package)
            self.assertEqual(found, ["in the wheel and not expected: " + name])

    def test_the_file_name_says_which_dist_info_directory_is_expected(self):
        path = self.wheel(self.right)
        other = path.with_name("pkg_name-2.0-py3-none-any.whl")
        path.rename(other)
        found = check_wheel.problems(other, self.package)
        self.assertIn("expected and not in the wheel: pkg_name-2.0.dist-info/METADATA", found)
        self.assertIn("in the wheel and not expected: " + self.INFO + "METADATA", found)
        for name in ("pkg_name.whl", "pkg_name-1.0-py3-none-any.zip"):
            self.assertEqual(check_wheel.problems(path.with_name(name), self.package),
                             ["not a wheel's file name: " + name])


if __name__ == "__main__":
    unittest.main()
