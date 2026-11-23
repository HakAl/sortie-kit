"""The reach note for a profile of one's own, the shift path given to `sortie verify`,
and two wordings."""
import contextlib
import io
import os
from importlib.resources import files
from pathlib import Path

from sortie_kit import agents, cli, grants
from sortie_kit.layout import Refused
from support import LocalTest


def seat(role, name, path, argv=None):
    profile = agents.load(name, "/nowhere", home="/nowhere/no-home")
    made = agents.seat(role, profile, "model", "family", {"path": path, "sha256": "0" * 64, "bytes": 1})
    return dict(made, argv=argv) if argv else made


class ReachByProgramTests(LocalTest):
    def test_a_profile_of_ones_own_on_a_known_program_gets_that_programs_note(self):
        own = dict(seat("reviewer", "agy", "/opt/agents/bin/agy"), agent="my-agy")
        first = agents.reach("reviewer", own)[0]
        self.assertTrue(first.startswith("--mode plan --sandbox: the CLI's plan mode, with its terminal restrictions on"), first)
        self.assertNotIn("has not inspected", first)
        # The sign-in is still named by the profile the developer chose.
        self.assertIn("with your sign-in for my-agy", agents.reach("reviewer", own)[2])

    def test_a_reviewer_profile_of_ones_own_is_told_its_other_arguments_were_not_read(self):
        # The run that was found may be undone by another argument beside it.
        own = dict(seat("reviewer", "agy", "/opt/agents/bin/agy"), agent="my-agy")
        caveat = "a profile of your own: sortie-kit has not inspected what its other arguments allow"
        self.assertEqual(agents.reach("reviewer", own)[1], caveat)
        self.assertNotIn(caveat, agents.reach("reviewer", seat("reviewer", "agy", "/opt/agents/bin/agy")))

    def test_the_same_arguments_on_a_program_of_another_name_keep_the_generic_note(self):
        other = dict(seat("reviewer", "agy", "/opt/tools/wrapper"), agent="my-agy")
        self.assertEqual(agents.reach("reviewer", other)[0],
                         "sortie-kit has not inspected what these arguments allow; this reviewer may be able to write")
        # And a known program without the argument run: the name alone says nothing.
        bare = dict(seat("reviewer", "agy", "/opt/tools/agy"), agent="my-agy",
                    argv=["--print", "{prompt}", "--model", "{model}"])
        self.assertIn("has not inspected", agents.reach("reviewer", bare)[0])

    def test_a_built_in_profile_keeps_its_note_when_the_pinned_file_is_named_by_version(self):
        # The pin is the resolved file: the claude CLI installs as .../claude/versions/2.1.330.
        pinned = seat("reviewer", "claude", "/opt/agents/share/claude/versions/2.1.330")
        self.assertTrue(agents.reach("reviewer", pinned)[0].startswith("--permission-mode plan: "))
        builder = seat("builder", "claude", "/opt/agents/share/claude/versions/2.1.330")
        self.assertIn("only what your Claude settings allow without asking", agents.reach("builder", builder)[0])

    def test_a_builder_profile_of_ones_own_on_codex_gets_the_lines_its_arguments_match_and_no_more(self):
        own = dict(seat("builder", "codex", "/opt/homebrew/bin/codex"), agent="my-codex")
        lines = agents.reach("builder", own)
        self.assertIn("--sandbox workspace-write: write anywhere in the repository", lines)
        self.assertEqual(lines[-2:], [
            "a profile of your own: sortie-kit has not inspected what its other arguments allow",
            "anything your own my-codex configuration allows on top still applies; sortie-kit does not read or limit it"])
        # Nothing is said about what such a profile does NOT pass: its other arguments were not read.
        said = " ".join(lines)
        for negative in ("reading is not limited by any of these", "no sandbox option is passed", "no permission option"):
            self.assertNotIn(negative, said)
        generic = "a profile of your own: sortie-kit has not inspected what these arguments allow"
        bare = dict(own, argv=["exec", "{prompt}"])
        self.assertEqual(agents.reach("builder", bare)[0], generic)
        self.assertNotIn("no sandbox option is passed", " ".join(agents.reach("builder", bare)))
        elsewhere = dict(own, executable=dict(own["executable"], path="/opt/tools/wrapped"))
        self.assertEqual(agents.reach("builder", elsewhere)[0], generic)

    def test_a_builder_profile_of_ones_own_on_claude_gets_the_generic_note_only(self):
        own = dict(seat("builder", "claude", "/opt/agents/bin/claude"), agent="my-claude")
        lines = agents.reach("builder", own)
        self.assertEqual(lines[0], "a profile of your own: sortie-kit has not inspected what these arguments allow")
        self.assertNotIn("no permission option", " ".join(lines))

    def test_the_built_in_builders_say_what_they_said(self):
        codex = agents.reach("builder", seat("builder", "codex", "/opt/homebrew/bin/codex"))
        self.assertIn("reading is not limited by any of these: it can read whatever your user can read", codex)
        bare = agents.reach("builder", seat("builder", "codex", "/opt/homebrew/bin/codex", argv=["exec", "{prompt}"]))
        self.assertTrue(bare[0].startswith("no sandbox option is passed: "), bare)


class ShiftRootTests(LocalTest):
    def setUp(self):
        super().setUp()
        self.real = Path(os.path.realpath(self.root))
        self.repo_path = self.real / "other" / "repo"
        self.shift = self.repo_path / ".sortie" / "shifts" / "ID"
        self.shift.mkdir(parents=True)
        (self.real / "here").mkdir()

    def at(self, directory, path):
        before = os.getcwd()
        os.chdir(directory)
        try:
            return grants.shift_root(path)
        finally:
            os.chdir(before)

    def test_a_path_with_dot_dot_or_dot_in_it_names_the_repository_it_leads_to(self):
        self.assertEqual(self.at(self.real / "here", "../other/repo/.sortie/shifts/ID"), self.repo_path)
        self.assertEqual(self.at(self.repo_path, "./.sortie/shifts/ID"), self.repo_path)
        self.assertEqual(grants.shift_root(str(self.repo_path) + "/./.sortie/../.sortie/shifts/ID/"), self.repo_path)

    def test_a_repository_reached_through_a_symlink_is_still_refused(self):
        (self.real / "link").symlink_to(self.real / "other")
        for path in (self.real / "link/repo/.sortie/shifts/ID", "../link/repo/.sortie/shifts/ID"):
            with self.subTest(path=str(path)), self.assertRaisesRegex(Refused, "symlink"):
                self.at(self.real / "here", path)

    def test_dot_dot_after_a_symlink_is_refused_because_the_two_readings_part_ways(self):
        # link -> other/repo/.sortie. As written, link/../other is `other` beside the link;
        # the system follows the link first and arrives in other/repo. Neither reading is taken.
        (self.real / "link").symlink_to(self.repo_path / ".sortie")
        for path in ("link/../other/repo/.sortie/shifts/ID",  # a shift only as written
                     "link/../.sortie/shifts/ID"):  # a shift only when the link is followed
            with self.subTest(path=path), self.assertRaises(Refused):
                grants.shift_root(str(self.real / path))

    def test_a_path_through_a_directory_that_is_not_there_is_refused(self):
        # As written, `absent/..` drops out and leaves a real shift; the system cannot walk it.
        for path in ("absent/../other/repo/.sortie/shifts/ID", "other/repo/absent/../.sortie/shifts/ID"):
            with self.subTest(path=path), self.assertRaisesRegex(Refused, "unavailable"):
                grants.shift_root(str(self.real / path))

    def test_the_last_written_part_must_be_the_shift_itself(self):
        # Callers take the id from the end of the path as it was given.
        (self.shift / "logs").mkdir()
        for path in (".sortie/shifts/ID/logs/..", ".sortie/shifts/ID/..", ".sortie/shifts/ID/../ID/logs/.."):
            with self.subTest(path=path), self.assertRaisesRegex(Refused, "end with the shift"):
                grants.shift_root(str(self.repo_path / path))
        for tail in ("/.", "/"):  # joined as text: a Path would drop them before the call
            self.assertEqual(grants.shift_root(str(self.repo_path) + "/.sortie/shifts/ID" + tail), self.repo_path)

    def test_a_symlink_whose_dot_dot_leads_back_to_the_same_place_is_still_a_symlink(self):
        # link -> a directory beside it: as written and as walked, `link/..` is the same place.
        shifts = self.repo_path / ".sortie" / "shifts"
        (shifts / "S2").mkdir()
        (shifts / "link").symlink_to(shifts / "S2")
        (self.repo_path / "src").mkdir()
        (self.repo_path / "side").symlink_to(self.repo_path / "src")
        for path in (".sortie/shifts/link/../ID", "side/../.sortie/shifts/ID"):
            with self.subTest(path=path), self.assertRaisesRegex(Refused, "symlink"):
                grants.shift_root(str(self.repo_path / path))

    def test_a_dot_given_from_inside_the_shift_is_refused_because_it_does_not_name_it(self):
        for path in (".", "", "./"):
            with self.subTest(path=path), self.assertRaisesRegex(Refused, "end with the shift"):
                self.at(self.shift, path)
        self.assertEqual(self.at(self.shift.parent, "ID"), self.repo_path)

    def test_a_path_through_a_file_is_refused(self):
        (self.repo_path / "README.md").write_text("x")
        with self.assertRaises(Refused):
            grants.shift_root(str(self.repo_path / "README.md/../.sortie/shifts/ID"))

    def test_what_is_not_a_shift_directory_is_refused_as_before(self):
        with self.assertRaisesRegex(Refused, "under repo/.sortie/shifts"):
            grants.shift_root(self.repo_path / ".sortie")
        with self.assertRaisesRegex(Refused, "unavailable"):
            grants.shift_root(self.repo_path / ".sortie/shifts/NONE")


class WordingTests(LocalTest):
    def test_the_help_of_start_no_longer_counts_two_seats(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertNotIn("two-seat", out.getvalue())
        self.assertIn("a driver, a builder and a reviewer", " ".join(out.getvalue().split()))

    def test_the_report_head_says_whose_reports_tests_and_reviews_are(self):
        text = " ".join(files("sortie_kit").joinpath("data/templates/closing-report.md").read_text().split())
        self.assertNotIn("Tests and reviews are the builder's reports", text)
        self.assertIn("Tests are the builder's own reports", text)
        self.assertIn("Reviews were run by the host with the signed reviewer, and the host wrote their receipts", text)

    def test_no_wake_is_a_flag_of_a_start_that_runs_the_shift(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            cli.main(["start", "--help"])
        self.assertIn("--no-wake", out.getvalue())
        # With a start that runs no shift it is refused before anything is prepared.
        repo = self.root / "repo"
        repo.mkdir()
        (self.root / "direction.md").write_text("Do it.\n")
        for other in ("--prepare-only", "--sign-only"):
            with self.subTest(other=other), contextlib.redirect_stderr(io.StringIO()) as err:
                code = cli.main(["start", "--repo", str(repo), "--direction", str(self.root / "direction.md"),
                                 "--for", "30m", "--no-wake", other, "--agent", "driver=fixture:m",
                                 "--agent", "builder=fixture:m", "--agent", "reviewer=fixture:fixture-review-model"])
            self.assertNotEqual(code, 0)
            self.assertIn("--no-wake applies only to a start that runs the shift", err.getvalue())
            self.assertFalse((repo / ".sortie").exists())
