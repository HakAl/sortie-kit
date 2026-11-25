"""The host makes one directory for the temporary files of builder turns and reviews,
gives it to both, records it and removes it at its end.

The bases are directories of each test's own, so nothing is made in the
system's short location here; the start that runs the whole command is in
test_start_live.py.
"""
import os
from pathlib import Path
from types import SimpleNamespace

from sortie_kit import lifecycle, receipts, report, turn_temp
from sortie_kit.host import run_shift
from sortie_kit.receipt_schema import decode
import test_host_review
import test_lifecycle

LINE = "- Temporary directory of builder turns and reviews: "


class EnvironmentTests(test_host_review.ReviewFixture):
    def test_the_environment_names_the_directory_given_and_scratch_without_one(self):
        base = dict(HOME="/developer/home", TMPDIR="/developer/own", GIT_DIR="/elsewhere")
        scratch = str(self.shift / "scratch")
        self.assertEqual(lifecycle.builder_env(self.shift, base), dict(
            HOME="/developer/home", TMPDIR=scratch, TMP=scratch, TEMP=scratch, GIT_TERMINAL_PROMPT="0"))
        self.assertEqual(lifecycle.builder_env(self.shift, base, temp="/short/sortie-1"), dict(
            HOME="/developer/home", TMPDIR="/short/sortie-1", TMP="/short/sortie-1", TEMP="/short/sortie-1",
            GIT_TERMINAL_PROMPT="0"))

    def test_a_builder_turn_and_a_review_both_get_the_directory_the_host_holds(self):
        self.driver.temp = SimpleNamespace(path="/short/sortie-1", record=lambda: dict(
            path="/short/sortie-1", bytes=15, state="in use"))
        self.reach_review()
        self.assertGreaterEqual(len(self.spawn.started), 2)
        for started in self.spawn.started:
            with self.subTest(argv=started["argv"][:3]):
                self.assertEqual([started["env"][name] for name in ("TMPDIR", "TMP", "TEMP")], ["/short/sortie-1"] * 3)
        self.assertEqual(self.spawn.started[-1]["argv"][2], "reviewer")

    def test_a_host_given_no_directory_keeps_scratch_and_its_closeout_has_no_entry(self):
        self.begin(running=True)
        self.assertEqual(self.spawn.started[0]["env"]["TMPDIR"], str(self.shift / "scratch"))
        self.end_turn()
        self.driver.finish("done")
        doc = decode((self.shift / "report.json").read_bytes())
        self.assertNotIn("temp", doc["actual_window"])
        self.assertNotIn("Temporary directory", (self.shift / "report.md").read_text())


class HostTests(test_lifecycle.LifecycleFixture):
    def setUp(self):
        super().setUp()
        self.base = self.root / "short"
        self.base.mkdir()
        self.made = []

    def factory(self, *bases):
        def make(repo):
            self.assertEqual(Path(repo), self.target)
            # The ceiling keeps the test apart from whatever checkout its own directory is in.
            self.made.append(turn_temp.make(repo, bases=[str(b) for b in (bases or (self.base,))], ceiling=str(self.root), own=True))
            self.addCleanup(self.made[-1].close)
            return self.made[-1]
        return make

    def shift_of_one_turn(self, factory, wrote=3, closed="exited on its own with status 0", recheck=None, **more):
        """A host loop whose driver briefs one task and then ends. The stand-in builder
        leaves `wrote` entries in its temporary directory."""
        seen = {}
        spawn = self.spawn

        def started(argv, *, cwd, env):
            where = Path(env["TMPDIR"])
            seen.update(existed=where.is_dir(), mode=os.stat(where).st_mode & 0o777)
            for n in range(wrote):
                (where / ("left-" + str(n))).mkdir()
                (where / ("left-" + str(n)) / "file").write_text("x")
            return spawn(argv, cwd=cwd, env=env)
        brief = self.artifact("briefs/task.md")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(dict(
            version=1, id="first", op="brief", task_id="task", kind="feature", brief=brief, next_owner="builder")))
        looks = iter([False, False])
        seat = SimpleNamespace(ended=lambda: next(looks, True), close=lambda: closed,
                               **({} if recheck is None else {"recheck": recheck}))
        code, why = run_shift(self.admission, started, seconds=100, wind_down=10, poll=.01, driver_seat=seat,
                              temp=factory, **more)
        doc = decode((self.shift / "report.json").read_bytes())
        return code, seen, doc["actual_window"], (self.shift / "report.md").read_text()

    def test_the_host_makes_it_gives_it_to_the_turn_records_it_and_removes_it(self):
        code, seen, window, text = self.shift_of_one_turn(self.factory())
        made, = self.made
        path = made.path
        self.assertEqual((code, Path(path).parent, seen), (0, self.base, dict(existed=True, mode=0o700)))
        env = self.spawn.started[0]["env"]
        self.assertEqual([env[name] for name in ("TMPDIR", "TMP", "TEMP")], [path] * 3)
        self.assertFalse(os.path.lexists(path))
        size = len(os.fsencode(path))
        self.assertEqual(window["temp"], dict(path=path, bytes=size, state="removed", entries=3, removed_entries=3))
        self.assertEqual(decode((self.shift / "closeout.json").read_bytes())["temp"], window["temp"])
        self.assertIn(LINE + report.plain(path, 400) + " (" + str(size) + " bytes), outside the repository. The host "
                      "removed it at the end, with 3 entries in it.\n", text)
        self.assertIn("temporary directory of builder turns and reviews: " + path + " (" + str(size) + " bytes)\n",
                      (self.shift / "logs" / "host.log").read_text())
        # Not a shutdown problem and nothing for "Not done".
        self.assertEqual(window["processes"]["shutdown"], dict(confirmed=True, groups=[], problems=[]))

    def test_no_usable_base_keeps_scratch_and_the_report_says_what_that_can_break(self):
        code, seen, window, text = self.shift_of_one_turn(self.factory(self.target), wrote=0)
        self.assertEqual(code, 0)
        self.assertEqual(self.spawn.started[0]["env"]["TMPDIR"], str(self.shift / "scratch"))
        self.assertEqual((window["temp"]["state"], window["temp"]["path"]), ("not made", None))
        self.assertIn("- **Temporary directory of builder turns and reviews: not made** (no directory outside the "
                      "repository and outside any Git checkout could be used). They kept their temporary files in "
                      "the shift's scratch/, inside the repository: tests that bind Unix sockets under the temporary "
                      "directory, or that must run outside a Git checkout, can fail for that alone.\n", text)
        self.assertIn("not made, the shift's scratch/ is used", (self.shift / "logs" / "host.log").read_text())

    def test_a_turn_not_seen_to_stop_leaves_the_directory_and_the_report_says_where(self):
        self.spawn.lingering = True  # a descendant keeps the group alive
        code, seen, window, text = self.shift_of_one_turn(self.factory())
        path = self.made[0].path
        self.assertTrue((Path(path) / "left-0" / "file").exists())
        kept = window["temp"]
        self.assertEqual((kept["state"], kept["entries"], kept["removed_entries"]), ("left", None, 0))
        self.assertIn("- **Temporary directory of builder turns and reviews: LEFT.** The host made it at "
                      + report.plain(path, 400), text)
        self.assertIn("a builder turn or a review was not seen to stop and may still write there. How many entries it held was "
                      "not counted. Nothing of the shift points into it. The host may not have checked that the path "
                      "still names that directory, so look at what is there before you remove anything.", text)
        self.assertNotIn("by hand", text)

    def driver_not_seen_to_stop(self, closed):
        # The driver runs as the same user and can read the path in the host's log.
        code, seen, window, text = self.shift_of_one_turn(self.factory(), closed=closed)
        kept = window["temp"]
        self.assertEqual((kept["state"], kept["entries"], kept["removed_entries"]), ("left", None, 0))
        self.assertEqual(kept["why"], "the driver process was not seen to stop and may still write there")
        self.assertTrue((Path(self.made[0].path) / "left-0" / "file").exists())
        self.assertIn("the driver process was not seen to stop", " ".join(window["processes"]["shutdown"]["problems"]))

    def test_a_driver_seen_to_be_gone_at_a_second_look_does_not_keep_the_directory(self):
        code, seen, window, text = self.shift_of_one_turn(
            self.factory(), closed="stop not observed: still exiting", recheck=lambda: "exited after its stop with status 0")
        self.assertEqual(window["temp"]["state"], "removed", window["temp"])
        self.assertEqual(window["processes"]["shutdown"]["problems"], [])

    def test_a_driver_whose_stop_was_not_observed_leaves_the_directory_untouched(self):
        self.driver_not_seen_to_stop("stop not observed: still exiting after SIGKILL")

    def test_a_driver_whose_stop_was_not_confirmed_leaves_the_directory_untouched(self):
        self.driver_not_seen_to_stop("stop not confirmed: no such process group")

    def test_a_removal_that_fails_changes_neither_the_exit_status_nor_the_shutdown_line(self):
        factory = self.factory()

        def make(repo):
            made = factory(repo)
            remove = made.remove
            made.remove = lambda seconds, **how: remove(0, **how)  # as when the report's time is all that is left
            return made
        code, seen, window, text = self.shift_of_one_turn(make)
        self.assertEqual((code, window["temp"]["state"], window["temp"]["why"]),
                         (0, "left", "no time was left for the removal"))
        self.assertEqual(window["processes"]["shutdown"], dict(confirmed=True, groups=[], problems=[]))
        self.assertIn("- Shutdown: confirmed.", text)
        self.assertIn("- **Temporary directory of builder turns and reviews: LEFT.** The host made it at ", text)
        self.assertTrue(os.path.isdir(self.made[0].path))
        self.assertIsNone(self.made[0].fd)  # the handles are let go all the same

    def test_the_directory_is_removed_before_the_look_at_the_repository(self):
        from unittest.mock import patch
        from sortie_kit import repo_state
        observe, order = repo_state.observe, []

        def looked(*args, **how):
            if "since" in how:  # the look at the end
                order.append(("look", self.made[0].state))
            return observe(*args, **how)
        with patch.object(repo_state, "observe", looked):
            self.shift_of_one_turn(self.factory())
        self.assertEqual(order, [("look", "removed")])

    def test_removal_is_given_no_time_the_report_needs(self):
        given = []
        factory = self.factory()

        def make(repo):
            made = factory(repo)
            remove = made.remove
            made.remove = lambda seconds, **how: (given.append(seconds), remove(seconds, **how))[1]
            return made
        from sortie_kit import host
        self.shift_of_one_turn(make)
        self.assertEqual(len(given), 1)
        # Finalization is five seconds at most, whatever the grant gives it.
        self.assertTrue(0 < given[0] <= 5 - host.REPORT_RESERVE, given)

    def test_with_no_more_than_the_reports_own_time_left_the_removal_gets_none(self):
        from unittest.mock import patch
        from sortie_kit import host
        final = lifecycle.Driver.final_seconds
        with patch.object(lifecycle.Driver, "final_seconds", lambda driver: min(final(driver), host.REPORT_RESERVE)):
            code, seen, window, text = self.shift_of_one_turn(self.factory())
        self.assertEqual((window["temp"]["state"], window["temp"]["why"]), ("left", "no time was left for the removal"))
        self.assertTrue(os.path.isdir(self.made[0].path))

    def test_a_report_written_while_the_shift_runs_says_the_directory_is_in_use(self):
        self.driver.temp = self.factory()(self.target)
        self.begin()
        self.assertEqual(self.action("finish", reason="done early", next_owner="developer")["status"], "closed")
        text = (self.shift / "report.md").read_text()
        path = self.made[0].path
        self.assertIn(LINE + report.plain(path, 400) + " (" + str(len(os.fsencode(path))) + " bytes), outside the "
                      "repository. The host removes it at its end.\n", text)
        self.made[0].remove(5.0)


class ReportWordsTests(test_lifecycle.LifecycleFixture):
    def line(self, entry):
        self.driver.temp = SimpleNamespace(path=None, record=lambda: entry)
        self.driver.finish("done")
        lines = [l for l in (self.shift / "report.md").read_text().splitlines() if "Temporary directory" in l]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def test_what_was_left_is_said_with_what_went_and_why(self):
        left = dict(path="/short/sortie-1", bytes=15, state="left", entries=170, removed_entries=42,
                    why="the time for the removal ran out")
        self.assertEqual(self.line(left), "- **Temporary directory of builder turns and reviews: LEFT.** The host "
                         "made it at /short/sortie-1 (15 bytes), outside the repository: the time for the removal "
                         "ran out. 42 of 170 entries in it were removed. Nothing of the shift points into it. The "
                         "host may not have checked that the path still names that directory, so look at what is "
                         "there before you remove anything.")
        self.assertIn("An unknown number of 170 entries in it were removed",
                      self.line(dict(left, removed_entries=None)))

    def test_a_replaced_path_is_said_as_that_and_nobody_is_told_to_remove_it(self):
        line = self.line(dict(path="/short/sortie-1", bytes=15, state="left", entries=2, removed_entries=2, replaced=True,
                              why="the path is no longer the directory the host made; what the host made was emptied"))
        self.assertEqual(line, "- **Temporary directory of builder turns and reviews: REPLACED.** The host made it at "
                         "/short/sortie-1 (15 bytes), outside the repository, and that path was then seen not to name "
                         "it: the path is no longer the directory the host made; what the host made was emptied. "
                         "2 of 2 entries in it were removed. Whatever is at that path now is not what the host made "
                         "and the host did not look into it; this report is no reason to remove it.")
        self.assertNotIn("by hand", line)
        # Only the host's own true counts as that: anything else in the field reads as a plain left.
        self.assertIn("LEFT", self.line(dict(path="/short/sortie-1", bytes=15, state="left", entries=2,
                                             removed_entries=2, replaced="yes", why="x")))

    def test_one_entry_and_none_are_said_in_plain_words(self):
        removed = dict(path="/short/sortie-1", bytes=15, state="removed", entries=1, removed_entries=1)
        self.assertTrue(self.line(removed).endswith("The host removed it at the end, with 1 entry in it."))
        self.assertTrue(self.line(dict(removed, entries=0, removed_entries=0)).endswith(
            "The host removed it at the end, with 0 entries in it."))
        self.assertTrue(self.line(dict(removed, entries=None)).endswith("The host removed it at the end."))
        self.assertTrue(self.line(dict(removed, unverified=True)).endswith(
            "with 1 entry in it. It was removed by its name, and this system gave no way to see afterwards that "
            "what went was the directory the host made."))

    def test_an_entry_of_another_shape_or_with_hostile_text_stays_inert_and_costs_no_report(self):
        for entry in ("yes", ["x"], dict(state="gone"), dict(state="removed", path=5, bytes="many"),
                      dict(state="left", path="/p", bytes=True)):
            with self.subTest(entry=entry):
                self.assertEqual(self.line(entry), "- Temporary directory of builder turns and reviews: not known; "
                                 "the closeout's entry on it could not be read.")
        line = self.line(dict(state="left", path="/p/<script>x</script>\n## Heading", bytes=9, entries="x",
                              removed_entries=-4, why="**bold** <b>why</b>"))
        self.assertNotIn("<script>", line)
        self.assertNotIn("<b>", line)
        self.assertIn("How many entries it held was not counted", line)
        self.assertIn("## Outcome", (self.shift / "report.md").read_text())
