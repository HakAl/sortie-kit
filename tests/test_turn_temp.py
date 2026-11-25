"""The directory the host makes for the temporary files of builder turns and reviews.

Every base here is a directory of the test's own; nothing is made in the
system's short location except by the one test that asks which base a real
shift would take, and that test makes nothing.
"""
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from sortie_kit import turn_temp


class TurnTempTests(unittest.TestCase):
    def setUp(self):
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        self.root = Path(os.path.realpath(held.name))
        self.repo, self.base = self.root / "repo", self.root / "base"
        self.repo.mkdir(); self.base.mkdir()
        (self.repo / ".git").mkdir()

    def make(self, *bases):
        # The ceiling keeps the test apart from whatever checkout its own temporary directory is in.
        made = turn_temp.make(self.repo, bases=[str(b) for b in (bases or (self.base,))], ceiling=str(self.root), own=True)
        self.addCleanup(made.close)
        return made

    def test_made_in_the_first_base_for_the_owner_alone_and_recorded_as_in_use(self):
        made = self.make()
        path = Path(made.path)
        self.assertEqual(path.parent, self.base)
        self.assertRegex(path.name, r"^sortie-[0-9a-f]{8}$")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
        self.assertEqual(made.record(), dict(path=str(path), bytes=len(os.fsencode(str(path))), state="in use"))

    def test_a_base_in_the_repository_or_in_any_git_checkout_is_passed_over(self):
        inside, checkout = self.repo / "tmp", self.root / "other" / "deep"
        inside.mkdir(); checkout.mkdir(parents=True)
        (self.root / "other" / ".git").write_text("gitdir: elsewhere\n")  # a linked working tree has a file
        made = self.make(inside, self.repo, checkout, self.root / "missing", self.base)
        self.assertEqual(Path(made.path).parent, self.base)
        self.assertEqual(list(inside.iterdir()) + list(checkout.iterdir()), [])
        self.assertEqual(turn_temp.base(self.repo, [str(inside), str(self.base)], ceiling=str(self.root), own=True), str(self.base))

    def test_a_base_reached_through_a_link_into_the_repository_is_passed_over(self):
        (self.repo / "tmp").mkdir()
        link = self.root / "link"
        link.symlink_to(self.repo / "tmp")
        made = self.make(link)
        self.assertIsNone(made.path)

    def test_a_base_others_can_write_is_taken_only_when_it_is_sticky(self):
        loose, sticky = self.root / "loose", self.root / "sticky"
        loose.mkdir(); sticky.mkdir()
        os.chmod(loose, 0o777)
        os.chmod(sticky, 0o1777)
        self.assertEqual(turn_temp.usable(self.repo, [str(loose), str(sticky)], ceiling=str(self.root), own=True), [str(sticky)])
        os.chmod(loose, 0o775)
        self.assertEqual(turn_temp.usable(self.repo, [str(loose)], ceiling=str(self.root), own=True), [])
        made = self.make(loose, sticky)
        self.assertEqual(Path(made.path).parent, sticky)
        made.remove(5.0)
        self.assertEqual(made.record()["state"], "removed")

    def test_a_base_that_cannot_take_a_directory_is_passed_over_for_the_next(self):
        from unittest.mock import patch
        real, asked = os.mkdir, []

        def mkdir(name, mode=0o777, *, dir_fd=None):
            asked.append(name)
            if len(asked) == 1:
                raise OSError(28, "No space left on device")
            return real(name, mode, dir_fd=dir_fd)
        other = self.root / "other-base"
        other.mkdir()
        with patch.object(turn_temp.os, "mkdir", mkdir):
            made = self.make(self.base, other)
        self.assertEqual((len(asked), Path(made.path).parent, list(self.base.iterdir())), (2, other, []))
        with patch.object(turn_temp.os, "mkdir", side_effect=OSError(30, "Read-only file system")):
            none = self.make(self.base)
        self.assertEqual(none.record()["state"], "not made")
        self.assertIn("Read-only file system", none.record()["why"])

    def test_directories_nested_deeper_than_the_bound_stop_the_removal_and_are_said(self):
        made = self.make()
        deep = Path(made.path)
        for n in range(4):
            deep = deep / "d"
        deep.mkdir(parents=True)
        from unittest.mock import patch
        with patch.object(turn_temp, "DEPTH", 3):
            made.remove(5.0)
        record = made.record()
        self.assertEqual((record["state"], record["entries"], record["removed_entries"]), ("left", 1, 0))
        self.assertIn("nested deeper than 3", record["why"])
        self.assertIsNone(made.fd)

    def test_a_host_takes_no_base_of_the_users_own_since_it_cannot_read_who_else_may_rename_in_it(self):
        # Mode bits do not show an access list, and only root can put one on what is root's.
        self.assertEqual(turn_temp.usable(self.repo, [str(self.base)], ceiling=str(self.root)), [])
        made = turn_temp.make(self.repo, bases=[str(self.base)], ceiling=str(self.root))
        self.addCleanup(made.close)
        self.assertEqual((made.record()["state"], list(self.base.iterdir())), ("not made", []))
        self.assertIn("scratch", turn_temp.words(self.repo, bases=[str(self.base)], ceiling=str(self.root)))
        short = os.path.realpath(turn_temp.BASES[0])
        info = os.stat(short)
        if info.st_uid == 0 and info.st_mode & 0o1000:  # as on a usual machine
            self.assertTrue(turn_temp.guarded(info))
            self.assertTrue(turn_temp.anchored(short))

    def test_work_that_comes_back_after_it_was_given_up_on_changes_nothing_more(self):
        import threading
        import time
        from unittest.mock import patch
        made = self.make()
        for n in range(3):
            (Path(made.path) / str(n)).write_text("x")
        hold, real = threading.Event(), os.lstat

        def slow(name, *, dir_fd=None):  # the look before an entry is taken does not come back in time
            hold.wait(20)
            return real(name, dir_fd=dir_fd)
        self.addCleanup(hold.set)
        with patch.object(turn_temp.os, "lstat", slow):
            made.remove(0.05)
            record = made.record()
            hold.set()
            deadline = time.monotonic() + 5
            while threading.active_count() > 1 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual((record["state"], record["entries"], record["removed_entries"]), ("left", None, None))
        self.assertEqual(sorted(os.listdir(made.path)), ["0", "1", "2"])

    def test_a_removal_this_system_cannot_look_back_at_is_recorded_as_unverified(self):
        from unittest.mock import patch
        made = self.make()
        with patch.object(turn_temp, "vanished", lambda fd, same: None):
            made.remove(5.0)
        record = made.record()
        self.assertEqual((record["state"], record.get("unverified")), ("removed", True), record)
        self.assertFalse(os.path.lexists(made.path))

    def test_whether_the_hosts_own_directory_is_gone_is_seen_by_reading_only(self):
        made = self.make()
        path, moved = Path(made.path), self.base / "moved"
        self.assertIs(turn_temp.vanished(made.fd, made.same), False)
        path.rename(moved)
        self.assertIs(turn_temp.vanished(made.fd, made.same), False)
        self.assertEqual(list(moved.iterdir()), [])  # nothing was made in it to find out
        moved.rmdir()
        self.assertIs(turn_temp.vanished(made.fd, made.same), True)

    def test_no_base_gives_no_directory_and_says_why(self):
        made = self.make(self.repo, self.root / "missing")
        self.assertIsNone(made.path)
        record = made.record()
        self.assertEqual((record["state"], record["path"]), ("not made", None))
        self.assertIn("no directory outside the repository and outside any Git checkout", record["why"])
        self.assertIsNone(turn_temp.base(self.repo, [str(self.repo)], ceiling=str(self.root), own=True))
        made.remove(1.0)  # nothing to remove, and nothing raised
        self.assertEqual(made.record()["state"], "not made")

    def test_the_bases_of_a_real_shift_start_at_the_short_system_location(self):
        first = turn_temp.BASES[0]
        self.assertEqual(first, os.path.join(os.sep, "tmp"))
        # The longest path under it: its real path, a separator, the prefix and eight characters.
        self.assertLessEqual(len(os.fsencode(os.path.realpath(first))) + 1 + len("sortie-") + 8, 28)

    def test_git_finds_no_checkout_in_the_directory(self):
        made = self.make()
        env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(self.root))
        found = subprocess.run(["git", "-C", made.path, "rev-parse", "--show-toplevel"], capture_output=True, env=env)
        self.assertNotEqual(found.returncode, 0, found.stdout)

    def test_a_checkout_above_the_ceiling_is_not_looked_at_and_one_below_it_is(self):
        (self.root / ".git").mkdir()
        self.assertEqual(turn_temp.base(self.repo, [str(self.base)], ceiling=str(self.root), own=True), str(self.base))
        self.assertIsNone(turn_temp.base(self.repo, [str(self.base)], own=True))

    def test_removal_counts_the_entries_and_takes_the_directory(self):
        made = self.make()
        path = Path(made.path)
        for n in range(3):
            (path / ("dir-" + str(n)) / "deep").mkdir(parents=True)
            (path / ("dir-" + str(n)) / "deep" / "file").write_text("x")
        (path / "file").write_text("x")
        made.remove(5.0)
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(made.record(), dict(path=str(path), bytes=len(os.fsencode(str(path))), state="removed",
                                             entries=4, removed_entries=4))

    def test_removal_follows_no_link(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "kept").write_text("kept")
        made = self.make()
        (Path(made.path) / "to-dir").symlink_to(outside)
        (Path(made.path) / "to-file").symlink_to(outside / "kept")
        made.remove(5.0)
        self.assertEqual(made.record()["state"], "removed")
        self.assertEqual((outside / "kept").read_text(), "kept")

    def test_a_socket_and_a_read_only_directory_are_removed(self):
        made = self.make()
        bound = socket.socket(socket.AF_UNIX)
        self.addCleanup(bound.close)
        bound.bind(os.path.join(made.path, "s"))
        closed = Path(made.path) / "closed"
        (closed / "in").mkdir(parents=True)
        os.chmod(closed / "in", 0o500)
        os.chmod(closed, 0o500)
        made.remove(5.0)
        self.assertEqual(made.record()["state"], "removed", made.record())

    def test_the_bases_after_the_short_one_are_read_out_of_the_environment_and_nothing_is_probed(self):
        from unittest.mock import patch
        named = dict(TMPDIR=str(self.base), TEMP="", TMP="relative/place")
        self.assertEqual(turn_temp.candidates(None, named), list(turn_temp.BASES) + [str(self.base), "relative/place"])
        self.assertEqual(turn_temp.candidates(None, {}), list(turn_temp.BASES))
        # Python finds its own temporary directory by writing a file into each place it
        # tries. The signer's line is made before anything is signed and must not do that.
        inside = self.repo / "tmp"
        inside.mkdir()
        with patch.dict(os.environ, dict(TMPDIR=str(inside), TEMP="", TMP="")), patch.object(tempfile, "tempdir", None), \
                patch.object(tempfile, "_get_default_tempdir", side_effect=AssertionError("probed")), \
                patch.object(turn_temp, "BASES", ()):
            self.assertIn("scratch", turn_temp.words(self.repo))
            self.assertIsNone(turn_temp.base(self.repo))
            made = turn_temp.make(self.repo)
        self.assertEqual(made.record()["state"], "not made")
        self.assertEqual(list(inside.iterdir()), [])

    def test_a_base_below_a_directory_another_user_could_rename_in_is_passed_over(self):
        above = self.root / "above"
        (above / "base").mkdir(parents=True)
        os.chmod(above, 0o777)
        self.assertEqual(turn_temp.usable(self.repo, [str(above / "base")], ceiling=str(self.root), own=True), [])
        os.chmod(above, 0o1777)  # sticky: only the owner of an entry renames it
        self.assertEqual(turn_temp.usable(self.repo, [str(above / "base")], ceiling=str(self.root), own=True), [str(above / "base")])
        os.chmod(above, 0o755)
        self.assertEqual(turn_temp.usable(self.repo, [str(above / "base")], ceiling=str(self.root), own=True), [str(above / "base")])

    def test_a_directory_that_could_not_be_opened_after_it_was_made_is_left_and_said(self):
        from unittest.mock import patch
        real = os.open

        def opened(name, flags, mode=0o777, *, dir_fd=None):
            if str(name).startswith(turn_temp.PREFIX):
                raise OSError(24, "Too many open files")
            return real(name, flags, mode, dir_fd=dir_fd)
        with patch.object(turn_temp.os, "open", opened):
            made = self.make()
        record = made.record()
        self.assertEqual(record["state"], "not made")
        # Not removed by a name whose identity was never seen.
        left, = self.base.iterdir()
        self.assertIn(left.name + " was left there", record["why"])

    def test_a_removal_that_does_not_come_back_is_not_waited_for_and_is_said_as_not_seen_to_finish(self):
        import threading
        import time
        from unittest.mock import patch
        made = self.make()
        (Path(made.path) / "file").write_text("x")
        hold, listed = threading.Event(), turn_temp.listed

        def stuck(fd, end, clock):  # one call into a file system that does not answer
            hold.wait(20)
            return listed(fd, end, clock)
        self.addCleanup(hold.set)
        began = time.monotonic()
        with patch.object(turn_temp, "listed", stuck):
            made.remove(0.05)
            took = time.monotonic() - began
            record = made.record()
            self.assertLess(took, 2.0)
            self.assertEqual((record["state"], record["entries"], record["removed_entries"]), ("left", None, None))
            self.assertIn("not seen to finish", record["why"])
            self.assertIsNone(made.fd)  # the handles are the work's own now, not closed under it
            hold.set()
            deadline = time.monotonic() + 5
            while threading.active_count() > 1 and time.monotonic() < deadline:
                time.sleep(0.01)
        # Told to stop, the work took nothing more once it came back.
        self.assertTrue((Path(made.path) / "file").exists())

    def test_an_empty_directory_that_took_the_name_as_it_was_removed_is_said_as_replaced(self):
        from unittest.mock import patch
        made = self.make()
        path, moved = Path(made.path), self.base / "moved"
        real = os.rmdir

        def rmdir(name, *, dir_fd=None):
            if name == path.name:  # between the look at the name and its removal
                path.rename(moved)
                path.mkdir()
            return real(name, dir_fd=dir_fd)
        with patch.object(turn_temp.os, "rmdir", rmdir):
            made.remove(5.0)
        record = made.record()
        self.assertEqual((record["state"], record.get("replaced")), ("left", True), record)
        self.assertIn("removed in its place", record["why"])
        self.assertEqual(list(moved.iterdir()), [])  # found out by reading: nothing was made in it

    def test_a_name_that_changed_so_that_it_cannot_be_removed_is_said_as_replaced(self):
        from unittest.mock import patch
        made = self.make()
        path, moved = Path(made.path), self.base / "moved"
        real = os.rmdir

        def rmdir(name, *, dir_fd=None):
            if name == path.name:  # between the look at the name and its removal
                path.rename(moved)
                path.write_text("a file of someone's")
            return real(name, dir_fd=dir_fd)
        with patch.object(turn_temp.os, "rmdir", rmdir):
            made.remove(5.0)
        record = made.record()
        self.assertEqual((record["state"], record.get("replaced")), ("left", True), record)
        self.assertEqual(path.read_text(), "a file of someone's")

    def test_a_removal_given_up_on_reads_nothing_from_work_that_still_runs(self):
        import threading
        from unittest.mock import patch
        made = self.make()
        for n in range(3):
            (Path(made.path) / str(n)).write_text("x")
        hold, clear = threading.Event(), turn_temp.clear

        def stuck(parent, name, end, clock, device, depth):  # after the listing, with entries counted
            hold.wait(20)
            return clear(parent, name, end, clock, device, depth)
        self.addCleanup(hold.set)
        with patch.object(turn_temp, "clear", stuck):
            made.remove(0.05)
            record = made.record()
            hold.set()
        self.assertEqual((record["state"], record["entries"], record["removed_entries"], "replaced" in record),
                         ("left", None, None, False))

    def test_a_name_that_is_gone_is_said_as_replaced_and_not_as_removed(self):
        made = self.make()
        Path(made.path).rename(self.base / "moved")
        made.remove(5.0)
        record = made.record()
        self.assertEqual((record["state"], record.get("replaced")), ("left", True), record)
        self.assertIn("no longer names anything", record["why"])

    def test_a_directory_whose_own_permissions_were_taken_away_is_removed(self):
        made = self.make()
        (Path(made.path) / "file").write_text("x")
        os.chmod(made.path, 0o500)
        made.remove(5.0)
        self.assertEqual(made.record()["state"], "removed", made.record())

    def test_an_entry_gone_by_itself_since_the_listing_is_not_read_as_a_replaced_directory(self):
        from unittest.mock import patch
        made = self.make()
        for n in range(3):
            (Path(made.path) / str(n)).write_text("x")
        listed = turn_temp.listed

        def then_one_goes(fd, end, clock):
            names = listed(fd, end, clock)
            os.unlink(os.path.join(made.path, names[0]))
            return names
        with patch.object(turn_temp, "listed", then_one_goes):
            made.remove(5.0)
        self.assertEqual(made.record()["state"], "removed", made.record())

    def test_a_listing_is_cut_by_the_time_entry_by_entry(self):
        made = self.make()
        for n in range(600):
            (Path(made.path) / str(n)).write_text("")
        ticks = iter([0.0, 0.0] + [9.0] * 5)
        made.remove(1.0, clock=lambda: next(ticks))
        record = made.record()
        self.assertEqual((record["state"], record["entries"], record["removed_entries"]), ("left", None, 0))
        self.assertIn("time", record["why"])
        self.assertEqual(len(os.listdir(made.path)), 600)

    def test_no_time_leaves_the_directory_and_says_where_and_why(self):
        made = self.make()
        (Path(made.path) / "file").write_text("x")
        made.remove(0)
        record = made.record()
        self.assertEqual((record["state"], record["entries"], record["removed_entries"]), ("left", None, 0))
        self.assertIn("no time", record["why"])
        self.assertTrue((Path(made.path) / "file").exists())

    def test_time_that_runs_out_midway_leaves_the_rest_and_counts_what_went(self):
        made = self.make()
        for n in range(5):
            (Path(made.path) / str(n)).write_text("x")
        ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0] + [9.0] * 50)  # the end, the start, the listing, and two looks an entry
        made.remove(1.0, clock=lambda: next(ticks))
        record = made.record()
        self.assertEqual((record["state"], record["entries"]), ("left", 5))
        self.assertLess(record["removed_entries"], 5)
        self.assertIn("time", record["why"])
        self.assertTrue(os.path.isdir(made.path))

    def test_a_turn_not_seen_to_stop_leaves_the_directory_untouched(self):
        made = self.make()
        (Path(made.path) / "file").write_text("x")
        made.remove(5.0, running="a builder turn was not seen to stop")
        record = made.record()
        self.assertEqual((record["state"], record["removed_entries"]), ("left", 0))
        self.assertIn("not seen to stop", record["why"])
        self.assertTrue((Path(made.path) / "file").exists())

    def test_a_name_that_now_is_another_directory_is_not_removed(self):
        made = self.make()
        path = Path(made.path)
        moved = self.base / "moved"
        path.rename(moved)
        path.mkdir()
        (path / "theirs").write_text("theirs")
        (moved / "ours").write_text("ours")
        made.remove(5.0)
        record = made.record()
        self.assertEqual((record["state"], record.get("replaced")), ("left", True))
        self.assertIn("no longer the directory the host made", record["why"])
        self.assertEqual((path / "theirs").read_text(), "theirs")
        self.assertFalse((moved / "ours").exists())  # what the host made is emptied through its own handle

    def test_the_line_for_the_signer_names_the_base_and_that_nothing_of_it_is_signed(self):
        line = turn_temp.words(self.repo, bases=[str(self.base)], ceiling=str(self.root), own=True)
        self.assertTrue(line.startswith("Temporary files: "), line)
        self.assertIn(str(self.base), line)
        self.assertIn("outside the repository", line)
        # Neither the place nor the removal is promised: both are decided when the shift runs.
        self.assertIn("As things are now that would be under " + str(self.base) + ";", line)
        self.assertIn("may be another", line)
        self.assertIn("tries to remove it", line)
        self.assertNotIn("removes it", line)
        none = turn_temp.words(self.repo, bases=[str(self.repo)], ceiling=str(self.root), own=True)
        self.assertIn("scratch", none)
        self.assertTrue(all(ord(c) < 128 for c in line + none))


if __name__ == "__main__":
    unittest.main()
