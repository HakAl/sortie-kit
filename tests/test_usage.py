"""What a seat's CLI stated of the tokens it used: the rule for the text form of codex,
what the host records of a builder turn and of a review attempt, and the Usage section
of the report.

The fragments are shaped as the ends of real logs of two kept shifts, cut small, with
paths and ids made neutral: a builder's merged output, where the final answer is printed
once more after the pair; a reviewer's stderr, which ends in the pair; and a builder log
that holds the pair of an earlier turn in its body.
"""
import hashlib
import os
import unittest
from unittest import mock

from sortie_kit import cli_words, receipts, report, shift_wait, usage
from sortie_kit.layout import Elapsed
from sortie_kit.lifecycle import BUILDER_LOG, Driver, TURN_LOG_LIMIT
from sortie_kit.receipt_schema import decode
from test_host_review import ReviewFixture

FORM = usage.CODEX_TEXT
BUILDER_END = (b"codex\nPlan round 0 receipt written: `receipts/plan-example.json`.\n\n"
               b"Baseline evidence preserved; no implementation changes. Stopping this turn for driver handoff.\n"
               b"tokens used\n29,528\n"
               b"Plan round 0 receipt written: `receipts/plan-example.json`.\n\n"
               b"Baseline evidence preserved; no implementation changes. Stopping this turn for driver handoff.\n")
REVIEW_END = (b'    "No code or tests were executed, as required."\n  ]\n}\n'
              b"=====END SORTIE REVIEW 0123456789abcdef=====\ntokens used\n47,966\n")
# The transcript of a turn that read the log of the turn before it, and then its own pair.
TWICE = (b"exec\n/bin/zsh -lc 'tail -n 3 logs/builder-turn-example-1.log'\n succeeded in 0ms:\n"
         b"Acknowledgment phase complete; stopping this turn as instructed.\ntokens used\n15,558\n"
         b"codex\nPlan written.\ntokens used\n29,528\nPlan written.\n")


def stated(total):
    return dict(form=FORM, stated=dict(total=total))


def missing(reason, form=FORM):
    return dict(not_reported=reason, **(dict(form=form) if form else {}))


class RuleTests(unittest.TestCase):
    def text(self, raw, wrapped=False):
        return usage.codex_text(raw, wrapped)

    def test_the_real_shapes_give_the_figure_the_cli_stated(self):
        self.assertEqual(self.text(BUILDER_END), stated(29528))
        self.assertEqual(self.text(REVIEW_END), stated(47966))
        self.assertEqual(self.text(b"tokens used\n999"), stated(999))  # no line end after the number
        self.assertEqual(self.text(b"tokens used\r\n1,000,000,000,000\r\n"), stated(10 ** 12))

    def test_of_a_log_with_the_marker_twice_the_last_pair_is_taken(self):
        self.assertEqual(self.text(TWICE), stated(29528))

    def test_no_marker_and_a_marker_with_no_number_line_are_no_figure(self):
        for raw in (b"", b"the answer alone\n", b"tokens used", b"tokens used\n", b"words\ntokens used\n\n15,558\n"):
            with self.subTest(raw=raw):
                self.assertEqual(self.text(raw), missing("no_figure"))
        self.assertEqual(self.text(None), missing("no_figure"))

    def test_a_last_marker_followed_by_words_is_not_reported_and_an_earlier_pair_is_never_taken_for_it(self):
        raw = b"tokens used\n15,558\nThe log ends with the lines\ntokens used\nand a number, as I said.\n"
        self.assertEqual(self.text(raw), missing("malformed"))

    def test_a_line_that_only_holds_the_marker_among_other_characters_is_no_marker(self):
        for line in (b"  tokens used", b"tokens used ", b"> tokens used", b"tokens used:", b"Tokens used"):
            with self.subTest(line=line):
                self.assertEqual(self.text(b"tokens used\n15,558\n" + line + b"\nwords\n"), stated(15558))
        self.assertEqual(self.text(b"tokens used\n 15,558\n"), missing("malformed"))

    def test_a_number_line_of_another_shape_or_out_of_bounds_is_malformed_and_nothing_of_it_is_kept(self):
        for line in ("1,,2", "1,23", "12,3456", ",123", "1.5", "-5", "+5", "0x10", "1e9", "15,558 tokens", "١٢٣",
                     "1" * 18, "1" * 60000, str(10 ** 12 + 1), "1,000,000,000,001"):
            with self.subTest(line=line[:30]):
                self.assertEqual(self.text(("tokens used\n" + line + "\n").encode()), missing("malformed"))
        self.assertEqual(self.text(b"tokens used\n0\n"), stated(0))  # a stated 0 is a figure

    def test_a_pair_cut_in_the_middle_by_the_window_is_not_taken(self):
        # The marker was let go at the front and the number line is what the window begins with.
        self.assertEqual(self.text(b"15,558\nthe final answer\n", wrapped=True), missing("no_figure"))
        self.assertEqual(self.text(b"ens used\n15,558\nanswer\n", wrapped=True), missing("no_figure"))

    def test_the_first_line_of_a_window_that_wrapped_is_dropped_as_it_may_be_half_a_line(self):
        # "not tokens used" lost its beginning: what is left reads as the marker and is none.
        self.assertEqual(self.text(b"tokens used\n15,558\nanswer\n", wrapped=True), missing("no_figure"))
        self.assertEqual(self.text(b"tokens used\n15,558\nanswer\n"), stated(15558))
        held = None
        for chunk in (b"x" * usage.TAIL, b"y" * 10 + b"\ntokens used\n7\n"):
            held = usage.roll(held, chunk)
        self.assertEqual((len(held[0]), held[1]), (usage.TAIL, True))
        self.assertEqual(usage.read(FORM, held, ended=True, whole=True), stated(7))

    def test_an_answer_longer_than_the_window_after_the_pair_loses_it(self):
        held = None
        for chunk in (b"tokens used\n15,558\n", b"a line of the final answer\n" * (usage.TAIL // 27 + 2)):
            held = usage.roll(held, chunk)
        self.assertEqual(usage.read(FORM, held, ended=True, whole=True), missing("no_figure"))
        # And more than the window, handed over at once, is looked at from its end only.
        self.assertEqual(self.text(b"tokens used\n15,558\n" + b"z" * usage.TAIL + b"\n"), missing("no_figure"))

    def test_bytes_that_are_no_text_do_not_stop_the_reading(self):
        self.assertEqual(self.text(b"\xff\xfe\x00\n\xc3\ntokens used\n15,558\n\xff answer\n"), stated(15558))
        self.assertEqual(self.text(b"tokens used\n15\xff558\n"), missing("malformed"))

    def test_the_form_is_that_of_the_signed_seat_and_only_the_text_form_of_codex_exec_is_known(self):
        seat = dict(agent="codex", argv=["exec", "--sandbox", "read-only", "{prompt}"], model="m")
        self.assertEqual(usage.form(seat), FORM)
        for other in (dict(seat, agent="claude"), dict(seat, argv=["exec", "--json", "{prompt}"]),
                      dict(seat, argv=["review", "{prompt}"]), dict(seat, argv=[]), dict(seat, argv="exec"),
                      dict(agent="codex"), None, "codex", []):
            with self.subTest(other=other):
                self.assertIsNone(usage.form(other))

    def test_a_run_that_did_not_end_well_or_whose_end_was_not_seen_has_no_figure_whatever_it_printed(self):
        held = usage.roll(None, BUILDER_END)
        self.assertEqual(usage.read(FORM, held, ended=True, whole=True), stated(29528))
        self.assertEqual(usage.read(FORM, held, ended=False, whole=True), missing("not_ended"))
        self.assertEqual(usage.read(FORM, held, ended=True, whole=False), missing("no_end"))
        self.assertEqual(usage.read(None, held, ended=True, whole=True), missing("no_form", None))
        self.assertEqual(usage.read("another-form", held, ended=True, whole=True), missing("no_form", None))
        self.assertEqual(usage.read(FORM, None, ended=True, whole=True), missing("no_figure"))

    def test_only_the_shapes_the_reader_gives_are_taken_from_a_state(self):
        for good in (stated(0), stated(10 ** 12), missing("no_end"), missing("no_form", None)):
            self.assertEqual(usage.checked(good), good)
        for bad in (None, [], "15", {}, stated(-1), stated(10 ** 12 + 1), stated(True), stated(1.0), stated("5"),
                    dict(form=FORM, stated=dict(total=5, more=1)), dict(form=FORM, stated=dict(total=5), cost=1),
                    dict(form="other", stated=dict(total=5)), dict(stated=dict(total=5)), dict(form=[], stated=dict(total=5)),
                    dict(not_reported="words of a seat"), dict(not_reported=[]), dict(not_reported=None),
                    dict(not_reported="no_end", form="other"), dict(not_reported="no_end", total=5)):
            with self.subTest(bad=bad):
                self.assertIsNone(usage.checked(bad))
                self.assertIsNone(usage.total(bad))
        self.assertEqual((usage.total(stated(0)), usage.total(missing("no_end"))), (0, None))

    def test_a_sum_says_of_how_many_runs_it_is_and_is_none_where_no_run_stated_a_figure(self):
        runs = [dict(seat="builder", task="a", **stated(5)), dict(seat="builder", task="a", **missing("no_end")),
                dict(seat="builder", task="b", **stated(7)), dict(seat="reviewer", task="a", **missing("no_form", None)),
                dict(seat="reviewer", task="a", form=FORM, stated=dict(total="9"))]
        self.assertEqual(usage.summed(runs, "seat"), [dict(seat="builder", total=12, counted=2, runs=3),
                                                     dict(seat="reviewer", total=None, counted=0, runs=2)])
        self.assertEqual(usage.summed(runs, "task", "seat"), [
            dict(task="a", seat="builder", total=5, counted=1, runs=2),
            dict(task="b", seat="builder", total=7, counted=1, runs=1),
            dict(task="a", seat="reviewer", total=None, counted=0, runs=2)])
        self.assertEqual(usage.summed([], "seat"), [])


class CodexSeats(ReviewFixture):
    """The fixture with a builder and a reviewer whose signed seats are `codex exec` in its
    text form. No such CLI is run: the fake process prints what the test gives it."""
    def setUp(self):
        super().setUp()
        for role in ("builder", "reviewer"):
            self.value["seats"][role] = dict(agent="codex", runtime="codex", argv=["exec", "--model", "{model}", "{prompt}"],
                                             model="example-" + role + "-model")
        self.regrant()

    def regrant(self):
        raw = receipts.encoded(self.value)
        (self.shift / "grant.json").write_bytes(raw)
        self.admission.raw = raw
        self.driver = Driver(self.admission, self.spawn, seconds=1000, wind_down=10)

    def turn(self, at=-1):
        return self.state()["tasks"]["task"]["turns"][at]

    def attempts(self, state=None):
        return (state or self.state())["tasks"]["task"]["reviews"][-1]["attempts"]

    def open_pipe(self, data, name="stdout"):
        """The process printed `data` on a pipe that something still holds open for writing."""
        reader, writer = os.pipe()
        os.write(writer, data)
        os.set_blocking(reader, False)
        stream = os.fdopen(reader, "rb", buffering=0)
        setattr(self.spawn.child, name, stream)
        self.addCleanup(os.close, writer)
        self.addCleanup(lambda: stream.closed or stream.close())

    def reported(self, reason="the fixture ends the shift"):
        self.driver.finish(reason)
        return (self.shift / "report.md").read_text(), decode((self.shift / "report.json").read_bytes())

    def again(self):
        """The report made once more from the kept files, as `sortie report` makes it."""
        doc = report.finalize(self.shift, 5)
        return (self.shift / "report.md").read_text(), doc

    def rewrite(self, change):
        """The state on file as a build from before the host recorded usage left it, or as `change` makes it."""
        path = self.shift / "driver-state.json"
        state = decode(path.read_bytes())
        change(state)
        path.chmod(0o600)
        path.write_bytes(receipts.encoded(state))

    @staticmethod
    def without_usage(state):
        for task in state["tasks"].values():
            for row in task["turns"]:
                row.pop("usage", None)
            for review in task["reviews"]:
                for attempt in review["attempts"]:
                    attempt.pop("usage", None)


class HostTurnTests(CodexSeats):
    def test_the_ended_turn_row_carries_what_the_cli_stated_and_the_state_reads_back(self):
        self.begin(running=True)
        self.prints(BUILDER_END)
        self.end_turn()
        self.assertEqual(self.turn()["usage"], stated(29528))
        result = shift_wait.wait(self.shift, 1, clock=self.clock.now, sleep=self.clock.sleep, now=self.clock.utc)
        self.assertEqual((result["status"], result["last_turn"]["usage"]), ("ready", stated(29528)))
        # The host that reads this state back takes it as it is, and so does one started later.
        self.assertEqual(self.consume_phase()["status"], "advanced")
        other = Driver(self.admission, self.spawn, seconds=1000, wind_down=10)
        with other.coordination(local_only=True) as view:
            self.assertEqual(other.load(view)["tasks"]["task"]["turns"][0]["usage"], stated(29528))
        self.assertNotIn("usage", self.state()["turn"] or {})  # the row of the turn that runs now has exact keys

    def test_of_a_log_with_the_marker_twice_the_turns_own_pair_is_recorded(self):
        self.begin(running=True)
        self.prints(TWICE)
        self.end_turn()
        self.assertEqual(self.turn()["usage"], stated(29528))

    def test_a_turn_with_another_status_is_not_reported_though_a_pair_stands_in_its_output(self):
        self.begin(running=True)
        self.prints(BUILDER_END)
        self.end_turn(1)
        self.assertEqual(self.turn()["usage"], missing("not_ended"))

    def test_a_turn_the_host_stopped_at_its_limit_is_not_reported_though_a_pair_stands_in_its_output(self):
        self.assertEqual(self.action("brief", task_id="task", kind="feature", brief=self.artifact("briefs/task.md"),
                                     next_owner="builder", turn_seconds=5)["status"], "recorded")
        self.prints(BUILDER_END)
        self.clock.sleep(5)
        self.driver.poll_turn()
        row = self.turn()
        self.assertEqual((row["outcome"], row["usage"]), ("timeout", missing("not_ended")))

    def test_a_turn_whose_stop_was_not_confirmed_is_not_reported(self):
        self.begin(running=True)
        self.prints(BUILDER_END)
        with mock.patch("sortie_kit.tick_supervise.stop_group", side_effect=PermissionError("no signal")):
            self.spawn.child.code = 0
            self.driver.poll_turn(stop="stopped: host stopped")
        row = self.turn()
        self.assertEqual((row["outcome"], row["usage"]), ("exit; stop not confirmed", missing("not_ended")))

    def test_a_turn_whose_pipe_was_not_seen_to_end_is_not_reported(self):
        """Something the turn started still holds the pipe: what was read is not known to be
        all of it, and a pair in it not known to be the last."""
        self.begin(running=True)
        self.open_pipe(BUILDER_END)
        self.end_turn()
        row = self.turn()
        self.assertEqual((row["outcome"], row["exit"], row["usage"]), ("exit", 0, missing("no_end")))
        self.assertIn(b"tokens used\n29,528\n", (self.shift / row["log"]).read_bytes())

    def test_a_cli_that_prints_no_such_lines_and_one_that_prints_nothing_have_no_figure(self):
        self.begin(running=True)
        self.prints(b"something else was put where the CLI is\nand it says nothing of tokens\n")
        self.end_turn()
        self.assertEqual(self.turn()["usage"], missing("no_figure"))
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.end_turn()  # no pipe at all: the fake has none unless a test gives it one
        self.assertEqual(self.turn()["usage"], missing("no_figure"))

    def test_more_output_than_the_log_keeps_still_gives_the_figure_from_the_end_of_the_pipe(self):
        self.begin(running=True)
        self.prints(b"x" * 100 + b"\n" + b"y" * TURN_LOG_LIMIT + b"\n" + BUILDER_END)
        self.driver.poll_turn()  # one pass while it runs reads as much as the log holds
        self.end_turn()
        row = self.turn()
        self.assertGreater(row["output_dropped_bytes"], 0)
        self.assertNotIn(b"tokens used", (self.shift / row["log"]).read_bytes())
        self.assertEqual(row["usage"], stated(29528))

    def test_a_log_written_to_after_the_turn_changes_nothing(self):
        self.begin(running=True)
        self.prints(BUILDER_END)
        self.end_turn()
        with open(self.shift / self.turn()["log"], "ab") as stream:
            stream.write(b"tokens used\n999,999\n")
        self.assertEqual(self.turn()["usage"], stated(29528))
        text, doc = self.reported()
        self.assertEqual([run["stated"] for run in doc["usage"]["runs"]], [dict(total=29528)])
        self.assertNotIn("999,999", text)

    def test_the_figure_of_one_turn_is_not_taken_for_the_next(self):
        self.begin(running=True)
        self.prints(BUILDER_END)
        self.end_turn()
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.prints(b"this turn printed no figure\n")
        self.end_turn()
        self.assertEqual([row["usage"] for row in self.state()["tasks"]["task"]["turns"]],
                         [stated(29528), missing("no_figure")])

    def test_a_turn_that_did_not_start_is_no_run_and_a_lost_one_was_recorded_by_no_host(self):
        self.spawn.failure = OSError("no such program")
        result = self.action("brief", task_id="task", kind="feature", brief=self.artifact("briefs/task.md"),
                             next_owner="builder")
        self.assertEqual(result["status"], "blocked", result)
        row = self.turn()
        self.assertTrue(row["outcome"].startswith("not started"), row)
        self.assertNotIn("usage", row)
        self.spawn.failure = None
        self.assertEqual(self.action("continue")["status"], "recorded")
        later = Driver(self.admission, self.spawn, seconds=1000, wind_down=10)  # a host that did not start that turn
        with later.coordination(local_only=True) as view:
            state = later.load(view)
            self.assertTrue(later.settle(state, view))
        self.assertEqual((state["tasks"]["task"]["turns"][-1]["outcome"], state["tasks"]["task"]["turns"][-1]["usage"]),
                         ("lost", missing("not_recorded", None)))

    def test_a_seat_of_another_form_is_not_reported_and_says_that_its_cli_states_none(self):
        self.value["seats"]["builder"] = dict(agent="fixture", runtime="codex")
        self.regrant()
        self.begin(running=True)
        self.prints(BUILDER_END)
        self.end_turn()
        self.assertEqual(self.turn()["usage"], missing("no_form", None))


class HostReviewTests(CodexSeats):
    def test_the_kept_attempt_carries_the_record_and_the_receipt_does_not_and_is_valid(self):
        self.reach_review()
        self.prints(b"transcript of the review\n" + REVIEW_END, "stderr")
        state = self.end_review()
        attempt, = self.attempts(state)
        self.assertEqual((state["last_review"]["outcome"], attempt["usage"]), ("ACCEPT", stated(47966)))
        receipt = self.review_receipt(state)
        self.assertNotIn("usage", receipt["data"]["attempts"][0])
        self.assertNotIn("usage", receipts.encoded(receipt).decode())
        entry = state["tasks"]["task"]["receipts"][-1]
        self.assertEqual((entry["stage"], entry["status"]), ("review", "satisfied"))  # checked and recorded as any receipt
        self.assertEqual(self.action("close")["status"], "closed")  # and the state reads back

    def test_the_figure_is_read_from_stderr_and_never_from_the_answer(self):
        self.reach_review()
        self.prints(b"the reviewer's transcript, with no figure\n", "stderr")
        state = self.end_review(self.review_output() + "tokens used\n999,999\n")
        attempt, = self.attempts(state)
        self.assertEqual((attempt["status"], attempt["usage"]), ("complete", missing("no_figure")))

    def test_each_attempt_has_its_own_record_and_one_that_failed_by_its_status_has_none(self):
        self.reach_review()
        self.prints(b"Error: no such model\ntokens used\n1,234\n", "stderr")
        self.end_review("", code=2)
        self.prints(REVIEW_END, "stderr")
        state = self.end_review()
        first, second = self.attempts(state)
        self.assertEqual((first["status"], first["usage"]), ("failed", missing("not_ended")))
        self.assertEqual((second["status"], second["usage"]), ("complete", stated(47966)))

    def test_an_attempt_that_ended_well_and_gave_no_review_still_states_what_it_used(self):
        self.reach_review()
        self.prints(REVIEW_END, "stderr")
        self.end_review("I looked and it seems fine.\n")
        self.prints(b"tokens used\n31,872\n", "stderr")
        state = self.end_review("Still fine.\n")
        self.assertEqual(state["last_review"]["outcome"], "FAILED")
        self.assertEqual([(item["status"], item["usage"]) for item in self.attempts(state)],
                         [("failed", stated(47966)), ("failed", stated(31872))])

    def test_a_review_the_host_ended_at_its_limit_is_not_reported(self):
        self.driver.review_limit = 100
        self.reach_review()
        for _ in range(2):
            self.prints(REVIEW_END, "stderr")
            self.clock.sleep(101)
            self.driver.poll_turn()
        self.assertEqual([item["usage"] for item in self.attempts()], [missing("not_ended"), missing("not_ended")])

    def test_a_review_whose_pipe_was_not_seen_to_end_is_not_reported(self):
        self.reach_review()
        self.open_pipe(REVIEW_END, "stderr")
        self.end_review()
        self.prints(REVIEW_END, "stderr")
        state = self.end_review()
        first, second = self.attempts(state)
        self.assertIn("could not be read to its end", " ".join(first["reasons"]))
        self.assertEqual((first["usage"], second["usage"]), (missing("no_end"), stated(47966)))

    def test_an_attempt_that_could_not_start_is_no_run(self):
        self.reach_review()
        self.prints(REVIEW_END, "stderr")
        self.spawn.failure = OSError("no such program")
        state = self.end_review("I looked and it seems fine.\n")
        first, second = self.attempts(state)
        self.assertEqual((first["usage"], second["ended_by"]), (stated(47966), "not started"))
        self.assertNotIn("usage", second)

    def test_the_receipt_finished_at_a_second_try_keeps_the_record(self):
        """The lock ran out after the receipt was written and recorded and before the state was
        saved: the attempts are then taken from the receipt this process built, which has no
        usage, and the record this process holds beside them is still put on the kept attempt."""
        self.reach_review()
        self.prints(REVIEW_END, "stderr")
        self.spawn.child.code = 0
        self.prints(self.review_output().encode())
        with mock.patch.object(self.driver, "save", side_effect=Elapsed("no time left under this lock")) as cut:
            with self.assertRaises(Elapsed):
                self.driver.poll_turn()
        self.assertEqual(cut.call_count, 1)
        self.assertIsNotNone(self.state()["review"]["running"])
        self.driver.record_turn()
        state = self.state()
        attempt, = self.attempts(state)
        self.assertEqual((state["last_review"]["outcome"], attempt["usage"]), ("ACCEPT", stated(47966)))
        self.assertNotIn("usage", self.review_receipt(state)["data"]["attempts"][0])

    def test_a_review_left_by_an_earlier_host_has_attempts_no_host_recorded(self):
        self.reach_review()
        self.prints(REVIEW_END, "stderr")
        self.end_review("I looked and it seems fine.\n")  # the first attempt is on file; the second runs
        later = Driver(self.admission, self.spawn, seconds=1000, wind_down=10)
        with later.coordination(local_only=True) as view:
            state = later.load(view)
            self.assertTrue(later.settle(state, view))
        review = state["tasks"]["task"]["reviews"][-1]
        self.assertEqual((review["outcome"], [item["usage"] for item in review["attempts"]]),
                         ("lost", [missing("not_recorded", None)]))


class ReportTests(CodexSeats):
    def test_the_report_knows_the_log_of_a_turn_by_the_name_and_the_size_the_host_gives_it(self):
        self.assertEqual((report.BUILDER_LOG, report.USAGE_LOG_LIMIT), (BUILDER_LOG, TURN_LOG_LIMIT))
        self.begin()
        self.assertEqual(self.turn()["log"], report.BUILDER_LOG.format(task="task", turn=1))

    def shift_with_usage(self):
        """A plan round with a figure for each run but one, then the review of the candidate."""
        self.begin(running=True)
        self.prints(BUILDER_END)
        self.end_turn()
        while self.task_state()["stage"] != "review":
            if self.task_state()["stage"] == "plan_review":
                if self.state()["review"] is None:
                    self.assertEqual(self.action("continue")["status"], "recorded")
                self.prints(REVIEW_END, "stderr")
                self.end_review()
            else:
                self.assertEqual(self.consume_phase()["status"], "advanced")
        self.assertEqual(self.action("continue")["status"], "blocked")  # the review of the candidate runs
        self.prints(b"no figure here\n", "stderr")
        return self.end_review()

    def section(self, text):
        lines = text.split("\n## Usage\n", 1)[1]
        return lines.split("\n## ", 1)[0]

    def test_the_report_has_the_runs_the_totals_and_what_is_not_counted(self):
        self.shift_with_usage()
        text, doc = self.reported()
        got = doc["usage"]
        self.assertEqual(set(got), {"runs", "by_task", "by_seat", "driver", "replayed"})
        self.assertEqual((got["driver"], got["replayed"]), ("not counted", False))
        self.assertEqual(got["runs"], [
            dict(seat="builder", task="task", turn=1, **stated(29528)),
            dict(seat="reviewer", task="task", review=1, attempt=1, **stated(47966)),
            dict(seat="reviewer", task="task", review=2, attempt=1, **missing("no_figure"))])
        self.assertEqual(got["by_task"], [dict(task="task", seat="builder", total=29528, counted=1, runs=1),
                                          dict(task="task", seat="reviewer", total=47966, counted=1, runs=2)])
        self.assertEqual(got["by_seat"], [
            dict(seat="builder", agent="codex", model="example-builder-model", total=29528, counted=1, runs=1),
            dict(seat="reviewer", agent="codex", model="example-reviewer-model", total=47966, counted=1, runs=2)])
        self.assertEqual(doc["schema"], "sortie-kit.report.v2")
        part = self.section(text)
        for line in ("- Builder (codex, example-builder-model): 29,528 tokens, from 1 of 1 run.",
                     "- Reviewer (codex, example-reviewer-model): 47,966 tokens, from 1 of 2 runs.",
                     "- Driver: not counted. It ran in your terminal and the host read none of its output.",
                     "- Task task: builder 29,528 tokens, from 1 of 1 run; reviewer 47,966 tokens, from 1 of 2 runs.",
                     "| task | builder | turn 1 | 29,528 |",
                     "| task | reviewer | review 1, attempt 1 | 47,966 |",
                     "| task | reviewer | review 2, attempt 1 | not reported: no figure in what it printed |"):
            self.assertIn(line + "\n", part)
        self.assertIn("as its CLI reported it", part)
        self.assertIn("The host did not count them", part)
        self.assertIn("No number is given across seats", part)
        self.assertNotIn("read again", part)
        self.assertEqual(doc["status"], "evidence summarized", doc["issues"])

    def test_a_figure_that_is_not_reported_is_never_shown_as_0(self):
        self.value["seats"]["builder"] = dict(agent="fixture", runtime="codex", model="example-builder-model")
        self.regrant()
        self.begin(running=True)
        self.prints(BUILDER_END)
        self.end_turn()
        text, doc = self.reported()
        self.assertEqual(doc["usage"]["by_seat"], [
            dict(seat="builder", agent="fixture", model="example-builder-model", total=None, counted=0, runs=1),
            dict(seat="reviewer", agent="codex", model="example-reviewer-model", total=None, counted=0, runs=0)])
        part = self.section(text)
        self.assertIn("- Builder (fixture, example-builder-model): not reported, from 0 of 1 run.\n", part)
        self.assertIn("| task | builder | turn 1 | not reported: its CLI states no usage in the form the seat runs it in |\n",
                      part)
        self.assertIn("- Reviewer (codex, example-reviewer-model): no run.\n", part)
        for zero in (" 0 tokens", "| 0 |", ": 0,", ": 0 "):
            self.assertNotIn(zero, part)
        self.assertNotIn("29,528", part)

    def test_a_shift_with_no_run_says_so(self):
        text, doc = self.reported()
        self.assertEqual((doc["usage"]["runs"], doc["usage"]["by_seat"][0]["runs"]), ([], 0))
        self.assertIn("No builder turn and no review ran.\n", self.section(text))

    def test_a_record_of_another_shape_in_the_state_is_shown_as_malformed_and_nothing_of_it_is_kept(self):
        self.begin(running=True)
        self.prints(BUILDER_END)
        self.end_turn()
        self.driver.finish("the fixture ends the shift")
        self.rewrite(lambda state: state["tasks"]["task"]["turns"][0].update(
            usage=dict(form=FORM, stated=dict(total=5, words="<b>a seat's words</b>"))))
        text, doc = self.again()
        self.assertEqual(doc["usage"]["runs"], [dict(seat="builder", task="task", turn=1, **missing("malformed", None))])
        self.assertNotIn("a seat's words", text)

    def test_an_older_state_without_usage_is_read_again_from_the_kept_logs_and_says_so(self):
        self.shift_with_usage()
        self.driver.finish("the fixture ends the shift")
        self.rewrite(self.without_usage)
        text, doc = self.again()
        got = doc["usage"]
        self.assertTrue(got["replayed"])
        self.assertEqual(got["runs"], [
            dict(seat="builder", task="task", turn=1, read_again=True, **stated(29528)),
            dict(seat="reviewer", task="task", review=1, attempt=1, read_again=True, **stated(47966)),
            dict(seat="reviewer", task="task", review=2, attempt=1, read_again=True, **missing("no_figure"))])
        part = self.section(text)
        self.assertIn("read again from the kept log", part)
        self.assertIn("which the host cannot tell from one written to later", part)
        self.assertIn("- Builder (codex, example-builder-model): 29,528 tokens, from 1 of 1 run.\n", part)
        self.assertEqual(doc["status"], "evidence summarized", doc["issues"])
        self.assertNotIn("logs/builder-turn-task-1.log", [item["path"] for item in doc["inputs"]])

    def test_no_log_is_read_again_for_a_shift_whose_host_recorded_usage(self):
        """One record is enough: what the host did not take is not filled in from a log,
        which can have been written to later."""
        self.begin(running=True)
        self.open_pipe(BUILDER_END)
        self.end_turn()
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.prints(BUILDER_END)
        self.end_turn()
        self.driver.finish("the fixture ends the shift")
        self.rewrite(lambda state: state["tasks"]["task"]["turns"][1].pop("usage"))
        text, doc = self.again()
        self.assertFalse(doc["usage"]["replayed"])
        self.assertEqual([{key: run[key] for key in run if key in ("stated", "not_reported")} for run in doc["usage"]["runs"]],
                         [dict(not_reported="no_end"), dict(not_reported="not_recorded")])
        self.assertNotIn("29,528", self.section(text))  # what the turn printed last is quoted under the task, as output

    def older(self, change=None, before=None):
        """The shift of `shift_with_usage` as a build without usage left it, with `before`
        done to the running shift first and `change` to the kept files after its end."""
        self.shift_with_usage()
        self.driver.finish("the fixture ends the shift")
        self.rewrite(self.without_usage)
        was = self.again()[1]["status"]
        if change is not None:
            change()
        text, doc = self.again()
        return text, doc, was

    def builder_log(self):
        return self.shift / "logs" / "builder-turn-task-1.log"

    def test_a_missing_builder_log_is_not_reported_and_no_problem_of_the_evidence(self):
        text, doc, was = self.older(lambda: self.builder_log().unlink())
        self.assertEqual(doc["usage"]["runs"][0], dict(seat="builder", task="task", turn=1, read_again=True,
                                                      **missing("no_log")))
        self.assertEqual((doc["status"], was), ("evidence summarized", "evidence summarized"), doc["issues"])

    def test_a_builder_log_larger_than_the_host_keeps_one_is_not_read(self):
        def grow():
            with open(self.builder_log(), "ab") as stream:
                stream.truncate(TURN_LOG_LIMIT + 1)
                stream.write(b"\ntokens used\n5\n")
        text, doc, was = self.older(grow)
        self.assertEqual(doc["usage"]["runs"][0]["not_reported"], "no_log")
        self.assertEqual(doc["status"], was)

    def test_a_builder_log_that_is_a_link_or_no_file_is_not_followed(self):
        def link():
            other = self.shift / "logs" / "elsewhere.log"
            other.write_bytes(BUILDER_END)
            self.builder_log().unlink()
            self.builder_log().symlink_to(other)
        text, doc, was = self.older(link)
        self.assertEqual(doc["usage"]["runs"][0]["not_reported"], "no_log")
        self.assertEqual(doc["status"], was)

    def test_only_the_end_of_a_long_builder_log_is_read_again(self):
        def longer():
            self.builder_log().write_bytes(b"tokens used\n5\n" + b"a line of the answer\n" * (usage.TAIL // 21 + 2))
        text, doc, was = self.older(longer)
        self.assertEqual(doc["usage"]["runs"][0]["not_reported"], "no_figure")

    def test_turns_that_did_not_end_well_or_dropped_output_are_not_read_again(self):
        self.shift_with_usage()
        self.driver.finish("the fixture ends the shift")
        for label, change in (("status", dict(exit=1)), ("limit", dict(outcome="timeout")),
                              ("dropped", dict(output_dropped_bytes=7)), ("log", dict(log="logs/another.log")),
                              ("path", dict(log="../direction.md")), ("no path", dict(log=None))):
            with self.subTest(label=label):
                def older(state):
                    self.without_usage(state)
                    state["tasks"]["task"]["turns"][0].update(change)
                (self.shift / "logs" / "another.log").write_bytes(BUILDER_END)
                self.rewrite(older)
                run = self.again()[1]["usage"]["runs"][0]
                self.assertIn(run["not_reported"], ("not_ended", "no_end", "no_log"))
                self.assertNotIn("stated", run)

    def test_a_review_log_changed_since_is_read_from_the_hosts_kept_copy_or_not_at_all(self):
        state = self.shift_with_usage()
        ref = self.review_receipt(dict(last_review=dict(receipt=state["tasks"]["task"]["reviews"][0]["receipt"]))
                                  )["data"]["attempts"][0]["stderr"]
        self.driver.finish("the fixture ends the shift")
        self.rewrite(self.without_usage)
        (self.shift / ref["path"]).write_bytes(b"tokens used\n999,999\n")
        text, doc = self.again()
        self.assertEqual(doc["usage"]["runs"][1], dict(seat="reviewer", task="task", review=1, attempt=1,
                                                      read_again=True, **stated(47966)))
        copy = self.shift / receipts.kept_path(ref["sha256"])
        copy.chmod(0o600)
        copy.write_bytes(b"tokens used\n888,888\n")
        text, doc = self.again()
        self.assertEqual(doc["usage"]["runs"][1]["not_reported"], "no_log")
        self.assertNotIn("999,999", text)
        self.assertNotIn("888,888", text)

    def test_a_review_whose_log_was_written_to_while_it_ran_or_that_dropped_output_is_not_read_again(self):
        self.reach_review()
        running = self.state()["review"]["running"]
        self.prints(REVIEW_END, "stderr")
        with open(self.shift / running["stderr"], "ab") as stream:
            stream.write(b"tokens used\n999,999\n")
        self.end_review()
        self.prints(b"words in the middle\n" * (TURN_LOG_LIMIT // 20 + 1) + REVIEW_END, "stderr")
        self.driver.poll_turn()
        state = self.end_review()
        first, second = self.review_receipt(state)["data"]["attempts"]
        self.assertIn("is not what the reviewer printed", " ".join(first["reasons"]))
        self.assertGreater(second["output_dropped_bytes"], 0)
        self.driver.finish("the fixture ends the shift")
        self.rewrite(self.without_usage)
        text, doc = self.again()
        self.assertEqual([run.get("not_reported") for run in doc["usage"]["runs"] if run["seat"] == "reviewer"],
                         ["no_log", "no_end"])
        self.assertNotIn("999,999", text)

    def test_a_review_whose_pipe_was_not_read_to_its_end_is_not_read_again(self):
        """Finding 1 of the read of the change: the log of such a run is what the host read
        and nothing was dropped, and its end is still not known to be the end of what the
        reviewer printed. The host that ran it says so in a reason of the receipt."""
        self.reach_review()
        self.open_pipe(REVIEW_END, "stderr")
        self.end_review()
        self.prints(REVIEW_END, "stderr")
        state = self.end_review()
        first, second = self.review_receipt(state)["data"]["attempts"]
        self.assertEqual((first["ended_by"], first["exit_status"], first["output_dropped_bytes"]), ("exit", 0, 0))
        self.assertIn(cli_words.UNDRAINED, first["reasons"])
        self.driver.finish("the fixture ends the shift")
        self.rewrite(self.without_usage)
        text, doc = self.again()
        self.assertEqual([run.get("not_reported", run.get("stated")) for run in doc["usage"]["runs"]
                          if run["seat"] == "reviewer"], ["no_end", dict(total=47966)])

    def test_an_attempt_whose_number_in_the_state_is_no_number_is_matched_to_none_of_the_receipt(self):
        """Finding 3: true equals 1, and the row of such an attempt would have had the figure of attempt 1."""
        self.reach_review()
        self.prints(REVIEW_END, "stderr")
        self.end_review()
        self.driver.finish("the fixture ends the shift")

        def older(state):
            self.without_usage(state)
            state["tasks"]["task"]["reviews"][-1]["attempts"][0]["attempt"] = True
        self.rewrite(older)
        run = self.again()[1]["usage"]["runs"][-1]
        self.assertEqual((run["attempt"], run.get("not_reported")), (None, "no_log"))
        self.assertNotIn("stated", run)

    def test_the_section_is_written_whatever_shape_the_usage_of_a_report_has(self):
        """Finding 2: a number where a list belongs raised, and the readable report was lost for it."""
        good = dict(runs=[], by_task=[], by_seat=[], driver="not counted", replayed=False)
        for label, change in (("by_seat", dict(by_seat=5)), ("runs", dict(runs=7)), ("by_task", dict(by_task=True)),
                              ("all", dict(by_seat=1.5, runs="words", by_task=dict(a=1))),
                              ("rows", dict(by_seat=[5, None, "x"], runs=[5, None, dict(seat=[], task={}, turn="x")],
                                            by_task=[dict(task=[]), 7])),
                              ("counts", dict(by_seat=[dict(seat="builder", total=5, counted=-1, runs=2)])),
                              ("more", dict(runs=[dict(seat="builder", task="a", turn=1)], more="many"))):
            with self.subTest(label=label):
                lines = report.usage_words(dict(good, **change))
                self.assertEqual(lines[0], "## Usage")
                self.assertNotIn("5 tokens", "\n".join(lines))
        for found in (5, "words", [], True):
            self.assertIn("Not known", report.usage_words(found)[2])

    def test_runs_left_out_at_the_bound_leave_no_count_of_more_beside_them(self):
        self.shift_with_usage()
        self.driver.finish("the fixture ends the shift")
        with receipts.session(self.shift, 5, local_only=True) as view, mock.patch.object(report, "USAGE_RUNS", 2):
            full = report.extract(view)
            self.assertEqual((len(full["usage"]["runs"]), full["usage"]["more"]), (2, 1))
            self.assertEqual(full["usage"]["by_seat"][1]["runs"], 2)  # the totals are of all the runs
            room = report.Reader(view, 1)
            got = report.usage_summary(room, decode((self.shift / "driver-state.json").read_bytes()), full["grant"])
        self.assertEqual((got["runs"], got["left_out"]), (None, report.LEFT_OUT))
        self.assertNotIn("more", got)

    def test_a_review_attempt_that_failed_by_its_status_is_not_read_again(self):
        self.reach_review()
        self.prints(b"Error: no such model\ntokens used\n1,234\n", "stderr")
        self.end_review("", code=2)
        self.prints(REVIEW_END, "stderr")
        self.end_review()
        self.driver.finish("the fixture ends the shift")
        self.rewrite(self.without_usage)
        text, doc = self.again()
        self.assertEqual([run.get("not_reported", run.get("stated")) for run in doc["usage"]["runs"]
                          if run["seat"] == "reviewer"], ["not_ended", dict(total=47966)])
        self.assertNotIn("1,234", self.section(text))

    def test_the_short_form_above_the_output_limit_says_that_usage_is_left_out(self):
        self.shift_with_usage()
        self.reported()
        size = max((self.shift / name).stat().st_size for name in ("report.json", "report.md"))
        with mock.patch.object(receipts, "DOCUMENT_LIMIT", size - 1), \
                mock.patch.object(report, "DETAIL_SMALL", report.DETAIL_LIMIT):
            doc = report.finalize(self.shift, 3)
        self.assertIn("output limit", doc["issues"][0]["reason"])
        self.assertEqual(doc["usage"], dict(runs=None, by_task=None, by_seat=None, driver="not counted", replayed=False,
                                            left_out="output limit exceeded; retained details omitted"))
        part = self.section((self.shift / "report.md").read_text())
        self.assertIn("left out of this report", part)
        self.assertNotIn("29,528", part)

    def test_runs_that_do_not_fit_in_the_reports_own_bound_are_left_out_and_the_seats_totals_stay(self):
        self.shift_with_usage()
        self.driver.finish("the fixture ends the shift")
        with receipts.session(self.shift, 5, local_only=True) as view:
            full = report.extract(view)
            room = report.Reader(view, len(receipts.encoded(full["usage"]["runs"])) - 1)
            room.details = 0
            got = report.usage_summary(room, decode((self.shift / "driver-state.json").read_bytes()), full["grant"])
        self.assertEqual((got["runs"], got["by_task"], got["left_out"]), (None, None, report.LEFT_OUT))
        self.assertEqual(got["by_seat"], full["usage"]["by_seat"])
        self.assertIn(dict(path="driver-state.json", reason=report.LEFT_OUT), room.issues)

    def test_a_shift_whose_state_could_not_be_read_has_no_usage_and_says_so(self):
        self.shift_with_usage()
        self.driver.finish("the fixture ends the shift")
        path = self.shift / "driver-state.json"
        path.chmod(0o600)
        path.write_bytes(b"{")
        text, doc = self.again()
        self.assertIsNone(doc["usage"])
        self.assertIn("not known", self.section(text))

    def test_the_digest_of_a_grant_is_what_names_the_form_for_a_reading_again(self):
        """The form is that of the seat in the kept grant: a shift whose reviewer was another
        CLI is not read by the rule of this one, whatever its logs hold."""
        self.value["seats"]["reviewer"] = dict(agent="fixture", runtime="codex", model="example-reviewer-model")
        self.regrant()
        self.shift_with_usage()
        self.driver.finish("the fixture ends the shift")
        self.rewrite(self.without_usage)
        text, doc = self.again()
        self.assertEqual([run.get("not_reported") for run in doc["usage"]["runs"]], [None, "no_form", "no_form"])
        self.assertEqual(hashlib.sha256((self.shift / "grant.json").read_bytes()).hexdigest(), doc["grant"]["sha256"])


if __name__ == "__main__":
    unittest.main()
