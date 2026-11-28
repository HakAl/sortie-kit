"""The review the host runs itself: what reaches the builder and the next review, what
counts as a failed attempt, what a failure does to the task, and wind-down."""
import os
import json
import subprocess
import unittest

import test_lifecycle
from unittest import mock

from sortie_kit import channels, review_dir
from sortie_kit.lifecycle import Driver, HELD, LOST_REVIEW, START_RESERVE, WRITE_RESERVE
from sortie_kit.layout import Refused
from sortie_kit.receipt_schema import decode


class ReviewFixture(test_lifecycle.LifecycleFixture):
    """The lifecycle fixture with the time under every lock counted by the fixture's clock."""
    def setUp(self):
        super().setUp()
        # A further attempt at a review is prepared only with room left under the lock, and
        # the receipt check is given what that room leaves. On the machine's clock a loaded
        # machine decided which of the two a test saw; here the room is what the test left.
        patched = self.session_time_by_the_clock()
        patched.start()
        self.addCleanup(patched.stop)

    def review_receipt(self, state=None):
        last = (state or self.state())["last_review"]
        return decode((self.shift / last["receipt"]["path"]).read_bytes())

    def directory(self):
        return self.shift / self.state()["review"]["running"]["directory"]


class HostReviewTests(ReviewFixture):
    def test_the_reviewer_is_shown_the_receipts_the_host_took_for_the_task_and_the_files_they_name(self):
        """A reviewer held a finding open through three rounds because the acknowledgment the
        brief asked for, which the host had taken, was not in front of it."""
        self.reach_review()
        task = self.task_state()
        taken = [item for item in task["receipts"] if item["status"] == "satisfied"]
        self.assertEqual([item["stage"] for item in taken], ["ack", "build"])
        for item in taken:
            self.assertIsNotNone(channels.timestamp(item["at"]), item)
        where = self.directory() / "evidence"
        self.assertEqual(sorted(p.name for p in (where / "receipts").iterdir()),
                         sorted(item["path"].split("/")[1] for item in taken))
        for item in taken:
            self.assertEqual((where / item["path"]).read_bytes(), (self.shift / item["path"]).read_bytes())
        self.assertEqual((where / "files" / "logs" / "ack.txt").read_text(), "acknowledged\n")
        self.assertEqual((where / "files" / "logs" / "tests.log").read_text(), "tests pass\n")
        said = " ".join((where / "HOST.md").read_text().split())
        for part in ("(acknowledgment, round 0", "(build, round 0", taken[0]["at"], taken[0]["sha256"],
                     "The task began at commit " + task["base"]):
            self.assertIn(part, said)
        # The review is taken as before, and the report reads the task as before.
        self.assertEqual(self.end_review()["last_review"]["outcome"], "ACCEPT")
        self.driver.finish("the fixture ends the shift")
        self.assertEqual(decode((self.shift / "report.json").read_bytes())["issues"], [])

    def test_the_reviewer_is_started_by_the_host_in_its_own_directory_with_the_signed_seat(self):
        self.reach_review()
        started = self.spawn.started[-1]
        where = self.directory()
        self.assertEqual(started["cwd"], str(where))
        self.assertEqual(where.parent, self.shift / "reviews")
        self.assertEqual(started["argv"][:3], ["fixture-agent", "run", "reviewer"])
        self.assertIn("REVIEW.md", started["argv"][3])
        self.assertLess(len(started["argv"][3]), 600)  # the instructions are a file, not an argument
        self.assertFalse([name for name in started["env"] if name.startswith("GIT_") and name != "GIT_TERMINAL_PROMPT"])
        for name in ("REVIEW.md", "brief.md", "change.patch", "tree"):
            self.assertTrue((where / name).exists(), name)
        self.assertFalse((where / "answers.json").exists())  # nothing earlier to answer in the first round
        # Not a builder turn: the turn count and the turn record are untouched.
        state = self.state()
        self.assertIsNone(state["turn"])
        self.assertEqual(len(state["tasks"]["task"]["turns"]), 1)

    def test_changes_reach_the_builder_and_its_answers_reach_the_next_review_where_prior_decides(self):
        self.reach_review()
        state = self.end_review(changes=True)
        last, task = state["last_review"], state["tasks"]["task"]
        self.assertEqual((last["outcome"], task["stage"], last["findings"]),
                         ("CHANGES", "repair_needed", [dict(id="N1", severity="major")]))
        kept = task["findings"]["candidate"]
        self.assertIn(kept["path"], last["detail"])
        handed = decode((self.shift / kept["path"]).read_bytes())
        self.assertEqual([(item["id"], item["claim"], item["earlier"]) for item in handed["findings"]],
                         [("N1", "wrong word", False)])
        self.assertEqual(self.action("repair", reason="Address N1", next_owner="builder")["status"], "advanced")
        context = decode((self.shift / "current-context.json").read_bytes())
        self.assertEqual((context["stage"], context["round"], context["findings"]), ("build", 1, kept))
        self.assertEqual(context["expected"]["required_findings"], ["N1"])
        # A build receipt that does not answer the finding is returned, and no review starts.
        target = {"kind": "commit", "commit": self.candidate}
        _, path = self.phase_receipt(target=target, answers=[])
        refused = self.action("consume", receipt=path, target=target)
        self.assertEqual(refused["status"], "blocked", refused)
        self.assertIsNone(self.state()["review"])
        self.assertEqual(self.action("repair", reason="Answer N1", next_owner="builder")["status"], "advanced")
        answers = [dict(id="N1", disposition="rejected", reason="The word is the one the brief asks for.", evidence=None)]
        _, path = self.phase_receipt(target=target, answers=answers)
        taken = self.action("consume", receipt=path, target=target)
        self.assertEqual(taken["status"], "advanced", taken)
        review = self.state()["review"]
        self.assertEqual(review["prior"], [dict(id="N1", severity="major")])
        given = json.loads((self.directory() / "answers.json").read_text())
        self.assertEqual(given["builder_answers"], answers)
        self.assertEqual([item["id"] for item in given["earlier_findings"]], ["N1"])
        self.assertIn("N1", (self.directory() / "REVIEW.md").read_text())
        # The reviewer says the earlier finding is not resolved: CHANGES again, the same finding handed on.
        state = self.end_review(changes=True, new="M", resolved=False)
        task = state["tasks"]["task"]
        self.assertEqual(state["last_review"]["outcome"], "CHANGES")
        self.assertEqual(sorted(item["id"] for item in task["required"]["candidate"]), ["M2", "N1"])
        again = decode((self.shift / task["findings"]["candidate"]["path"]).read_bytes())
        earlier = next(item for item in again["findings"] if item["id"] == "N1")
        self.assertEqual((earlier["earlier"], earlier["claim"], earlier["reviewer_reason"]),
                         (True, "wrong word", "Checked against the patch."))
        self.assertEqual(self.action("repair", reason="Address both", next_owner="builder")["status"], "advanced")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        state = self.end_review()
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"]), ("ACCEPT", "accepted"))
        self.assertEqual(self.review_receipt(state)["data"]["prior"],
                         [dict(id="N1", severity="major"), dict(id="M2", severity="major")])
        self.assertEqual(state["tasks"]["task"]["round"], 3)

    def test_an_accept_that_leaves_an_earlier_major_unresolved_is_a_failed_attempt_and_no_accept(self):
        self.reach_review()
        self.end_review(changes=True)
        self.action("repair", reason="Address N1", next_owner="builder")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        state = self.end_review(resolved=False)  # says ACCEPT, and that N1 is not resolved
        review, task = state["review"], state["tasks"]["task"]
        self.assertEqual((task["stage"], task["accepted"]), ("review", None))
        self.assertEqual([item["status"] for item in review["attempts"]], ["failed"])
        self.assertIn("N1", " ".join(review["attempts"][0]["reasons"]))
        self.assertEqual(review["running"]["attempt"], 2)  # run once more, as any failed review is
        state = self.end_review(resolved=False)
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"]), ("FAILED", "blocked"))
        self.assertIsNone(state["tasks"]["task"]["accepted"])

    def answered(self, *requirements, verdict="ACCEPT", findings=(), schema=None):
        review = self.state()["review"]
        record = self.record(review["target"], verdict, findings, [], requirements=requirements or None, schema=schema)
        return self.end_review(self.said(record, review["running"]["marker"]))

    def test_an_accept_beside_a_requirement_not_met_is_a_failed_attempt_and_two_block_the_task_and_not_the_shift(self):
        """Seen on a real shift: the reviewer wrote that the reported defect could still happen, rated it
        minor and accepted, and the host did what the verdict said. With the brief answered requirement by
        requirement such an answer contradicts itself, and the host takes no verdict from it."""
        self.reach_review()
        missed = self.need("retry seconds and the minimum wait agree", "not_met", "source.txt:1")
        state = self.answered(self.need(), missed, findings=[self.finding("F1", "minor")])
        review, task = state["review"], state["tasks"]["task"]
        self.assertEqual((task["stage"], task["accepted"]), ("review", None))
        self.assertEqual([(item["status"], item["verdict"], item["record"]) for item in review["attempts"]],
                         [("failed", None, None)])
        self.assertIn("ACCEPT with a requirement answered not_met (retry seconds and the minimum wait agree) is not an accept",
                      review["attempts"][0]["reasons"][0])
        self.assertEqual(review["running"]["attempt"], 2)  # run once more, as any failed review is
        state = self.answered(missed)
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((last["outcome"], task["stage"], task["accepted"], state["active"], state["mode"]),
                         ("FAILED", "blocked", None, None, "active"))
        self.assertIn("review failed twice", task["reason"])
        receipt = self.review_receipt(state)
        self.assertEqual((receipt["data"]["outcome"], [item["status"] for item in receipt["data"]["attempts"]]),
                         ("FAILED", ["failed", "failed"]))
        self.assertEqual(self.action("brief", task_id="next", kind="defect", brief=self.artifact("briefs/task.md"),
                                     next_owner="builder")["status"], "recorded")

    def test_a_commit_review_without_the_requirement_list_is_a_failed_attempt_and_one_with_it_is_taken_whole(self):
        self.reach_review()
        # The answer an earlier build asked for, written by habit or from an old example.
        state = self.answered(schema="sortie-kit.review.v1")
        first, = state["review"]["attempts"]
        self.assertEqual((first["status"], first["verdict"]), ("failed", None))
        self.assertIn("the schema asked of this review is sortie-kit.review.v3", first["reasons"][0])
        # An accept beside a requirement the reviewer did not check is taken, and the record is kept whole.
        unchecked = self.need("no caller outside the module changes", "not_checked", "")
        state = self.answered(self.need(), unchecked)
        task = state["tasks"]["task"]
        self.assertEqual((state["last_review"]["outcome"], task["stage"]), ("ACCEPT", "accepted"))
        last = self.review_receipt(state)["data"]["attempts"][-1]
        self.assertEqual((last["record"]["schema"], last["record"]["requirements"]),
                         ("sortie-kit.review.v3", [self.need(), unchecked]))
        self.assertEqual(self.action("close")["status"], "closed")

    def test_a_requirement_marked_met_beside_a_finding_that_names_it_is_a_failed_attempt_and_two_block_the_task(self):
        """Seen in a replay of a kept review: the requirement marked `met` and, under ACCEPT, a minor finding
        that said a case of that same requirement fails. With each finding naming its requirement such an
        answer contradicts itself, and the host takes no verdict from it."""
        self.reach_review()
        agree = self.need("retry seconds and the minimum wait agree")
        named = dict(self.finding("F1", "minor"), requirement=2)
        state = self.answered(self.need(), agree, findings=[named])
        review, task = state["review"], state["tasks"]["task"]
        self.assertEqual((task["stage"], task["accepted"]), ("review", None))
        self.assertEqual([(item["status"], item["verdict"], item["record"]) for item in review["attempts"]],
                         [("failed", None, None)])
        self.assertIn("finding F1 names requirement 2 (retry seconds and the minimum wait agree), which the list "
                      "marks `met`", review["attempts"][0]["reasons"][0])
        self.assertEqual(review["running"]["attempt"], 2)
        state = self.answered(self.need(), dict(agree, status="not_checked", where=""), findings=[named])
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((last["outcome"], task["stage"], task["accepted"], state["active"]), ("FAILED", "blocked", None, None))
        self.assertIn("review failed twice", task["reason"])

    def test_an_accept_beside_a_requirement_disclosed_is_taken_and_the_record_is_kept_whole(self):
        self.reach_review()
        told = self.need("the second part, or a statement that it is not delivered", "disclosed", "notes.md:3")
        named = dict(self.finding("F1", "minor"), requirement=2)
        state = self.answered(self.need(), told, findings=[named])
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"]), ("ACCEPT", "accepted"))
        last = self.review_receipt(state)["data"]["attempts"][-1]
        self.assertEqual((last["record"]["requirements"], last["record"]["findings"]), ([self.need(), told], [named]))

    def test_changes_beside_a_requirement_not_met_go_to_the_builder_as_any_changes_do(self):
        self.reach_review()
        named = dict(self.finding("F1", "major"), requirement=1)
        state = self.answered(self.need(status="not_met"), verdict="CHANGES", findings=[named, self.finding("F2", "minor")])
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"]), ("CHANGES", "repair_needed"))
        # What goes to the builder, and with its answers to the next review, is the finding and not a
        # position in a list that neither of them is given.
        handed = decode((self.shift / state["tasks"]["task"]["findings"]["candidate"]["path"]).read_bytes())
        self.assertEqual([sorted(item) for item in handed["findings"]], [sorted(
            ("id", "severity", "location", "claim", "evidence", "recommendation", "earlier", "reviewer_reason"))] * 2)
        self.assertEqual(self.review_receipt(state)["data"]["attempts"][-1]["record"]["findings"][0]["requirement"], 1)
        self.assertEqual(self.review_receipt(state)["data"]["attempts"][-1]["record"]["requirements"][0]["status"], "not_met")

    def test_a_review_that_fails_twice_blocks_the_task_and_not_the_shift_and_uses_no_repair_round(self):
        self.reach_review()
        state = self.end_review("I looked and it seems fine.\n")
        self.assertEqual(state["review"]["running"]["attempt"], 2)
        self.assertEqual(len(self.spawn.started), 3)  # the one builder turn, then the reviewer twice
        state = self.end_review("Still fine.\n")
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((last["outcome"], task["stage"], task["round"], state["active"], state["mode"], state["review"]),
                         ("FAILED", "blocked", 0, None, "active", None))
        self.assertIn("review failed twice", task["reason"])
        self.assertIn("no repair round was used", last["detail"])
        receipt = self.review_receipt(state)
        self.assertEqual((receipt["role"], receipt["data"]["outcome"], len(receipt["data"]["attempts"])),
                         ("reviewer", "FAILED", 2))
        self.assertIsNone(task["accepted"])
        # The shift goes on: the next task can be briefed.
        result = self.action("brief", task_id="next", kind="defect", brief=self.artifact("briefs/task.md"),
                             next_owner="builder")
        self.assertEqual(result["status"], "recorded", result)

    def test_a_first_attempt_that_fails_and_a_second_that_accepts_are_one_receipt_and_an_accept(self):
        self.reach_review()
        state = self.end_review(code=3)  # a sound answer from a reviewer that exited with an error
        first, = state["review"]["attempts"]
        self.assertEqual((first["status"], first["exit_status"], first["verdict"]), ("failed", 3, None))
        self.assertIn("status 0", first["reasons"][0])
        state = self.end_review()
        task = state["tasks"]["task"]
        self.assertEqual((state["last_review"]["outcome"], task["stage"]), ("ACCEPT", "accepted"))
        data = self.review_receipt(state)["data"]
        self.assertEqual([(item["attempt"], item["status"], item["verdict"]) for item in data["attempts"]],
                         [(1, "failed", None), (2, "complete", "ACCEPT")])
        self.assertEqual([item["stage"] for item in task["receipts"]].count("review"), 1)
        self.assertEqual(task["accepted"]["receipt"]["path"], state["last_review"]["receipt"]["path"])
        self.assertEqual([len(item["attempts"]) for item in task["reviews"]], [2])
        self.assertEqual(self.action("close")["status"], "closed")

    def test_a_reviewer_that_runs_past_its_signed_limit_is_stopped_and_that_attempt_failed(self):
        self.reach_review()
        self.assertEqual(self.state()["review"]["running"]["limit_seconds"], 600)
        self.clock.sleep(599)
        self.driver.poll_turn()
        self.assertEqual(self.state()["review"]["attempts"], [])
        self.clock.sleep(2)
        child = self.spawn.child
        self.driver.poll_turn()
        self.assertEqual(child.signals, ["terminate"])
        first, = self.state()["review"]["attempts"]
        self.assertEqual((first["status"], first["ended_by"]), ("failed", "timeout"))

    def test_a_reviewer_first_seen_ended_after_its_signed_limit_is_a_failed_attempt_whatever_it_printed(self):
        """The limit was checked only while the process still ran, so a
        reviewer that ended between its limit and the host's next look was an accept."""
        self.reach_review()
        self.clock.sleep(599)
        self.driver.poll_turn()
        self.prints(self.review_output().encode())  # a sound ACCEPT
        self.spawn.child.code = 0
        self.clock.sleep(2)  # the host next looks one second after the limit
        self.driver.poll_turn()
        state = self.state()
        first = (state["review"] or state["tasks"]["task"]["reviews"][-1])["attempts"][0]
        self.assertEqual((first["status"], first["verdict"], first["exit_status"]), ("failed", None, 0))
        self.assertNotEqual(first["ended_by"], "exit")
        self.assertIn("after its signed limit", " ".join(first["reasons"]))
        self.assertEqual((state["tasks"]["task"]["stage"], state["tasks"]["task"]["accepted"]), ("review", None))

    def test_a_verdict_is_taken_from_what_the_reviewer_printed_and_never_from_what_is_in_its_log(self):
        """The host parsed what it read back from the log, which another
        seat can write to in place while the review runs."""
        self.reach_review()
        for label, printed in (("printed nothing", b""), ("printed no verdict", b"I looked.\n")):
            with self.subTest(label=label):
                review = self.state()["review"]
                self.assertIsNotNone(review, "the attempt before this one was taken as a verdict")
                running = review["running"]
                with open(self.shift / running["stdout"], "ab") as stream:
                    stream.write(self.review_output().encode())  # a seat puts a sound ACCEPT into the log
                self.prints(printed)
                self.spawn.child.code = 0
                self.driver.poll_turn()
                state = self.state()
                last = (state["review"] or state["tasks"]["task"]["reviews"][-1])["attempts"][-1]
                self.assertEqual((last["status"], last["verdict"]), ("failed", None))
                self.assertIn("is not what the reviewer printed", " ".join(last["reasons"]))
                # And the object was looked for in what was printed, where there is none.
                self.assertGreaterEqual(len(last["reasons"]), 2, last["reasons"])
                self.assertIsNone(state["tasks"]["task"]["accepted"])
        self.assertEqual(state["tasks"]["task"]["stage"], "blocked")

    def test_error_output_that_cannot_be_read_back_fails_the_attempt(self):
        # A sound verdict on stdout is not taken while the other log is unverified.
        from unittest.mock import patch
        from sortie_kit import lifecycle
        self.reach_review()
        self.prints(self.review_output().encode())
        self.spawn.child.code = 0
        real, wanted = os.pread, self.driver.err_fd
        self.assertIsNotNone(wanted)

        def pread(fd, size, offset):
            if fd == wanted:
                raise OSError(5, "Input/output error")
            return real(fd, size, offset)
        with patch.object(lifecycle.os, "pread", pread):
            self.driver.poll_turn()
        state = self.state()
        first = (state["review"] or state["tasks"]["task"]["reviews"][-1])["attempts"][0]
        self.assertEqual((first["status"], first["verdict"]), ("failed", None))
        self.assertIn("stderr could not be read back", " ".join(first["reasons"]))
        self.assertIsNone(state["tasks"]["task"]["accepted"])

    def test_output_that_could_not_be_read_to_its_end_fails_the_attempt(self):
        from unittest.mock import patch
        self.reach_review()
        self.prints(self.review_output().encode())
        self.spawn.child.code = 0
        real = self.driver.pump

        def pump():
            real()
            if self.driver.ending is None and self.driver.stopping is not None:  # the last drain, at the close
                raise OSError(5, "Input/output error")
        with patch.object(self.driver, "pump", pump):
            self.driver.poll_turn()
        state = self.state()
        first = (state["review"] or state["tasks"]["task"]["reviews"][-1])["attempts"][0]
        self.assertEqual((first["status"], first["verdict"]), ("failed", None), first["reasons"])
        self.assertIn("could not be read to its end", " ".join(first["reasons"]))

    def piped(self, data, name="stdout"):
        """The reviewer's output through a real pipe of this test's own. Returns the writing end."""
        reading, writing = os.pipe()
        os.write(writing, data)
        os.set_blocking(reading, False)
        stream = open(reading, "rb", buffering=0)
        self.addCleanup(stream.close)
        setattr(self.spawn.child, name, stream)
        return writing

    def test_a_pipe_something_still_holds_open_at_the_last_drain_fails_the_attempt(self):
        # The reviewer ended with status 0 and its group is gone, and a sound verdict was read.
        # Something that left the group still holds the writing end, so that nothing more was
        # there to read is not the end of what is printed.
        for name in ("stdout", "stderr"):
            with self.subTest(pipe=name):
                self.setUp()
                self.reach_review()
                if name == "stderr":
                    self.prints(self.review_output().encode())
                writing = self.piped(self.review_output().encode() if name == "stdout" else b"a note\n", name)
                self.addCleanup(os.close, writing)
                self.spawn.child.code = 0
                self.driver.poll_turn()
                state = self.state()
                first = (state["review"] or state["tasks"]["task"]["reviews"][-1])["attempts"][0]
                self.assertEqual((first["status"], first["verdict"]), ("failed", None), first["reasons"])
                self.assertIn("could not be read to its end", " ".join(first["reasons"]))
                self.assertIsNone(state["tasks"]["task"]["accepted"])

    def test_a_pipe_that_was_read_to_its_end_backs_the_verdict(self):
        self.reach_review()
        os.close(self.piped(self.review_output().encode()))
        os.close(self.piped(b"a note\n", "stderr"))
        self.spawn.child.code = 0
        self.driver.poll_turn()
        state = self.state()
        last = state["tasks"]["task"]["reviews"][-1]["attempts"][-1]
        self.assertEqual((last["status"], last["verdict"]), ("complete", "ACCEPT"), last.get("reasons"))

    def test_output_longer_than_one_pass_reads_is_read_to_its_end_at_the_close(self):
        # One pass of the host reads a bounded number of pieces. At the close the host goes on
        # until the end, so that a long sound answer is not cut and then taken for whole.
        from unittest.mock import patch
        from sortie_kit import lifecycle
        self.reach_review()
        os.close(self.piped(self.review_output().encode()))
        self.spawn.child.code = 0
        real = os.read
        with patch.object(lifecycle, "PUMP_READS", 2), \
                patch.object(lifecycle.os, "read", lambda fd, size: real(fd, min(size, 64))):
            self.driver.poll_turn()
        state = self.state()
        last = state["tasks"]["task"]["reviews"][-1]["attempts"][-1]
        self.assertEqual((last["status"], last["verdict"]), ("complete", "ACCEPT"), last.get("reasons"))

    def test_error_output_changed_in_the_log_fails_the_attempt_too(self):
        self.reach_review()
        running = self.state()["review"]["running"]
        self.prints(self.review_output().encode())
        with open(self.shift / running["stderr"], "ab") as stream:
            stream.write(b"not printed by the reviewer\n")
        self.spawn.child.code = 0
        self.driver.poll_turn()
        state = self.state()
        first = (state["review"] or state["tasks"]["task"]["reviews"][-1])["attempts"][0]
        self.assertEqual((first["status"], first["verdict"]), ("failed", None))
        self.assertIn("is not what the reviewer printed", " ".join(first["reasons"]))

    def test_the_serving_host_goes_on_after_a_reviews_end_could_not_be_saved_under_one_lock(self):
        """The time under the lock ran out after the review receipt was
        recorded; the foreground loop took that as its own end, exit 3, and shut the driver
        down, though the next pass finishes the same receipt."""
        from unittest import mock
        from sortie_kit import host, receipts
        from sortie_kit.layout import Elapsed
        self.reach_review()
        save, steps, cut = self.driver.save, [], []

        def saved(view, state):
            if steps and not cut:
                cut.append(self.clock.now())
                raise Elapsed("no time left under this lock")
            return save(view, state)

        def sleep(seconds):
            self.clock.sleep(seconds)
            state = self.state()
            if not steps:
                self.prints(self.review_output().encode())
                self.spawn.child.code = 0
                steps.append("review ended")
            elif steps == ["review ended"] and state["tasks"]["task"]["stage"] == "accepted":
                # The driver's next command for the task: the host must still be there to take it.
                command = dict(version=1, id="next", op="close", task_id="task",
                               request_id=state["tasks"]["task"]["request_id"])
                (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
                steps.append("close sent")
            elif steps[-1] == "close sent" and state["tasks"]["task"]["stage"] == "closed":
                steps.append("closed")
            if steps[-1] == "closed" or self.clock.now() > 60:
                self.clock.sleep(max(0, 990 - self.clock.now()))  # nothing more to see: on to the stop
        with mock.patch.object(host, "Driver", lambda *args, **kwargs: self.driver), \
                mock.patch.object(self.driver, "save", saved):
            code, why = host.run_shift(self.admission, self.spawn, seconds=1000, wind_down=10, poll=1, sleep=sleep)
        self.assertEqual(len(cut), 1)
        self.assertEqual(steps, ["review ended", "close sent", "closed"])
        self.assertEqual((code, why), (0, host.ENDING))
        state = self.state()
        self.assertEqual((state["tasks"]["task"]["stage"], state["last_review"]["outcome"]), ("closed", "ACCEPT"))
        self.assertEqual(len(self.review_files()), 1)

    def test_a_reviewer_that_changes_the_repository_or_the_receipts_fails_that_attempt(self):
        self.reach_review()
        (self.target / "source.txt").write_text("the reviewer wrote here\n")
        state = self.end_review()  # a sound ACCEPT, from a run during which the repository changed
        first, = state["review"]["attempts"]
        self.assertEqual((first["status"], first["repository"], first["verdict"]), ("failed", "changed", None))
        self.assertIn("changed while the review ran", " ".join(first["reasons"]))
        self.assertEqual(state["tasks"]["task"]["stage"], "review")
        (self.shift / "receipts" / "planted.json").write_text("{}")
        state = self.end_review()
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"]), ("FAILED", "blocked"))
        self.assertEqual([item["repository"] for item in self.review_receipt(state)["data"]["attempts"]],
                         ["changed", "changed"])

    def test_the_patch_of_a_task_with_two_commits_holds_both(self):
        self.begin("defect")
        self.assertEqual(self.task_state()["base"], self.candidate)  # where the repository was at the brief
        base = self.candidate
        for name in ("first.txt", "second.txt"):
            (self.target / name).write_text("added by " + name + "\n")
            self.candidate = self.commit()
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        patch = (self.directory() / "change.patch").read_text()
        for name in ("first.txt", "second.txt"):
            self.assertIn("+added by " + name, patch)
            self.assertTrue((self.directory() / "tree" / name).is_file())
        self.assertNotIn("source.txt", patch)  # committed before the task began
        state = self.end_review()
        data = self.review_receipt(state)["data"]
        self.assertEqual((data["base"], data["target"]["commit"]), (base, self.candidate))

    def wind_down_at(self, seconds):
        """A build handed in `seconds` into a 1000 second shift whose wind-down began at 300."""
        self.driver = Driver(self.admission, self.spawn, seconds=1000, wind_down=700)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(seconds)
        self.assertEqual(self.driver.active_seconds(), 0)
        return self.consume_phase()

    def test_in_wind_down_a_review_whose_whole_limit_fits_is_started(self):
        result = self.wind_down_at(390)  # 600 seconds of working time are left
        self.assertEqual(result["status"], "advanced", result)
        self.assertIn("started by the host", result["detail"])
        self.assertEqual(self.state()["review"]["running"]["limit_seconds"], 600)
        self.assertEqual(self.end_review()["tasks"]["task"]["stage"], "accepted")

    def given_to_make(self, seconds):
        """What `review_dir.make` was given for the review of a build handed in `seconds` into the
        shift of `wind_down_at`."""
        given = []
        make = review_dir.make

        def seen(*args, **named):
            given.append((named["evidence"], named["evidence_seconds"]))
            return make(*args, **named)
        with mock.patch.object(review_dir, "make", seen):
            result = self.wind_down_at(seconds)
        self.assertEqual(result["status"], "advanced", result)
        return given

    def test_evidence_is_shown_in_the_reviews_own_time_less_what_the_start_is_allowed(self):
        """Whether a review may start is decided when it is begun, and its end is fixed then.
        Preparing it, the evidence too, comes out of its own time. Evidence is given that
        time less what the host allows the work between the directory and the start (the
        look at the repository, the marks, the seat's start, the writes)."""
        given = self.given_to_make(382)  # in wind-down; the limit of 600 seconds fits with eight over
        self.assertEqual(len(given[0][0]), 2)
        self.assertEqual(START_RESERVE, 5 + 0.25 + 2 * WRITE_RESERVE + 1)
        self.assertEqual(given[0][1], 600 - START_RESERVE)
        self.assertTrue((self.directory() / "evidence" / "HOST.md").exists())

    def test_in_wind_down_a_review_whose_limit_only_just_fits_is_shown_the_evidence_too(self):
        """Until the begin decided, evidence was held to the time by which the limit still fit,
        and a review with seven seconds over got none."""
        given = self.given_to_make(383)
        self.assertEqual((len(given[0][0]), given[0][1]), (2, 600 - START_RESERVE))
        self.assertTrue((self.directory() / "evidence" / "HOST.md").exists())
        self.assertEqual(self.state()["review"]["running"]["limit_seconds"], 600)

    def slow_make(self, seconds, also=lambda: None):
        """`review_dir.make` that takes `seconds` of the fixture's clock, as an export of a large repository does."""
        make = review_dir.make

        def slow(*args, **named):
            made = make(*args, **named)
            self.clock.sleep(seconds)
            also()
            return made
        return mock.patch.object(review_dir, "make", slow)

    def test_a_review_begun_in_wind_down_while_its_limit_fit_starts_though_preparing_it_took_seconds(self):
        """The limit fit when the review was begun. That preparing the directory took three
        seconds does not lose the review: it ends when it would have, and runs that much shorter."""
        with self.slow_make(3):
            result = self.wind_down_at(390)  # exactly 600 seconds of working time are left
        self.assertEqual(result["status"], "advanced", result)
        self.assertIn("started by the host", result["detail"])
        self.assertEqual(self.driver.work_seconds(), 597)
        self.assertEqual(self.state()["review"]["running"]["limit_seconds"], 600)
        self.assertEqual(self.driver.turn_end, self.clock.now() + 597)  # fixed at the begin: not a second later
        self.assertEqual(self.end_review()["tasks"]["task"]["stage"], "accepted")

    def test_a_review_begun_before_wind_down_starts_though_wind_down_came_while_it_was_prepared(self):
        self.driver = Driver(self.admission, self.spawn, seconds=300, wind_down=100)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(196)
        self.assertEqual((self.driver.active_seconds(), self.driver.work_seconds()), (4, 94))
        with self.slow_make(6):
            result = self.consume_phase()
        self.assertEqual(result["status"], "advanced", result)
        self.assertIn("started by the host", result["detail"])
        self.assertEqual(self.driver.active_seconds(), 0)
        self.assertEqual(self.state()["review"]["running"]["limit_seconds"], 94)
        self.assertEqual(self.driver.turn_end, self.clock.now() + 88)
        self.assertTrue((self.directory() / "evidence" / "HOST.md").exists())

    def not_started(self, result, why):
        state = self.state()
        self.assertEqual((state["last_review"]["outcome"], state["review"]), ("FAILED", None), result)
        attempts = state["tasks"]["task"]["reviews"][-1]["attempts"]  # as the state keeps them, receipt or none
        self.assertEqual([item["ended_by"] for item in attempts], ["not started"])
        self.assertIn(why, attempts[0]["reasons"][0])
        self.assertEqual(len(self.spawn.started), 1)  # the builder turn of the brief and no reviewer

    def test_a_review_whose_own_time_ran_out_while_it_was_prepared_is_not_started(self):
        self.driver = Driver(self.admission, self.spawn, seconds=300, wind_down=10)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(289)
        self.assertEqual((self.driver.active_seconds(), self.driver.work_seconds()), (1, 6))
        with self.slow_make(6):
            result = self.consume_phase()
        # Its own time ends with the working time here, and with that the time of the command it was prepared
        # under: whichever of the checks finds it first, no reviewer is started.
        self.not_started(result, "not started: ")

    def test_a_review_whose_own_time_is_over_after_preparing_is_not_started_though_the_shift_has_time(self):
        """The review's own end alone: the command it was prepared under and the working time
        of the shift both have seconds left, and the review's limit of five is used up."""
        self.driver = Driver(self.admission, self.spawn, seconds=1000, wind_down=700)
        self.driver.review_limit = 5
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(980)
        self.assertEqual((self.driver.active_seconds(), self.driver.work_seconds()), (0, 10))
        with self.slow_make(6):
            result = self.consume_phase()
        self.assertEqual(self.driver.work_seconds(), 4)
        self.not_started(result, "the time for the review ran out while it was prepared")

    def slept_in_the_export(self, left):
        """The files are exported in half a second of the clock that runs while the machine is awake,
        and the machine slept meanwhile: `left` seconds of working time remain after it."""
        export = review_dir.export

        def slow(*args, **named):
            done = export(*args, **named)
            self.clock.sleep(0.5)
            self.admission.budget.end = self.clock.now() + left + self.driver.closing
            return done
        return mock.patch.object(review_dir, "export", slow)

    def test_evidence_does_not_take_the_working_time_that_is_left_after_the_machine_slept_in_the_export(self):
        """Found by the merge gate with a probe: what evidence may take was worked out before the
        export from the review's own time, which is of the clock that runs while the machine is
        awake. The machine sleeps in the export, four seconds of working time are left after
        it, evidence takes five within what it was given, and the reviewer that the required
        files alone would have had is not started. Evidence is held to the working time as it
        is after the export: with less than the start is allowed, none is shown."""
        self.driver = Driver(self.admission, self.spawn, seconds=300, wind_down=100)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(196)
        self.assertEqual(self.driver.work_seconds(), 94)
        show, shown = review_dir.show, []

        def five_seconds(*args, **named):
            shown.append(1)
            done = show(*args, **named)
            self.clock.sleep(5)
            return done
        with self.slept_in_the_export(4), mock.patch.object(review_dir, "show", five_seconds):
            result = self.consume_phase()
        self.assertEqual(result["status"], "advanced", result)
        self.assertIn("started by the host", result["detail"])
        self.assertEqual((shown, self.driver.work_seconds()), ([], 4))
        self.assertFalse((self.directory() / "evidence").exists())
        self.assertIn("The host did not show the builder's receipts to this review, nor its own statement of what "
                      "it saw of the repository", " ".join((self.directory() / "REVIEW.md").read_text().split()))
        self.assertEqual(self.driver.reviewing["evidence"], "late")

    def test_evidence_is_shown_after_a_sleep_in_the_export_that_left_working_time_beyond_what_the_start_is_allowed(self):
        self.driver = Driver(self.admission, self.spawn, seconds=300, wind_down=100)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(196)
        with self.slept_in_the_export(20):
            result = self.consume_phase()
        self.assertIn("started by the host", result["detail"])
        self.assertEqual(self.driver.reviewing["evidence"], "shown")
        self.assertTrue((self.directory() / "evidence" / "HOST.md").exists())

    def test_a_review_is_not_started_when_the_signed_deadline_came_closer_while_it_was_prepared(self):
        """The end of a review is fixed on the clock that only runs while the machine is awake.
        The signed deadline is a time of day. When the machine slept while a review was
        prepared, the working time can be over although the review's own time is not, and
        no reviewer is started into the time kept for the stop and the report."""
        def slept():
            self.admission.budget.end = self.clock.now() + self.driver.closing
        with self.slow_make(1, slept):
            result = self.wind_down_at(380)  # the limit fits, and of the review's own time one second is used
        self.assertEqual(self.driver.work_seconds(), 0)
        self.not_started(result, "the time for the review ran out while it was prepared")

    def test_in_wind_down_a_review_that_does_not_fit_is_not_started_and_the_work_stays_handed_in(self):
        started = len(self.spawn.started) + 1  # the builder turn of the brief
        result = self.wind_down_at(391)  # one second short
        self.assertEqual(result["status"], "advanced", result)  # the receipt is taken all the same
        self.assertIn(HELD.strip(), result["detail"])
        state = self.state()
        self.assertEqual((state["review"], state["last_review"], state["tasks"]["task"]["stage"]), (None, None, "review"))
        self.assertEqual(len(self.spawn.started), started)
        again = self.action("continue")
        self.assertEqual(again["status"], "blocked", again)
        self.assertIn(HELD.strip(), again["detail"])
        self.assertEqual(len(self.spawn.started), started)
        self.driver.finish("window ended")
        self.assertIn("handed in and not reviewed", (self.shift / "report.md").read_text())

    def test_a_failed_review_is_not_run_again_in_wind_down_when_the_limit_no_longer_fits(self):
        self.wind_down_at(390)
        self.clock.sleep(5)
        state = self.end_review("nothing usable\n")
        task = state["tasks"]["task"]
        self.assertEqual((state["last_review"]["outcome"], state["review"], task["stage"], state["active"]),
                         ("FAILED", None, "review", "task"))
        self.assertIn("was not run again", state["last_review"]["detail"])
        self.assertEqual(len(self.review_receipt(state)["data"]["attempts"]), 1)

    def test_a_review_left_by_an_earlier_host_is_lost_and_blocks_the_shift(self):
        self.reach_review()
        child = self.spawn.child
        self.driver = Driver(self.admission, self.spawn, seconds=900, wind_down=10)
        self.driver.poll_turn()  # nothing owned here: no signal, no record
        self.assertIsNotNone(self.state()["review"]["running"])
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["detail"], LOST_REVIEW)
        state = self.state()
        self.assertEqual((state["mode"], state["next_owner"], state["review"], state["last_review"]["outcome"]),
                         ("blocked", "developer", None, "lost"))
        self.assertEqual([item["outcome"] for item in state["tasks"]["task"]["reviews"]], ["lost"])
        self.assertEqual(state["tasks"]["task"]["stage"], "review")
        self.assertEqual(child.signals, [])

    def test_a_note_sent_while_a_review_runs_is_answered_and_the_review_ends_as_it_would_have(self):
        from sortie_kit import receipts, shift_wait
        self.reach_review()
        before = self.state()
        command = dict(version=1, id="doubt", op="note", kind="question", text="Is the prior window the same length?",
                       task_id="task")
        raw = receipts.encoded(command)
        (self.shift / "driver-command.json").write_bytes(raw)
        self.driver.poll()
        seen, _ = shift_wait.observe(self.shift, 5)
        # The wait goes on for the review and already carries the answer to this command and no other.
        self.assertEqual((seen["status"], seen["answered"], seen["response"]["id"], seen["response"]["status"]),
                         ("waiting", True, "doubt", "recorded"))
        self.assertEqual(seen["review"]["task_id"], "task")
        state = self.state()
        self.assertEqual((state["review"], state["tasks"]["task"], state["turn"]),
                         (before["review"], before["tasks"]["task"], before["turn"]))
        started = len(self.spawn.started)
        state = self.end_review()
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"]), ("ACCEPT", "accepted"))
        self.assertEqual(len(self.spawn.started), started)
        self.assertEqual([(n["kind"], n["task_id"]) for n in state["notes"]], [("question", "task")])
        seen, _ = shift_wait.observe(self.shift, 5)
        self.assertEqual((seen["status"], seen["response"]["id"], seen["last_review"]["outcome"]), ("ready", "doubt", "ACCEPT"))

    def test_the_driver_cannot_hand_in_a_review_receipt_or_act_on_the_task_while_a_review_runs(self):
        self.reach_review()
        for op, extra in (("consume", dict(receipt="receipts/mine.json")), ("close", {}),
                          ("repair", dict(reason="no", next_owner="builder"))):
            result = self.action(op, **extra)
            self.assertEqual(result["status"], "blocked", result)
            self.assertIn("a review is running", result["detail"])
        again = self.action("continue")
        self.assertEqual(again["status"], "blocked", again)
        self.assertEqual(len(self.spawn.started), 2)

    def test_a_reviewer_that_cannot_be_started_is_a_failed_attempt_with_its_reason(self):
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.spawn.failure = OSError("no such program")
        result = self.consume_phase()
        self.assertEqual(result["status"], "blocked", result)
        state = self.state()
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"], state["active"]),
                         ("FAILED", "blocked", None))
        attempts = self.review_receipt(state)["data"]["attempts"]
        self.assertEqual([item["ended_by"] for item in attempts], ["not started", "not started"])
        self.assertIn("no such program", attempts[0]["reasons"][0])

    def test_the_receipt_check_takes_a_complete_attempt_only_as_the_host_takes_one(self):
        """A receipt that calls an attempt complete and says it did not end on its own with
        status 0 and all its output kept is refused by the check, whoever presents it."""
        from sortie_kit import receipts
        self.reach_review()
        state = self.end_review()
        good = self.review_receipt(state)
        expected = "briefs/expected-" + good["request_id"] + ".json"
        def checked(change):
            value = json.loads(json.dumps(good))
            change(value["data"]["attempts"][-1])
            value["receipt_id"] = "tried-" + str(len(list((self.shift / "receipts").iterdir())))
            path = "receipts/" + value["receipt_id"] + ".json"
            (self.shift / path).write_bytes(receipts.encoded(value))
            return receipts.validate_receipt(self.shift, path, expected, seconds=10)
        # The copy as the host wrote it is sound, and only its slot is taken already.
        self.assertEqual(checked(lambda last: None)["status"], "duplicate")
        for label, change in (("exit status", lambda last: last.update(exit_status=3)),
                              ("stopped at its limit", lambda last: last.update(ended_by="limit")),
                              ("output dropped", lambda last: last.update(output_dropped_bytes=9)),
                              ("a reason for failure", lambda last: last.update(reasons=["it failed"])),
                              ("no command", lambda last: last.update(argv=[]))):
            with self.subTest(label=label):
                with self.assertRaisesRegex(Refused, "a complete review attempt ended on its own with status 0"):
                    checked(change)

    def test_a_review_whose_end_could_not_be_written_in_time_is_written_at_the_next_chance_and_not_lost(self):
        from unittest import mock
        from sortie_kit.layout import Elapsed
        self.reach_review()
        self.prints(self.review_output().encode())
        self.spawn.child.code = 0
        with mock.patch.object(self.driver, "finalize", side_effect=Elapsed("no time left under this lock")):
            with self.assertRaises(Elapsed):
                self.driver.poll_turn()
        self.assertIsNotNone(self.state()["review"]["running"])  # nothing of the end reached the disk
        self.driver.record_turn()
        state = self.state()
        self.assertEqual((state["last_review"]["outcome"], state["review"], state["tasks"]["task"]["stage"]),
                         ("ACCEPT", None, "accepted"))
        self.assertEqual(self.driver.settle(state), False)

    def stopped_after_the_receipt(self, name, *, retry=True, on=None, recorded=True, **kwargs):
        """The review ends, its receipt is written and its slot recorded, and the time under
        the lock runs out at `name` (a Driver method called after that and before the state
        is on disk; or a function of the module `on`). Then the next chance to write the
        end, and the state after it."""
        from unittest import mock
        from sortie_kit.layout import Elapsed
        self.prints(self.review_output(**kwargs).encode() if "text" not in kwargs else kwargs["text"].encode())
        self.spawn.child.code = 0
        with mock.patch.object(on or self.driver, name, side_effect=Elapsed("no time left under this lock")) as cut:
            with self.assertRaises(Elapsed):
                self.driver.poll_turn()
        self.assertEqual(cut.call_count, 1)
        state = self.state()
        self.assertIsNotNone(state["review"]["running"])  # the state on disk still has the review running
        self.recorded = [entry for entry in self.ledger()["entries"] if entry["receipt_id"].startswith("review-")]
        self.assertEqual(len(self.recorded), 1 if recorded else 0)
        self.assertEqual(len(self.review_files()), 1)  # the receipt file is there either way
        if retry:
            self.driver.record_turn()
        return self.state()

    def ledger(self):
        return decode((self.shift / "receipts" / ".consumed.json").read_bytes())

    def review_files(self):
        return sorted(path.name for path in (self.shift / "receipts").iterdir() if path.name.startswith("review-"))

    def test_an_accept_whose_receipt_was_recorded_and_whose_state_was_not_saved_is_taken_up_and_not_blocked(self):
        """The open Major of the part 2 re-check: the second try found the slot taken, could
        not write a second receipt, and blocked a task the reviewer had accepted."""
        self.reach_review()
        state = self.stopped_after_the_receipt("save")
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((last["outcome"], state["review"], task["stage"], task["reason"]), ("ACCEPT", None, "accepted", ""))
        only, = self.recorded
        self.assertEqual(self.review_files(), [only["receipt_id"] + ".json"])  # one receipt, the recorded one
        self.assertEqual(last["receipt"], dict(path="receipts/" + only["receipt_id"] + ".json", sha256=only["sha256"]))
        self.assertEqual(task["accepted"]["receipt"]["path"], last["receipt"]["path"])
        self.assertEqual([item["stage"] for item in task["receipts"]].count("review"), 1)
        attempt, = task["reviews"][-1]["attempts"]
        self.assertEqual((attempt["status"], attempt["verdict"], attempt["repository"]), ("complete", "ACCEPT", "unchanged"))
        self.assertEqual(self.review_receipt(state)["data"]["attempts"][0]["ended"], attempt["ended"])
        self.assertEqual(self.driver.settle(state), False)
        self.assertEqual(self.action("close")["status"], "closed")

    def test_the_same_at_the_closeout_of_the_shift(self):
        self.reach_review()
        self.stopped_after_the_receipt("save", retry=False)
        self.driver.finish("the fixture ends the shift")
        state = self.state()
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"]), ("ACCEPT", "accepted"))
        self.assertEqual(len(self.review_files()), 1)

    def test_changes_whose_receipt_was_recorded_before_the_findings_file_reach_the_builder_all_the_same(self):
        self.reach_review()
        state = self.stopped_after_the_receipt("outstanding", changes=True)
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((last["outcome"], task["stage"], last["findings"]),
                         ("CHANGES", "repair_needed", [dict(id="N1", severity="major")]))
        handed = decode((self.shift / task["findings"]["candidate"]["path"]).read_bytes())
        self.assertEqual([item["id"] for item in handed["findings"]], ["N1"])
        self.assertEqual(handed["review_receipt"], "receipts/" + self.recorded[0]["receipt_id"] + ".json")
        self.assertEqual(len(self.review_files()), 1)
        self.assertEqual(self.action("repair", reason="Address N1", next_owner="builder")["status"], "advanced")

    def test_a_review_that_failed_twice_and_was_recorded_is_told_as_that_and_not_as_a_receipt_that_could_not_be_written(self):
        self.reach_review()
        self.end_review("I looked and it seems fine.\n")
        state = self.stopped_after_the_receipt("save", text="Still fine.\n")
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((last["outcome"], task["stage"], state["active"]), ("FAILED", "blocked", None))
        self.assertIn("review failed twice", task["reason"])
        self.assertEqual(last["receipt"]["path"], "receipts/" + self.recorded[0]["receipt_id"] + ".json")
        self.assertEqual(len(self.review_receipt(state)["data"]["attempts"]), 2)
        self.assertEqual(len(self.review_files()), 1)

    def test_a_receipt_that_was_written_and_not_yet_recorded_is_recorded_at_the_next_chance_and_not_written_twice(self):
        """Without this the host's own receipt file made the second try see receipts/ as
        changed by the reviewer."""
        from sortie_kit import receipts
        self.reach_review()
        state = self.stopped_after_the_receipt("validate_receipt", on=receipts, recorded=False)
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((last["outcome"], task["stage"]), ("ACCEPT", "accepted"))
        only, = self.review_files()
        self.assertEqual(last["receipt"]["path"], "receipts/" + only)
        attempt, = task["reviews"][-1]["attempts"]
        self.assertEqual((attempt["status"], attempt["repository"]), ("complete", "unchanged"))
        self.assertEqual(len(self.spawn.started), 2)  # the builder turn and the one run of the reviewer

    def refused_at_the_next_chance(self, change, why):
        """The receipt is recorded, the state is not saved, and before the next chance
        something in receipts/ is no longer what this host wrote and recorded."""
        self.reach_review()
        self.stopped_after_the_receipt("save", retry=False)
        only, = self.recorded
        change(self.shift / "receipts" / (only["receipt_id"] + ".json"), only)
        self.driver.record_turn()
        state = self.state()
        task = state["tasks"]["task"]
        self.assertEqual((state["last_review"]["outcome"], task["stage"], task["accepted"]), ("FAILED", "blocked", None))
        self.assertIn(why, task["reason"])

    def test_a_recorded_receipt_that_changed_since_it_was_recorded_is_not_taken_up(self):
        def change(path, entry):
            value = decode(path.read_bytes())
            value["data"]["attempts"][-1]["repository"] = "not checked"
            path.write_bytes(json.dumps(value).encode())
        self.refused_at_the_next_chance(change, "a receipt that this host did not write is on record")

    def test_a_ledger_entry_for_other_bytes_than_this_host_wrote_is_not_taken_up(self):
        from sortie_kit import receipts
        def change(path, entry):
            ledger = self.ledger()
            for item in ledger["entries"]:
                if item["receipt_id"] == entry["receipt_id"]:
                    item["sha256"] = "0" * 64
            (self.shift / "receipts" / ".consumed.json").write_bytes(receipts.encoded(ledger))
        self.refused_at_the_next_chance(change, "a receipt that this host did not write is on record")

    def test_a_review_receipt_a_seat_recorded_while_the_review_ran_is_never_taken_as_the_hosts(self):
        """A reviewer knows its marker, its command and its own output. A receipt it writes
        about them and records with `sortie receipt validate --record` takes the slot;
        it is not what the host built, so it backs nothing and the task is not accepted."""
        from sortie_kit import receipts
        self.reach_review()
        review, task = self.state()["review"], self.state()["tasks"]["task"]
        running = review["running"]
        output = self.review_output().encode()
        self.prints(output)
        self.driver.poll_turn()  # the review still runs; the host moves what it printed so far into the log
        record = json.loads(output.decode().split("=====\n", 1)[1].rsplit("\n=====END", 1)[0])
        expected = decode((self.shift / "current-context.json").read_bytes())
        attempt = dict(attempt=1, directory=running["directory"], instructions_sha256="0" * 64, marker=running["marker"],
                       argv=self.spawn.started[-1]["argv"], started=running["started"], ended=running["started"],
                       elapsed_seconds=1, exit_status=0, ended_by="exit",
                       stdout=dict(path=running["stdout"], sha256=receipts.digest(output)), stderr=None,
                       output_dropped_bytes=0, status="complete", verdict="ACCEPT", reasons=[], record=record,
                       repository="unchanged")
        forged = dict(version=3, receipt_id="review-forged", shift_id=self.shift.name, task_id="task",
                      request_id=task["request_id"], role="reviewer", phase="review", round=task["round"],
                      brief=task["brief"],
                      data=dict(target=review["target"], base=task["base"],
                                implementer_family=expected["builder"]["family"], reviewer=expected["reviewer"],
                                limit_seconds=600, outcome="ACCEPT", prior=[], attempts=[attempt]))
        (self.shift / "receipts" / "review-forged.json").write_bytes(receipts.encoded(forged))
        context = "briefs/forged-expected.json"
        (self.shift / context).write_bytes(receipts.encoded(self.driver.review_expected(task)))
        taken = receipts.validate_receipt(self.shift, "receipts/review-forged.json", context, record=True)
        self.assertEqual((taken["status"], taken["recorded"]), ("satisfied", True), taken)  # the seat's record stands
        self.spawn.child.code = 0
        self.driver.poll_turn()
        state = self.state()
        while state["review"] is not None and state["review"]["running"] is not None:
            state = self.end_review()  # the run the host makes once more
        task = state["tasks"]["task"]
        self.assertEqual((state["last_review"]["outcome"], task["stage"], task["accepted"]), ("FAILED", "blocked", None))
        self.assertIsNone(state["last_review"]["receipt"])
        # Refused for whose receipt is on record, on a second run that was itself a sound ACCEPT.
        self.assertEqual(task["reviews"][-1]["attempts"][-1]["verdict"], "ACCEPT")
        self.assertIn("a receipt that this host did not write is on record", task["reason"])

    def test_a_receipt_file_that_changed_before_it_was_recorded_is_not_recorded(self):
        from sortie_kit import receipts
        self.reach_review()
        self.stopped_after_the_receipt("validate_receipt", on=receipts, recorded=False, retry=False)
        only, = self.review_files()
        (self.shift / "receipts" / only).write_bytes(b"{}")
        self.driver.record_turn()
        state = self.state()
        task = state["tasks"]["task"]
        self.assertEqual((state["last_review"]["outcome"], task["stage"], task["accepted"]), ("FAILED", "blocked", None))
        self.assertIn("the review receipt changed since this host wrote it", task["reason"])

    def test_an_attempt_that_could_not_start_is_not_run_again_under_a_lock_with_no_time_left(self):
        """The second attempt needs time under the lock to make its directory: without it the
        review has failed once, the work stays handed in, and `continue` runs it on."""
        from unittest import mock
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.spawn.failure = OSError("no such program")
        # Only the room a further attempt needs is taken away. The lock itself keeps its
        # time for the record of the failure: with half a second of room that record was
        # given 50 ms, which on the machine's clock a loaded machine did not always meet.
        with mock.patch("sortie_kit.lifecycle.PREPARE_ROOM", 10 ** 6):
            _, path = self.phase_receipt(target={"kind": "commit", "commit": self.candidate})
            self.action("consume", receipt=path, target={"kind": "commit", "commit": self.candidate})
        state = self.state()
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"], state["active"]),
                         ("FAILED", "review", "task"))
        self.assertEqual(len(self.review_receipt(state)["data"]["attempts"]), 1)
        self.spawn.failure = None
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.assertEqual(self.end_review()["last_review"]["outcome"], "ACCEPT")

    def out_of_time_at_the_record(self, calls):
        """The check of the receipt the host writes for a review finds no time left; a
        builder's own receipt is checked as ever."""
        from unittest import mock
        from sortie_kit import receipts
        from sortie_kit.layout import Elapsed
        real = receipts.validate_receipt

        def check(*args, **kwargs):
            calls.append(args[1])
            if args[1].startswith("receipts/review-"):
                raise Elapsed("duration elapsed")
            return real(*args, **kwargs)
        return mock.patch.object(receipts, "validate_receipt", check)

    def test_a_failed_start_whose_record_ran_out_of_time_is_a_failed_review_on_record_and_the_shift_goes_on(self):
        """The attempt could not start and nothing is left under the lock to record a
        receipt for it. That is a shortage of time under one lock and no damage: the
        shift is not blocked, the review is kept as failed with no receipt, so the wait
        and the report show it, nothing is left begun, and `continue` starts another."""
        from unittest import mock
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.spawn.failure = OSError("no such program")
        calls = []
        with mock.patch("sortie_kit.lifecycle.PREPARE_ROOM", 10 ** 6), self.out_of_time_at_the_record(calls):
            _, path = self.phase_receipt(target={"kind": "commit", "commit": self.candidate})
            answer = self.action("consume", receipt=path, target={"kind": "commit", "commit": self.candidate})
        self.assertEqual((len(calls), calls[0], calls[1].startswith("receipts/review-")), (2, path, True))
        state = self.state()
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((answer["status"], state["mode"], state["pending"], state["active"], state["review"]),
                         ("advanced", "active", None, "task", None))
        self.assertEqual((task["stage"], last["outcome"], last["receipt"], len(task["reviews"])), ("review", "FAILED", None, 1))
        kept = task["reviews"][0]
        self.assertEqual((kept["outcome"], kept["receipt"], len(kept["attempts"]), kept["detail"]),
                         ("FAILED", None, 1, last["detail"]))
        self.assertIn("no such program", kept["attempts"][0]["reasons"][0])
        for part in ("could not start", "no such program", "the time under the host's lock ran out",
                     "backs nothing", "Send `continue`"):
            self.assertIn(part, answer["detail"])
            self.assertIn(part, last["detail"])
        # The wait shows the failed review, and the state loads in a host started later.
        from sortie_kit import shift_wait
        (self.shift / "driver-command.json").unlink(missing_ok=True)
        reading = shift_wait.observe(self.shift)[0]
        self.assertEqual((reading["status"], reading["review"], reading["last_review"]["outcome"]),
                         ("ready", None, "FAILED"))
        self.spawn.failure = None
        self.assertEqual(self.action("continue")["status"], "recorded")
        state = self.state()
        self.assertEqual((state["review"]["review"], state["review"]["running"]["attempt"]), (2, 1))
        state = self.end_review()
        self.assertEqual((state["last_review"]["outcome"], state["tasks"]["task"]["stage"]), ("ACCEPT", "accepted"))
        self.assertEqual(len(state["tasks"]["task"]["reviews"]), 2)

    def test_a_second_attempt_that_could_not_start_with_no_time_for_the_record_is_not_left_untold(self):
        """The host runs a failed attempt once more by itself, with no command of the
        driver's to answer. When that second attempt cannot start and the record runs
        out of time, the review is on record as failed twice and the task is blocked,
        as for any review that failed twice; nothing is left begun for a `continue`
        that nobody was told to send."""
        self.reach_review()
        self.spawn.failure = OSError("no such program")
        calls = []
        with self.out_of_time_at_the_record(calls):
            state = self.end_review("I looked and it seems fine.\n")
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((state["review"], state["mode"], state["active"], task["stage"]), (None, "active", None, "blocked"))
        self.assertEqual((last["outcome"], last["receipt"], len(task["reviews"][0]["attempts"])), ("FAILED", None, 2))
        self.assertIn("review failed twice", task["reason"])
        self.assertIn("no such program", task["reason"])
        self.spawn.failure = None
        result = self.action("brief", task_id="next", kind="defect", brief=self.artifact("briefs/task.md"),
                             next_owner="builder")
        self.assertEqual(result["status"], "recorded", result)

    def out_of_time_at_the_second_attempts_intent(self, *, landed, record_fails=False):
        """The host runs a failed attempt once more by itself. The save of that second
        attempt's intent finds the time under the lock over: before anything was written,
        or (`landed`) after the state file took it. `record_fails`: the write of the
        record that follows, on its own time, runs out as well."""
        from unittest import mock
        from sortie_kit.layout import Elapsed
        real, calls = self.driver.save, []

        def save(view, state):
            calls.append(1)
            if len(calls) == 1:
                if landed:
                    real(view, state)
                raise Elapsed("duration elapsed")
            if len(calls) == 2 and record_fails:
                raise Elapsed("duration elapsed")
            return real(view, state)
        return mock.patch.object(self.driver, "save", save)

    def failed_once_and_told(self, state):
        """The review is on record as failed with its one attempt, nothing is left begun,
        the work stays handed in, and the wait shows it to a driver nobody answered."""
        from sortie_kit import shift_wait
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual((state["review"], state["mode"], state["active"], task["stage"]), (None, "active", "task", "review"))
        self.assertEqual((last["outcome"], last["receipt"], len(task["reviews"])), ("FAILED", None, 1))
        self.assertEqual([item["attempt"] for item in task["reviews"][0]["attempts"]], [1])
        for part in ("attempt 2 could not start", "the time under the host's lock ran out", "Send `continue`"):
            self.assertIn(part, last["detail"])
        (self.shift / "driver-command.json").unlink(missing_ok=True)
        reading = shift_wait.observe(self.shift)[0]
        self.assertEqual((reading["status"], reading["review"], reading["last_review"]["outcome"]),
                         ("ready", None, "FAILED"))
        started = len(self.spawn.started)
        self.assertEqual(self.action("continue")["status"], "recorded")
        state = self.state()
        self.assertEqual((len(self.spawn.started), state["review"]["review"], state["review"]["running"]["attempt"]),
                         (started + 1, 2, 1))
        self.assertEqual(self.end_review()["last_review"]["outcome"], "ACCEPT")

    def test_a_second_attempt_whose_intent_could_not_be_saved_in_time_is_not_left_begun_and_untold(self):
        """No command of the driver's is answered here, so a review left begun with no
        attempt running would be known to nobody and be in no report."""
        self.reach_review()
        with self.out_of_time_at_the_second_attempts_intent(landed=False):
            state = self.end_review("I looked and it seems fine.\n")
        self.failed_once_and_told(state)

    def test_the_same_when_that_intent_had_reached_the_state_file(self):
        self.reach_review()
        with self.out_of_time_at_the_second_attempts_intent(landed=True):
            state = self.end_review("I looked and it seems fine.\n")
        self.failed_once_and_told(state)

    def test_the_end_of_a_first_attempt_is_never_taken_as_the_end_of_a_second_that_did_not_run(self):
        """The second attempt's intent reached the state file, the time was found over, and
        the record on its own time ran out too. The host still holds what it saw of the
        first attempt's end. At the next chance that is not the end of attempt 2, which
        no process ever ran: the review is failed once, and not twice with a made-up attempt."""
        from sortie_kit.layout import Elapsed
        self.reach_review()
        with self.out_of_time_at_the_second_attempts_intent(landed=True, record_fails=True):
            self.prints(b"I looked and it seems fine.\n")
            self.spawn.child.code = 0
            with self.assertRaises(Elapsed):
                self.driver.poll_turn()
        on_file = self.state()["review"]
        self.assertEqual((on_file["running"]["attempt"], len(on_file["attempts"])), (2, 1))
        started = len(self.spawn.started)
        self.driver.record_turn(live=True)
        self.assertEqual(len(self.spawn.started), started)
        self.failed_once_and_told(self.state())

    def test_no_reviewer_is_started_by_a_command_whose_time_is_over(self):
        """The start check of the reviewer took longer than a command has. No process is
        started after that: the review is kept as failed once, as any attempt that could
        not start with no time left for its receipt, and `continue` starts another."""
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        started, real = len(self.spawn.started), self.admission.seat_start

        def seat_start(role, prompt, cwd=None):
            self.clock.sleep(16)
            return real(role, prompt, cwd)
        self.admission.seat_start = seat_start
        try:
            _, path = self.phase_receipt(target={"kind": "commit", "commit": self.candidate})
            answer = self.action("consume", receipt=path, target={"kind": "commit", "commit": self.candidate})
        finally:
            self.admission.seat_start = real
        state = self.state()
        task, last = state["tasks"]["task"], state["last_review"]
        self.assertEqual(len(self.spawn.started), started)
        self.assertEqual((answer["status"], state["mode"], state["pending"], state["review"], task["stage"]),
                         ("advanced", "active", None, None, "review"))
        self.assertEqual((last["outcome"], last["receipt"]), ("FAILED", None))
        self.assertIn("time of the command ran out", last["detail"])
        self.assertEqual(self.action("continue")["status"], "recorded")
        self.assertEqual(self.end_review()["last_review"]["outcome"], "ACCEPT")

    def test_the_repository_is_untouched_by_the_host_making_a_review_directory(self):
        before = subprocess.check_output(["git", "-C", str(self.target), "status", "--porcelain"], timeout=5)
        self.reach_review()
        self.end_review()
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.target), "status", "--porcelain"], timeout=5),
                         before)


class PackagedTests(unittest.TestCase):
    def test_no_packaged_file_names_the_earlier_review_script_or_a_skills_directory(self):
        from importlib.resources import files
        def packaged(node):
            for item in node.iterdir():
                if item.is_dir():
                    yield from packaged(item)
                elif not item.name.endswith(".pyc"):
                    yield item
        seen = 0
        for item in packaged(files("sortie_kit")):
            seen += 1
            text = item.read_bytes().decode("utf-8", "replace")
            for name in ("agy_review.py", ".agents/skills", ".claude/skills", ".codex/skills"):
                self.assertNotIn(name, text, str(item))
        self.assertGreater(seen, 40)  # the modules, templates and schemas were really walked


class HostReviewReportTests(ReviewFixture):
    """What the report of a shift says about the reviews the host ran."""
    def report(self, reason="the fixture ends the shift"):
        self.driver.finish(reason)
        return (self.shift / "report.md").read_text(), decode((self.shift / "report.json").read_bytes())

    def test_an_accept_after_a_round_of_changes_is_reported_with_both_reviews_and_the_answers(self):
        from sortie_kit import report
        self.reach_review()
        self.end_review(changes=True)
        self.action("repair", reason="Address N1", next_owner="builder")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.end_review()
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual((doc["tasks"][0]["acceptance"], doc["issues"]), (report.ACCEPTED, []))
        for part in ("### task (defect): accepted locally",
                     "- Tasks: 1 accepted locally of 1 briefed",
                     "- Result: accepted locally. The host's review of commit " + self.candidate + " ended in ACCEPT",
                     "- Reviews, started by the host with the signed reviewer (fixture, model example-review-model, "
                     "family example-review-family):",
                     "  - Review 1, of the candidate, round 0: CHANGES on commit " + self.candidate + "; 1 finding.",
                     "    - N1 (major) at source.txt:1: wrong word",
                     "  - Review 2, of the candidate, round 1: ACCEPT on commit " + self.candidate
                     + "; no findings; earlier findings resolved: N1.",
                     "    - answers to the review's findings: N1 fixed",
                     "- Repair rounds used: 1 of 3."):
            self.assertIn(part, text)
        # A review is the host's work: it is not listed as something the builder handed in,
        # and nothing of the earlier review tool is named.
        handed = text.split("- What the builder handed in, as the builder reported it:")[1].split("- Reviews, started")[0]
        self.assertNotIn("review", handed.replace("answers to the review's findings", ""))
        for gone in ("agy", "the driver recorded acceptance", "same-family"):
            self.assertNotIn(gone, text)
        # The fixture's grant gives its seats no model; a signed grant's reviewer seat is read in test_start_live.
        self.assertIn("- Reviewer: fixture, model", text.split("## What the kept grant says")[1])
        self.assertIn("picked: the first installed agent of another family than the builder",
                      text.split("## What the kept grant says")[1])
        self.assertEqual([item["outcome"] for item in doc["tasks"][0]["reviews"]], ["CHANGES", "ACCEPT"])

    def test_a_first_attempt_that_failed_and_a_second_that_accepted_is_reported_accepted_with_both_shown(self):
        from sortie_kit import report
        self.reach_review()
        self.end_review(code=3)
        self.end_review()
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual((doc["tasks"][0]["acceptance"], doc["issues"]), (report.ACCEPTED, []))
        for part in ("### task (defect): accepted locally",
                     "  - Review 1, of the candidate, round 0: ACCEPT on commit " + self.candidate + "; no findings; 2 attempts.",
                     "    - Attempt 1: failed (exit with status 3",
                     "    - Attempt 2: complete, ACCEPT"):
            self.assertIn(part, text)

    def test_a_review_that_failed_twice_is_reported_as_blocking_the_task_without_a_repair_round(self):
        self.reach_review()
        self.end_review("I looked and it seems fine.\n")
        self.end_review("Still fine.\n")
        text, doc = self.report()
        task = doc["tasks"][0]
        self.assertEqual((task["acceptance"], task["stage"], task["round"]), ("acceptance missing", "blocked", 0))
        for part in ("### task (defect): not accepted",
                     "review failed twice",
                     "  - Review 1, of the candidate, round 0: FAILED on commit " + self.candidate
                     + ": the reviewer gave nothing the host could take, in 2 attempts. No verdict was taken and no "
                       "repair round was used.",
                     "    - Attempt 1: failed (", "    - Attempt 2: failed ("):
            self.assertIn(part, text)
        self.assertNotIn("Repair rounds used", text)

    def test_a_reviewer_that_changed_the_repository_is_reported(self):
        self.reach_review()
        (self.target / "source.txt").write_text("the reviewer wrote here\n")
        self.end_review()
        self.end_review()
        text, _ = self.report()
        self.assertIn("**the repository or the shift's receipts changed while this attempt ran**", text)
        # The second attempt saw the changed file before it began, so it could accept.
        self.assertIn("    - Attempt 1: failed", text)

    def test_work_handed_in_and_not_reviewed_is_told_apart_from_work_that_was_reviewed(self):
        self.driver = Driver(self.admission, self.spawn, seconds=1000, wind_down=700)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(391)  # in wind-down, one second short of the review's limit
        self.assertIn(HELD.strip(), self.consume_phase()["detail"])
        text, doc = self.report("window ended")
        self.assertIsNone(doc["tasks"][0].get("reviews"))
        self.assertIn("- Handed in and not reviewed: the host started no review of commit " + self.candidate, text)
        self.assertIn("### task (defect): not accepted", text)
        self.assertNotIn("- Reviews, started by the host", text)

    def test_a_review_that_failed_once_and_was_not_run_again_leaves_the_work_not_reviewed(self):
        self.driver = Driver(self.admission, self.spawn, seconds=1000, wind_down=700)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(390)
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(5)
        self.end_review("nothing usable\n")
        text, doc = self.report("window ended")
        self.assertIn("- Handed in and not reviewed: the review of commit " + self.candidate
                      + " failed and was not run again", text)
        self.assertIn("  - Review 1, of the candidate, round 0: FAILED on commit", text)
        self.assertEqual(doc["tasks"][0]["acceptance"], "acceptance missing")

    def test_a_same_family_review_is_said_as_that_with_its_reason(self):
        from sortie_kit import receipts
        self.value["review_policy"].update(review_family="example-builder-family", same_family_review=True,
                                           review_choice="picked-same-family")
        raw = receipts.encoded(self.value)
        (self.shift / "grant.json").write_bytes(raw)
        self.admission.raw = raw
        self.driver = Driver(self.admission, self.spawn, seconds=1000, wind_down=10)
        self.reach_review()
        self.end_review()
        self.action("close")
        text, doc = self.report()
        said = ("same-family review: the reviewer is of the builder's own family (example-builder-family), because "
                "no agent of another family is installed")
        self.assertIn("- Reviews: " + said, text.split("## What was asked")[0])
        self.assertIn(said, text.split("## What the kept grant says")[1])
        self.assertIn("picked: no agent of another family is installed", text)
        self.assertEqual(doc["tasks"][0]["acceptance"], "local acceptance recorded; retained review matches candidate")
        from sortie_kit import launcher
        lines = launcher.closing_lines((self.shift / "report.json").read_bytes(), 0, str)
        self.assertIn("Reviews: " + said + ".", lines)

    def test_an_accept_receipt_that_is_not_what_the_host_would_have_taken_backs_no_acceptance(self):
        """Kept files that agree with each other and still describe no accept of the host's."""
        from sortie_kit import receipts, report
        self.reach_review()
        state = self.end_review()
        self.action("close")
        name = state["last_review"]["receipt"]["path"]
        good = {item: (self.shift / item).read_bytes() for item in (name, "driver-state.json", "receipts/.consumed.json")}
        for label, change in (("ended by the limit", lambda last: last.update(ended_by="limit")),
                              ("output dropped", lambda last: last.update(output_dropped_bytes=7)),
                              ("no output kept", lambda last: last.update(stdout=None)),
                              ("no marker", lambda last: last.update(marker=None))):
            with self.subTest(label=label):
                for item, raw in good.items():
                    (self.shift / item).write_bytes(raw)
                value = decode(good[name])
                change(value["data"]["attempts"][-1])
                raw = receipts.encoded(value)
                (self.shift / name).write_bytes(raw)
                old, new = receipts.digest(good[name]).encode(), receipts.digest(raw).encode()
                for item in ("driver-state.json", "receipts/.consumed.json"):
                    self.assertIn(old, good[item])
                    (self.shift / item).write_bytes(good[item].replace(old, new))
                self.driver.cleaned = True
                doc = report.finalize(self.shift, 3)
                self.assertEqual(doc["tasks"][0]["acceptance"], report.UNBACKED, doc["issues"])

    def test_check_output_written_over_after_the_build_was_consumed_still_backs_the_acceptance(self):
        # As a later builder turn did on a real shift: it gave its test log the name an
        # earlier, accepted task's build receipt had hashed.
        from sortie_kit import receipts, report
        self.reach_review()
        state = self.end_review()
        self.action("close")
        build = next(r for r in state["tasks"]["task"]["receipts"] if r["stage"] == "build")
        outputs = [c["output"] for c in decode((self.shift / build["path"]).read_bytes())["data"]["checks"]]
        self.assertTrue(outputs)
        for ref in outputs:
            self.assertEqual(receipts.digest((self.shift / receipts.kept_path(ref["sha256"])).read_bytes()), ref["sha256"])
            (self.shift / ref["path"]).write_text("the output of another task's run\n")
        text, doc = self.report()
        self.assertEqual((doc["tasks"][0]["acceptance"], doc["issues"], doc["status"]),
                         (report.ACCEPTED, [], "evidence summarized"))
        self.assertIn("- Tasks: 1 accepted locally of 1 briefed", text)
        for ref in outputs:
            self.assertIn(ref["path"] + " was written over or removed after the host took this receipt; the host's "
                          "copy from then is " + receipts.kept_path(ref["sha256"]), text)

    def test_a_review_receipt_that_changed_after_the_host_wrote_it_backs_no_acceptance(self):
        from sortie_kit import report
        self.reach_review()
        state = self.end_review()
        self.action("close")
        path = self.shift / state["last_review"]["receipt"]["path"]
        path.write_bytes(path.read_bytes().replace(b'"ACCEPT"', b'"ACCEPT" '))
        text, doc = self.report()
        self.assertEqual(doc["tasks"][0]["acceptance"], report.UNBACKED)
        self.assertIn("acceptance recorded, not backed by the kept evidence", text)
        self.assertIn("but the kept evidence does not back it: the review receipt "
                      + state["last_review"]["receipt"]["path"] + " is missing, changed, could not be read or was too large to repeat.", text)


class OpenFindingsTests(ReviewFixture):
    """Findings of a review that ended in ACCEPT: nobody is asked to act on them, so the
    report lists them."""
    HEAD = "## Findings left open by reviews that ended in ACCEPT"

    def report(self):
        self.driver.finish("the fixture ends the shift")
        return (self.shift / "report.md").read_text(), decode((self.shift / "report.json").read_bytes())

    def accept(self, *findings):
        review = self.state()["review"]
        record = self.record(review["target"], "ACCEPT", findings,
                             [dict(id=item["id"], resolved=True, reason="Checked.") for item in review["prior"]])
        return self.end_review(self.said(record, review["running"]["marker"]))

    def test_minor_findings_of_an_accepted_commit_are_counted_in_the_outcome_and_listed(self):
        self.reach_review()
        late = dict(self.finding("F2", "minor"), location="tests/test_x.py:9", claim="the test <b>claims</b>\n## more than it tests")
        self.accept(self.finding("F1", "minor"), late)
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        found = doc["open_findings"]
        number = doc["tasks"][0]["reviews"][-1]["review"]
        self.assertEqual((found["count"], found["more"], found["unread"]), (2, 0, 0))
        self.assertEqual(found["items"][0], dict(
            task="task", review=number, stage="review", round=0, receipt=doc["tasks"][0]["reviews"][-1]["receipt"]["path"],
            id="F1", severity="minor", location="source.txt:1", claim="wrong word", earlier=False, task_accepted=True))
        outcome = text.split("## Outcome")[1].split("## What was asked")[0]
        self.assertIn("- Findings left open by reviews that ended in ACCEPT: 2. They are listed under that heading below.\n", outcome)
        section = text.split(self.HEAD)[1].split("\n## ")[0]
        self.assertIn("- task, review " + str(number) + " of the candidate (round 0), F1 (minor) at source.txt:1: wrong word",
                      section)
        self.assertIn("F2 (minor) at tests/test_x.py:9: the test", section)
        self.assertNotIn("<b>", section)  # a reviewer's words stay inert
        self.assertEqual(section.count("\n- "), 2)
        self.assertEqual(doc["issues"], [])

    def test_an_accept_without_findings_after_a_round_of_changes_leaves_none(self):
        self.reach_review()
        self.end_review(changes=True)  # N1, major: the repair round's, and resolved by the next review
        self.action("repair", reason="Address N1", next_owner="builder")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.accept()
        text, doc = self.report()
        self.assertEqual(doc["open_findings"], dict(count=0, items=[], more=0, unread=0, unaccepted=0))
        self.assertIn("- Findings left open by reviews that ended in ACCEPT: none.\n", text)
        self.assertIn(self.HEAD + "\n\nNone: no review that ended in ACCEPT left a finding open.\n", text)

    def test_a_finding_of_an_accepted_plan_is_listed_beside_those_of_the_commit(self):
        self.begin("feature")
        for stage in ("plan_review", "review"):
            while self.task_state()["stage"] != stage:
                self.assertEqual(self.consume_phase()["status"], "advanced")
            if self.state()["review"] is None:
                self.assertEqual(self.action("continue")["status"], "recorded")
            self.accept(self.finding("F1", "minor"))
        text, doc = self.report()
        self.assertEqual([(item["stage"], item["id"]) for item in doc["open_findings"]["items"]],
                         [("plan_review", "F1"), ("review", "F1")])
        section = text.split(self.HEAD)[1].split("\n## ")[0]
        self.assertIn(" of the plan (round 0), F1 (minor)", section)
        self.assertIn(" of the candidate (round 0), F1 (minor)", section)

    def test_a_finding_of_an_accepted_plan_of_a_task_never_accepted_is_not_said_to_be_on_accepted_work(self):
        # A shift stopped after the plan review: 0 accepted of 1 briefed, and the same Outcome
        # counted the minors of that review as left open "on accepted work".
        self.begin("feature")
        while self.task_state()["stage"] != "plan_review":
            self.assertEqual(self.consume_phase()["status"], "advanced")
        if self.state()["review"] is None:
            self.assertEqual(self.action("continue")["status"], "recorded")
        self.accept(self.finding("F1", "minor"), self.finding("F2", "minor"))
        text, doc = self.report()
        found = doc["open_findings"]
        self.assertEqual((found["count"], found["unaccepted"]), (2, 2))
        self.assertEqual([item["task_accepted"] for item in found["items"]], [False, False])
        outcome = text.split("## Outcome")[1].split("## What was asked")[0]
        self.assertIn("- Tasks: 0 accepted locally of 1 briefed", outcome)
        self.assertNotIn("on accepted work", text)
        self.assertIn("- Findings left open by reviews that ended in ACCEPT: 2. They are listed under that heading "
                      "below. Of these, 2 are on a task that was not accepted locally.\n", outcome)
        section = text.split(self.HEAD)[1].split("\n## ")[0]
        self.assertIn("An ACCEPT here is the verdict of one review, of a plan or of a candidate, and not the acceptance "
                      "of a task", section)
        self.assertIn("- task (task not accepted), review 1 of the plan (round 0), F1 (minor)", section)

    def test_a_report_kept_from_before_tasks_were_told_apart_is_not_said_to_mark_them(self):
        from sortie_kit import report
        item = dict(task="task", review=1, stage="plan_review", round=0, receipt="receipts/r.json", id="F1",
                    severity="minor", location="a.py:1", claim="c", earlier=False)
        line, section = report.open_words(dict(count=1, items=[item], more=0, unread=0))
        text = "\n".join(section)
        self.assertEqual(line, "- Findings left open by reviews that ended in ACCEPT: 1. They are listed under that heading below.")
        self.assertIn("not the acceptance of a task", text)
        self.assertIn("This report does not say which of the findings below are on a task that was not accepted", text)
        self.assertNotIn("is marked", text)
        self.assertIn("- task, review 1 of the plan", text)
        line, section = report.open_words(dict(count=1, items=[dict(item, task_accepted=False)], more=0, unread=0, unaccepted=1))
        self.assertIn("and a finding on a task that was not is marked", "\n".join(section))

    def test_findings_on_an_accepted_task_are_not_marked_and_the_outcome_adds_nothing(self):
        self.reach_review()
        self.accept(self.finding("F1", "minor"))
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual((doc["open_findings"]["count"], doc["open_findings"]["unaccepted"]), (1, 0))
        self.assertIn("- Findings left open by reviews that ended in ACCEPT: 1. They are listed under that heading below.\n", text)
        self.assertNotIn("not accepted locally", text.split("## Outcome")[1].split("## What was asked")[0])
        self.assertNotIn("(task not accepted)", text)

    def test_an_accepted_review_whose_receipt_cannot_be_read_is_counted_as_not_known(self):
        self.reach_review()
        self.accept(self.finding("F1", "minor"))
        _, doc = self.report()
        path = self.shift / doc["tasks"][0]["reviews"][-1]["receipt"]["path"]
        path.chmod(0o600)
        path.write_bytes(path.read_bytes().replace(b"wrong word", b"other word"))
        from sortie_kit import report
        doc = report.finalize(self.shift, 3)
        text = (self.shift / "report.md").read_text()
        self.assertEqual(doc["open_findings"], dict(count=0, items=[], more=0, unread=1, unaccepted=0))
        self.assertIn("- Findings left open by reviews that ended in ACCEPT: not known for 1 accepted review, whose receipt could not "
                      "be read; none in the others.", text)

    def test_an_earlier_minor_finding_that_the_accepting_review_left_unresolved_is_listed(self):
        self.reach_review()
        review = self.state()["review"]
        late = dict(self.finding("F2", "minor"), location="tests/test_x.py:9", claim="tests less than it says")
        self.end_review(self.said(self.record(review["target"], "CHANGES", [self.finding("F1", "major"), late], []),
                                  review["running"]["marker"]))
        self.action("repair", reason="Address F1", next_owner="builder")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        review = self.state()["review"]
        self.assertEqual([item["id"] for item in review["prior"]], ["F1", "F2"])
        answers = [dict(id="F1", resolved=True, reason="Fixed."), dict(id="F2", resolved=False, reason="Still so.")]
        self.end_review(self.said(self.record(review["target"], "ACCEPT", [], answers), review["running"]["marker"]))
        self.assertEqual(self.state()["last_review"]["outcome"], "ACCEPT")
        text, doc = self.report()
        found, last = doc["open_findings"], doc["tasks"][0]["reviews"][-1]
        self.assertEqual((found["count"], found["more"]), (1, 0))
        self.assertEqual(found["items"], [dict(
            task="task", review=last["review"], stage="review", round=1, receipt=last["receipt"]["path"],
            id="F2", severity="minor", location="tests/test_x.py:9", claim="tests less than it says", earlier=True,
            task_accepted=True)])
        self.assertIn("- Findings left open by reviews that ended in ACCEPT: 1. ", text)
        section = text.split(self.HEAD)[1].split("\n## ")[0]
        self.assertIn("F2 (minor, raised by an earlier review and not resolved in this one) at tests/test_x.py:9: "
                      "tests less than it says", section)

    def test_when_the_drivers_record_cannot_be_read_the_number_is_not_known_rather_than_none(self):
        self.reach_review()
        self.accept(self.finding("F1", "minor"))
        self.report()
        path = self.shift / "driver-state.json"
        path.chmod(0o600)
        path.write_bytes(b"{not json")
        from sortie_kit import report
        doc = report.finalize(self.shift, 3)
        text = (self.shift / "report.md").read_text()
        self.assertEqual(doc["open_findings"], dict(count=None, items=[], more=0, unread=0, unaccepted=0))
        self.assertIn("- Findings left open by reviews that ended in ACCEPT: not known, because the tasks could not be read in "
                      "full or were left out of this report.\n", text)
        section = text.split(self.HEAD)[1].split("\n## ")[0]
        self.assertIn("Not known", section)
        self.assertNotIn("None:", section)

    def test_the_short_report_at_the_output_limit_does_not_keep_a_list_of_tasks_it_dropped(self):
        from unittest.mock import patch
        from sortie_kit import receipts, report
        self.reach_review()
        self.accept(self.finding("F1", "minor"))
        self.report()
        size = max((self.shift / name).stat().st_size for name in ("report.json", "report.md"))
        with patch.object(receipts, "DOCUMENT_LIMIT", size - 1):  # the bound on each output, not on reading
            doc = report.finalize(self.shift, 3)
        self.assertIn("output limit", doc["issues"][0]["reason"])
        self.assertEqual(doc["open_findings"], dict(count=None, items=[], more=0, unread=0, unaccepted=0))
        self.assertIn("- Findings left open by reviews that ended in ACCEPT: not known", (self.shift / "report.md").read_text())

    def test_which_later_review_marks_one_resolved_and_which_review_lists_it(self):
        from sortie_kit import report
        def kept(stage, number, outcome, findings=(), asked=(), answered=(), status="complete"):
            last = dict(status=status, findings=[dict(id=i, severity="minor", location="a.py:1", claim="c") for i in findings],
                        findings_count=len(findings), prior=[dict(id=i, resolved=r, reason="why " + i) for i, r in answered])
            return dict(reference=dict(path="receipts/r%d.json" % number, stage=stage, round=0,
                                       status="satisfied" if outcome == "ACCEPT" else "blocked"),
                        integrity="matched", role="reviewer",
                        data=dict(outcome=outcome, prior=[dict(id=i, severity="minor") for i in asked], attempts=[last]))
        def found(*receipts):
            reviews = [dict(review=n + 1, receipt=dict(path=item["reference"]["path"])) for n, item in enumerate(receipts)]
            return report.open_findings([dict(id="task", receipts=list(receipts), reviews=reviews)])
        def left(*receipts):
            return [(item["review"], item["id"]) for item in found(*receipts)["items"]]
        first = kept("review", 1, "ACCEPT", ["F1", "F2"])
        self.assertEqual(left(first), [(1, "F1"), (1, "F2")])
        # A later ACCEPT that was asked about both resolved one; the other is listed once, under that later review.
        both = dict(asked=["F1", "F2"], answered=[("F1", True), ("F2", False)])
        self.assertEqual(left(first, kept("review", 2, "ACCEPT", **both)), [(2, "F2")])
        # Any later review of the stage that was asked can resolve it, not only the next one.
        self.assertEqual(left(first, kept("review", 2, "CHANGES", ["F9"]),
                              kept("review", 3, "ACCEPT", asked=["F1"], answered=[("F1", True)])), [(1, "F2")])
        # A later review that asked for changes and left it unresolved does not list it; the accepted one still does.
        self.assertEqual(left(first, kept("review", 2, "CHANGES", ["F9"], **both)), [(1, "F2")])
        # An answer nobody asked for, a review of another stage, and one whose last attempt did not complete, resolve nothing.
        self.assertEqual(left(first, kept("review", 2, "ACCEPT", answered=[("F1", True)])), [(1, "F1"), (1, "F2")])
        self.assertEqual(left(first, kept("plan_review", 2, "ACCEPT", asked=["F1", "F2"], answered=[("F1", True), ("F2", True)])),
                         [(1, "F1"), (1, "F2")])
        self.assertEqual(left(first, kept("review", 2, "CHANGES", ["F9"], status="failed", **both)), [(1, "F1"), (1, "F2")])
        # A later review that raises the id anew speaks of another finding from then on.
        self.assertEqual(left(first, kept("review", 2, "CHANGES", ["F1"]),
                              kept("review", 3, "ACCEPT", asked=["F1"], answered=[("F1", True)])), [(1, "F1"), (1, "F2")])
        # An earlier finding the accepting review left unresolved: its place and claim are the earlier receipt's.
        raised = kept("review", 1, "CHANGES", ["F1", "F2"])
        carried = found(raised, kept("review", 2, "ACCEPT", **both))["items"]
        self.assertEqual([(i["review"], i["id"], i["severity"], i["location"], i["claim"], i["earlier"]) for i in carried],
                         [(2, "F2", "minor", "a.py:1", "c", True)])
        # Without that earlier receipt, what is left is the reviewer's reason.
        raised["integrity"] = "damaged"
        carried = found(raised, kept("review", 2, "ACCEPT", **both))["items"]
        self.assertEqual([(i["id"], i["location"], i["claim"]) for i in carried], [("F2", None, "why F2")])
        # More findings than the report keeps of one review are counted, not listed.
        many = kept("review", 1, "ACCEPT", ["F1"])
        many["data"]["attempts"][-1]["findings_count"] = 20
        seen = report.open_findings([dict(id="task", receipts=[many], reviews=[])])
        self.assertEqual((seen["count"], len(seen["items"]), seen["more"]), (20, 1, 19))
        # Shapes that are not what the host writes are passed over, never raised on.
        self.assertEqual(report.open_findings([dict(id="task", receipts=[dict(reference=None), 3, dict(
            reference=dict(stage="review", status="satisfied"), role="reviewer", integrity="matched",
            data=dict(outcome="ACCEPT", attempts="x"))], reviews="x"), "task"])["count"], 0)
        # Every answer to an earlier finding is kept, so one late in a long list is not lost.
        answers = [dict(id="F%d" % n, resolved=n != 17, reason="r") for n in range(20)]
        kept_data = report.review_kept(dict(attempts=[dict(record=dict(prior=answers, findings=[]))]))
        self.assertEqual([p["id"] for p in kept_data["attempts"][0]["prior"] if p["resolved"] is False], ["F17"])
        # A finding whose place is not known is said without one, and never as `null`.
        line, section = report.open_words(dict(count=1, more=0, unread=0, items=[dict(
            task="task", review=None, stage="review", round=0, id="F2", severity="minor", location=None, claim="why", earlier=True)]))
        self.assertNotIn("null", "\n".join(section))
        self.assertIn("F2 (minor, raised by an earlier review and not resolved in this one): why", "\n".join(section))
        # A closeout entry on the hold that is not what the host writes is never raised on and never called whole.
        for odd in (dict(refusals=3), dict(gaps="x"), dict(refusals_count=1), dict(gaps_count=2)):
            entry = dict(dict(asked=True, means="a fixture", held=True, whole=True, refusals=[], gaps=[]), **odd)
            self.assertIn("NOT for the whole shift", report.wake_words(dict(wake=entry))[0], odd)
        # What is not a number is said as not known.
        line, section = report.open_words(dict(count=None, items=[], more=0, unread=0, unaccepted=0))
        self.assertIn("not known", line)
        self.assertNotIn("None:", "\n".join(section))


class RequirementsReportTests(ReviewFixture):
    """What the brief asks for, as the review that accepted a task answered it: an ACCEPT beside a
    requirement the reviewer did not check is taken, and the report never leaves it unsaid."""
    LINE = "- Requirements accepted unchecked: "
    V1 = "sortie-kit.review.v1"

    def report(self):
        self.driver.finish("the fixture ends the shift")
        return (self.shift / "report.md").read_text(), decode((self.shift / "report.json").read_bytes())

    def accept(self, *requirements, schema=None):
        review = self.state()["review"]
        record = self.record(review["target"], "ACCEPT", [], [dict(id=item["id"], resolved=True, reason="Checked.")
                                                               for item in review["prior"]],
                             requirements=requirements or None, schema=schema)
        return self.end_review(self.said(record, review["running"]["marker"]))

    def as_an_earlier_build(self):
        """The host as it was before the list: it asked a commit review for the earlier schema."""
        from unittest.mock import patch
        from sortie_kit import review
        patched = patch.object(review, "schema_for", lambda kind: self.V1)
        patched.start()
        self.addCleanup(patched.stop)

    def outcome(self, text):
        return text.split("## Outcome")[1].split("## What was asked")[0]

    def test_requirements_accepted_unchecked_are_counted_in_the_outcome_and_listed_under_the_task(self):
        self.reach_review()
        self.accept(self.need(), self.need("old rows <b>still</b> load\n## and more", "not_checked", ""),
                    self.need("nothing is written outside the cache", "not_checked", "tree/src/cache.py"))
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        last = doc["tasks"][0]["reviews"][-1]
        self.assertEqual(doc["requirements_unchecked"], dict(count=2, more=0, unasked=0, unread=0, items=[
            dict(task="task", review=last["review"], receipt=last["receipt"]["path"],
                 asked="old rows <b>still</b> load\n## and more", where=""),
            dict(task="task", review=last["review"], receipt=last["receipt"]["path"],
                 asked="nothing is written outside the cache", where="tree/src/cache.py")]))
        self.assertIn(self.LINE + "2. They are listed under each task below.\n", self.outcome(text))
        task = text.split("### task (")[1].split("\n### ")[0].split("\n## ")[0]
        self.assertIn("- What the brief asks for, as review " + str(last["review"]) + " that ended in ACCEPT answered it:\n"
                      "  - met: the word says after (seen at source.txt:1)\n"
                      "  - **not_checked**: old rows &lt;b&gt;still&lt;/b&gt; load ## and more\n"
                      "  - **not_checked**: nothing is written outside the cache (place named: tree/src/cache.py)\n", task)
        self.assertNotIn("<b>", task)  # a reviewer's words stay inert
        kept = doc["tasks"][0]["receipts"][-1]["data"]["attempts"][-1]
        self.assertEqual((kept["requirements_asked"], [item["status"] for item in kept["requirements"]]),
                         (True, ["met", "not_checked", "not_checked"]))
        self.assertEqual(doc["issues"], [])

    UNSEEN = "no time was left for it before this review had to start"

    def begun_at(self, seconds, *, window=300, wind_down=100):
        """A build handed in `seconds` into a shift of `window` seconds whose last `wind_down` are wind-down."""
        self.driver = Driver(self.admission, self.spawn, seconds=window, wind_down=wind_down)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(seconds)
        result = self.consume_phase()
        self.assertEqual(result["status"], "advanced", result)
        self.assertIn("started by the host", result["detail"])

    def host_log(self):
        path = self.shift / "logs" / "host.log"
        return path.read_text() if path.exists() else ""

    def test_a_review_begun_four_seconds_before_wind_down_as_on_a_real_shift(self):
        """Seen on a real shift of five minutes: the candidate's review began four seconds before
        wind-down, the host showed it none of the builder's receipts, and nothing said so. Its
        REVIEW.md described evidence/, the reviewer answered five requirements not_checked and
        the candidate was accepted. The evidence was held to the time by which a review may
        still start; it is shown in the review's own time now, of which there were 94 seconds."""
        self.begun_at(196)
        self.assertEqual((self.driver.active_seconds(), self.driver.work_seconds()), (4, 94))
        where = self.directory()
        self.assertEqual(sorted(p.name for p in (where / "evidence" / "receipts").iterdir()),
                         sorted(item["path"].split("/")[1] for item in self.task_state()["receipts"]))
        self.assertTrue((where / "evidence" / "HOST.md").exists())
        told = " ".join((where / "REVIEW.md").read_text().split())
        self.assertIn("`evidence/`: the builder's receipts", told)
        state = self.accept(self.need(), self.need("the baseline ran first", "not_checked", ""))
        self.assertEqual(state["last_review"]["outcome"], "ACCEPT")
        self.assertEqual(self.review_receipt(state)["data"]["attempts"][-1]["evidence"], "shown")
        self.assertNotIn("was not shown", self.host_log())
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual(doc["issues"], [])
        self.assertNotIn("did not show", text)

    def test_a_review_with_less_of_its_own_time_than_the_start_is_allowed_is_told_that_it_was_shown_none(self):
        """What is left of the silence found on that shift: a review whose own time is shorter
        than what the host allows the start is shown none of the builder's receipts. It reads
        so in REVIEW.md, and the receipt, the host's log and the report each say it."""
        self.begun_at(289, wind_down=10)
        self.assertEqual((self.driver.active_seconds(), self.driver.work_seconds()), (1, 6))
        where = self.directory()
        self.assertFalse((where / "evidence").exists())
        told = " ".join((where / "REVIEW.md").read_text().split())
        self.assertNotIn("`evidence/`: the builder's receipts", told)
        self.assertIn("The host did not show the builder's receipts to this review, nor its own statement of what "
                      "it saw of the repository: " + self.UNSEEN, told)
        self.assertIn("Answer a requirement that rests on a receipt, a log or the state of the repository "
                      "`not_checked`.", told)
        line = ("review 1 of task task, attempt 1: the reviewer was not shown the builder's receipts nor the host's "
                "statement of the repository (" + self.UNSEEN + ")")
        self.assertEqual(self.host_log().count(line), 1)
        state = self.accept(self.need(), self.need("the baseline ran first", "not_checked", ""))
        self.assertEqual(state["last_review"]["outcome"], "ACCEPT")
        attempt = self.review_receipt(state)["data"]["attempts"][-1]
        self.assertEqual(attempt["evidence"], "no_time")
        self.assertEqual(state["tasks"]["task"]["reviews"][-1]["attempts"][-1]["evidence"], "no_time")
        self.assertEqual(self.host_log().count("was not shown"), 1)
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual(doc["issues"], [])
        self.assertEqual(doc["tasks"][0]["receipts"][-1]["data"]["attempts"][-1]["evidence"], "no_time")
        task = text.split("### task (")[1].split("\n### ")[0].split("\n## ")[0]
        self.assertIn("  - **The host did not show this review the builder's receipts**: " + self.UNSEEN
                      + ". Answers of `not_checked` in it may rest on that.\n", task)
        self.assertIn("- **The host did not show review 1 the builder's receipts**: " + self.UNSEEN + ". An answer of "
                      "`not_checked` below may rest on that and not on what the builder left out.\n"
                      "- What the brief asks for, as review 1 that ended in ACCEPT answered it:\n", task)

    def test_a_review_that_was_shown_the_receipts_says_so_in_its_receipt_and_nowhere_else(self):
        self.reach_review()
        self.assertTrue((self.directory() / "evidence" / "HOST.md").exists())
        self.assertIn("`evidence/`: the builder's receipts", " ".join((self.directory() / "REVIEW.md").read_text().split()))
        state = self.accept(self.need(), self.need("the baseline ran first", "not_checked", ""))
        self.assertEqual(self.review_receipt(state)["data"]["attempts"][-1]["evidence"], "shown")
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual(doc["tasks"][0]["receipts"][-1]["data"]["attempts"][-1]["evidence"], "shown")
        self.assertNotIn("did not show", text)
        self.assertNotIn("was not shown", self.host_log())

    def test_a_receipt_that_does_not_say_what_became_of_the_evidence_is_read_as_before(self):
        """A shift kept from before the host said it has attempts without the key. Its receipt
        keeps the shape, and the report says nothing of evidence and keeps no key for it. A
        value that is none of the host's four is no receipt of the host's."""
        from sortie_kit import report
        from sortie_kit.layout import Refused
        from sortie_kit.receipt_schema import validate_shape
        self.reach_review()
        state = self.accept(self.need())
        value = self.review_receipt(state)
        validate_shape(value, "receipt")
        attempt = value["data"]["attempts"][-1]
        self.assertEqual(attempt.pop("evidence"), "shown")
        validate_shape(value, "receipt")  # as an earlier build wrote it
        self.assertNotIn("evidence", report.review_kept(value["data"])["attempts"][-1])
        self.assertIsNone(report.unshown_words(attempt))
        for wrong in (None, "none", "", 1, ["late"]):
            attempt["evidence"] = wrong
            with self.assertRaises(Refused):
                validate_shape(value, "receipt")
            # A retained file changed by hand: the report takes no words from it.
            self.assertNotIn("evidence", report.review_kept(value["data"])["attempts"][-1])
            self.assertIsNone(report.unshown_words(attempt))
            self.assertNotIn("did not show", report.attempt_words(dict(attempt, status="failed")))
        for known in ("no_time", "late", "failed"):
            attempt["evidence"] = known
            validate_shape(value, "receipt")
            self.assertEqual(report.review_kept(value["data"])["attempts"][-1]["evidence"], known)
            self.assertIn("The host did not show this run the builder's receipts: ",
                          report.attempt_words(dict(attempt, status="failed", reasons=["nothing usable"])))

    def test_a_shift_in_the_shape_of_a_kept_one_whose_lines_on_delivery_went_unchecked(self):
        """A kept shift of the project's own: the second of two tasks, one new commit on `main`
        on top of the first task's, no remote, the builder's checkouts as linked working
        trees under the shift's scratch/. Its brief said "Do not push." and "One new local
        commit for this task, containing only this task's changes."; the reviewers answered
        the first with nothing under `where` and said of the second, on that shift's other
        task, that the statement named the two commits "but not the commit count between
        them". The statement gives each now. One commit is answered from it; that nothing
        was pushed stays `not_checked`, as no look can show it, with what the host saw."""
        import subprocess
        from sortie_kit import repo_state

        def git(*args):
            subprocess.run(["git", "-C", str(self.target), *args], check=True, capture_output=True, timeout=10)
        git("symbolic-ref", "HEAD", "refs/heads/main")
        git("reset", "-q", "--hard", self.base)  # the first task's commit: this task begins on it
        for name, commit in (("one-baseline", self.base), ("one-candidate", self.candidate)):
            git("worktree", "add", "-q", "--detach", str(self.shift / "scratch" / name), commit)
        self.driver.repo["start"] = repo_state.observe(self.target, 5)
        brief = self.artifact("briefs/task-probe.md", "- Do not push.\n- One new local commit for this task, containing "
                                                      "only this task's changes.\n")
        result = self.action("brief", task_id="probe", kind="defect", brief=brief, next_owner="builder")
        self.assertEqual(result["status"], "recorded", result)
        self.end_turn()
        self.assertEqual(self.task_state()["base"], self.base)
        git("reset", "-q", "--hard", self.candidate)  # the builder's one commit
        while self.task_state()["stage"] != "review":
            self.assertEqual(self.consume_phase()["status"], "advanced")
        where = self.directory()
        self.assertEqual((where / "brief.md").read_text().splitlines()[0], "- Do not push.")
        said = " ".join((where / "evidence" / "HOST.md").read_text().split())
        for part in ("- Branch: `HEAD` is on the branch `main`, and is at the candidate.",
                     "- Commits of the task: 1 commit from the commit the task began at (not counted) to the "
                     "candidate (counted). It is: " + self.candidate + ".",
                     "- Remotes: the repository's own configuration names no remote now, and named none when the "
                     "shift began.",
                     "- Remote-tracking refs: the repository has no ref under `refs/remotes/`, so the candidate is "
                     "on none.",
                     "- Not committed: no file is untracked and not ignored; no path is staged and not committed.",
                     "- Not that nothing was pushed."):
            self.assertIn(part, said)
        self.assertNotIn("scratch", said.split("## The repository")[1])  # the shift's own files are no leftovers
        told = " ".join((where / "REVIEW.md").read_text().split())
        self.assertIn("That nothing was pushed: answer `not_checked` and say under `where` what the host saw", told)
        seen = "evidence/HOST.md: no remote now or when the shift began, on no remote-tracking ref"
        self.accept(self.need("One new local commit for this task", "met", "evidence/HOST.md: 1 commit, on main"),
                    self.need("The commit holds only this task's changes", "met", "change.patch"),
                    self.need("Do not push", "not_checked", seen))
        self.assertEqual(self.review_receipt()["data"]["attempts"][-1]["evidence"], "shown")
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual((doc["requirements_unchecked"]["count"],
                          [(item["asked"], item["where"]) for item in doc["requirements_unchecked"]["items"]]),
                         (1, [("Do not push", seen)]))
        self.assertIn(self.LINE + "1. ", self.outcome(text))
        self.assertIn("  - met: One new local commit for this task (seen at evidence/HOST.md: 1 commit, on main)\n"
                      "  - met: The commit holds only this task's changes (seen at change.patch)\n"
                      "  - **not_checked**: Do not push (place named: " + seen + ")\n", text)
        self.assertEqual(doc["issues"], [])

    def test_a_plan_review_shown_no_evidence_is_told_so_without_the_answer_it_does_not_have(self):
        self.driver = Driver(self.admission, self.spawn, seconds=300, wind_down=10)
        self.begin("feature")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(289)
        result = self.consume_phase()  # the plan is handed in and the host starts its review
        self.assertEqual(result["status"], "advanced", result)
        told = " ".join((self.directory() / "REVIEW.md").read_text().split())
        self.assertIn("The host did not show the builder's receipts to this review: " + self.UNSEEN, told)
        self.assertNotIn("not_checked", told)
        state = self.end_review()
        self.assertEqual(self.review_receipt(state)["data"]["attempts"][-1]["evidence"], "no_time")
        text, _ = self.report()
        self.assertIn("  - **The host did not show this review the builder's receipts**: " + self.UNSEEN + ".\n", text)
        self.assertNotIn("not_checked` in it may rest", text)

    def test_an_attempt_whose_directory_never_came_back_says_nothing_of_evidence_and_one_that_did_says_it_first(self):
        """An attempt that fails before its directory is made knows nothing of evidence: the key
        is left out, since the receipt's shape takes one of four words or none. One that fails
        after it says what it knows, and its line in the host's log stands before the next attempt's."""
        from sortie_kit.layout import Refused
        self.driver = Driver(self.admission, self.spawn, seconds=300, wind_down=10)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(289)  # one second before wind-down, six of working time: less than the start is allowed
        make, calls = review_dir.make, []

        def first_fails(*args, **named):
            calls.append(1)
            if len(calls) == 1:
                raise Refused("the repository could not be exported")
            return make(*args, **named)
        with mock.patch.object(review_dir, "make", first_fails):
            result = self.consume_phase()
        self.assertEqual(result["status"], "advanced", result)
        self.assertEqual(self.host_log().count("was not shown"), 1)
        self.assertIn("attempt 2: the reviewer was not shown", self.host_log())
        state = self.accept(self.need())
        first, second = self.review_receipt(state)["data"]["attempts"]
        self.assertEqual((first["ended_by"], "evidence" in first, second["evidence"]), ("not started", False, "no_time"))
        self.assertNotIn("evidence", state["tasks"]["task"]["reviews"][-1]["attempts"][0])

    def test_a_first_attempt_that_did_not_start_after_its_directory_was_made_is_in_the_log_before_the_second(self):
        self.driver = Driver(self.admission, self.spawn, seconds=300, wind_down=10)
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.clock.sleep(289)  # one second before wind-down, six of working time: less than the start is allowed
        snapshot, calls = review_dir.snapshot, []

        def first_fails(*args, **named):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("the repository could not be read")
            return snapshot(*args, **named)
        with mock.patch.object(review_dir, "snapshot", first_fails):
            result = self.consume_phase()
        self.assertEqual(result["status"], "advanced", result)
        lines = [line.split(" ", 1)[1] for line in self.host_log().splitlines() if "was not shown" in line]
        self.assertEqual([line.split(":")[0] for line in lines],
                         ["review 1 of task task, attempt 1", "review 1 of task task, attempt 2"])
        state = self.accept(self.need())
        self.assertEqual([item["evidence"] for item in self.review_receipt(state)["data"]["attempts"]],
                         ["no_time", "no_time"])
        text, _ = self.report()
        self.assertIn("  - Attempt 1: failed (not started): not started: the repository could not be read. The host did "
                      "not show this run the builder's receipts: " + self.UNSEEN + ".\n", text)

    TOLD = "- Requirements accepted as said to be not delivered: "

    def test_a_requirement_accepted_as_disclosed_is_counted_in_the_outcome_and_listed_under_the_task(self):
        """Seen on a real shift: the accepting review said a part of the task was not delivered, as the brief
        allowed, and the report read one task accepted and nothing left open."""
        self.reach_review()
        self.accept(self.need(), self.need("the second <i>part</i>, or a statement", "disclosed", "notes.md:3"),
                    self.need("old rows still load", "not_checked", ""))
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        last = doc["tasks"][0]["reviews"][-1]
        self.assertEqual(doc["requirements_disclosed"], dict(count=1, more=0, items=[
            dict(task="task", review=last["review"], receipt=last["receipt"]["path"],
                 asked="the second <i>part</i>, or a statement", where="notes.md:3")]))
        # The unchecked ones are counted apart, in the shape they had.
        self.assertEqual((doc["requirements_unchecked"]["count"], [i["asked"] for i in doc["requirements_unchecked"]["items"]]),
                         (1, ["old rows still load"]))
        outcome = self.outcome(text)
        self.assertIn(self.TOLD + "1. The brief allowed saying so in place of the work, as the review that accepted "
                      "the task read it. They are listed under each task below.\n", outcome)
        self.assertIn(self.LINE + "1. ", outcome)
        task = text.split("### task (")[1].split("\n### ")[0].split("\n## ")[0]
        self.assertIn("  - **disclosed** (not delivered, and said so): the second &lt;i&gt;part&lt;/i&gt;, or a statement "
                      "(said at notes.md:3)\n", task)
        self.assertEqual(doc["issues"], [])

    def test_an_accept_with_every_requirement_met_leaves_none(self):
        self.reach_review()
        self.accept(self.need(), self.need("the test names the case", "met", "tests/test_x.py:9"))
        text, doc = self.report()
        self.assertEqual(doc["requirements_unchecked"], dict(count=0, items=[], more=0, unasked=0, unread=0))
        self.assertEqual(doc["requirements_disclosed"], dict(count=0, items=[], more=0))
        self.assertIn(self.LINE + "none.\n", self.outcome(text))
        self.assertIn(self.TOLD + "none.\n", self.outcome(text))
        self.assertIn("  - met: the test names the case (seen at tests/test_x.py:9)\n", text)

    def test_a_task_no_review_accepted_has_no_such_lines_and_a_request_for_changes_is_not_counted(self):
        self.reach_review()
        review = self.state()["review"]
        record = self.record(review["target"], "CHANGES", [self.finding()], [],
                             requirements=[self.need(status="not_met"), self.need("x", "not_checked", "")])
        self.end_review(self.said(record, review["running"]["marker"]))
        text, doc = self.report()
        self.assertEqual(doc["requirements_unchecked"], dict(count=0, items=[], more=0, unasked=0, unread=0))
        self.assertNotIn("What the brief asks for", text)

    def test_a_review_kept_from_before_the_list_is_said_to_have_been_asked_for_none(self):
        self.as_an_earlier_build()
        self.reach_review()
        state = self.accept(schema=self.V1)
        self.assertEqual(state["last_review"]["outcome"], "ACCEPT")
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        from sortie_kit import report
        self.assertEqual((doc["tasks"][0]["acceptance"], doc["issues"]), (report.ACCEPTED, []))
        self.assertEqual(doc["requirements_unchecked"], dict(count=0, items=[], more=0, unasked=1, unread=0))
        self.assertIn(self.LINE + "not known: no requirement list was asked of 1 accepted review, which a build "
                      "from before the list ran.\n", self.outcome(text))
        self.assertNotIn(self.LINE + "none", text)
        self.assertIn("- What the brief asks for: no requirement list was asked of review "
                      + str(doc["tasks"][0]["reviews"][-1]["review"]) + ", which accepted this task. It was run by a "
                      "build from before the list, and the receipt is as that build wrote it.\n", text)
        kept = doc["tasks"][0]["receipts"][-1]["data"]["attempts"][-1]
        self.assertEqual((kept["requirements_asked"], kept["requirements"]), (False, None))

    def test_an_accepted_review_whose_receipt_cannot_be_read_is_counted_as_not_known(self):
        self.reach_review()
        self.accept(self.need("x", "not_checked", ""))
        _, doc = self.report()
        path = self.shift / doc["tasks"][0]["reviews"][-1]["receipt"]["path"]
        path.chmod(0o600)
        path.write_bytes(path.read_bytes().replace(b"not_checked", b"not_looked "))
        from sortie_kit import report
        doc = report.finalize(self.shift, 3)
        text = (self.shift / "report.md").read_text()
        self.assertEqual(doc["requirements_unchecked"], dict(count=0, items=[], more=0, unasked=0, unread=1))
        self.assertIn(self.LINE + "not known for 1 accepted review, whose receipt could not be read or relied on; "
                      "none in the others.\n", self.outcome(text))
        # Nor is none said of the disclosed ones where a list could not be read.
        self.assertIn(self.TOLD + "none in the answers that could be read; not known for the accepted reviews named in "
                      "the line above.\n", self.outcome(text))
        self.assertIn("- What the brief asks for: not known. The receipt of the review that accepted this task could "
                      'not be read; see "Problems with the evidence".\n', text)

    def test_when_the_tasks_cannot_be_read_or_are_dropped_the_number_is_not_known_rather_than_none(self):
        from unittest.mock import patch
        from sortie_kit import receipts, report
        self.reach_review()
        self.accept(self.need("x", "not_checked", ""))
        self.report()
        unknown = dict(count=None, items=[], more=0, unasked=0, unread=0)
        size = max((self.shift / name).stat().st_size for name in ("report.json", "report.md"))
        with patch.object(receipts, "DOCUMENT_LIMIT", size - 1):  # the bound on each output, not on reading
            doc = report.finalize(self.shift, 3)
        self.assertIn("output limit", doc["issues"][0]["reason"])
        self.assertEqual(doc["requirements_unchecked"], unknown)
        self.assertEqual(doc["requirements_disclosed"], dict(count=None, items=[], more=0))
        self.assertIn(self.LINE + "not known", (self.shift / "report.md").read_text())
        path = self.shift / "driver-state.json"
        path.chmod(0o600)
        path.write_bytes(b"{not json")
        doc = report.finalize(self.shift, 3)
        self.assertEqual(doc["requirements_unchecked"], unknown)
        self.assertEqual(doc["requirements_disclosed"], dict(count=None, items=[], more=0))
        self.assertIn(self.LINE + "not known, because the tasks could not be read in full or were left out of this "
                      "report.\n", (self.shift / "report.md").read_text())
        self.assertIn(self.TOLD + "not known, for the same reason.\n", (self.shift / "report.md").read_text())

    def test_the_count_and_the_words_take_whatever_a_kept_report_holds(self):
        from sortie_kit import report
        def row(outcome="ACCEPT", requirements=(), asked=True, stage="review", **changed):
            last = dict(status="complete", verdict=outcome, requirements_asked=asked,
                        requirements=None if requirements is None else [dict(asked=a, status=s, where="") for a, s in requirements])
            return dict(dict(reference=dict(stage=stage, status="satisfied", path="receipts/r.json", round=0), role="reviewer",
                             integrity="matched", data=dict(outcome=outcome, attempts=[last])), **changed)
        def held(*rows, named=-1, matched=True, **changed):
            """A task summary whose recorded acceptance names the receipt of one of the rows, the last by default."""
            return dict(dict(id="task", receipts=list(rows), accepted_receipt=rows[named]["reference"] if rows else None,
                             review_matched=matched, reviews=[dict(review=4, receipt=dict(path="receipts/r.json"))]), **changed)
        count = lambda *rows, **kw: report.requirements_unchecked([held(*rows, **kw)])
        unknown = dict(count=0, items=[], more=0, unasked=0, unread=1)
        # The review a task was accepted on is the one its recorded acceptance names, and no other. A later
        # ACCEPT among the receipts that the acceptance does not name gives no list.
        first = row(requirements=[("a", "not_checked")], reference=dict(stage="review", status="satisfied", path="receipts/a.json", round=0))
        other = row(requirements=[("b", "not_checked"), ("c", "not_checked")])
        self.assertEqual([i["asked"] for i in count(first, other, named=0)["items"]], ["a"])
        # An acceptance whose receipt is not among the task's receipts is not known, and never none.
        gone = held(first, accepted_receipt=dict(stage="review", status="satisfied", path="receipts/gone.json", round=0))
        self.assertEqual(report.requirements_unchecked([gone]), unknown)
        self.assertIn("not known", report.unchecked_words(report.requirements_unchecked([gone])))
        self.assertIn("not known", report.requirement_words(gone)[0])
        for odd in (None, 3, "receipts/r.json", {}):
            self.assertEqual(report.requirements_unchecked([held(first, accepted_receipt=odd)]), unknown, odd)
        # Nor is one the report did not find to be the host's consumed ACCEPT of the candidate.
        unmatched = "not known. The review receipt the recorded acceptance names is not, as kept, the host's consumed ACCEPT"
        for odd in (held(first, matched=False), held(first, matched=None), held(row("CHANGES", [("a", "not_checked")])),
                    held(dict(first, role="builder")), held(row(stage="plan_review", asked=False, requirements=None))):
            self.assertEqual(report.requirements_unchecked([odd]), unknown, odd)
            self.assertIn(unmatched, report.requirement_words(odd)[0], odd)
        # The Outcome line and the line under the task do not say different things of the same receipt.
        self.assertIn("whose receipt could not be read or relied on", report.unchecked_words(unknown))
        self.assertIn("could not be read;", report.requirement_words(gone)[0])
        self.assertIn("does not hold a requirement list", report.requirement_words(held(row(requirements=None)))[0])
        # A task with no recorded acceptance has no accepting review, whatever its receipts say.
        free = held(first)
        del free["accepted_receipt"]
        self.assertEqual(report.requirements_unchecked([free])["count"], 0)
        self.assertEqual(report.requirement_words(free), [])
        seen = count(row(requirements=[("a", "met"), ("b", "not_checked"), ("c", "not_met")]))
        self.assertEqual((seen["count"], [(i["asked"], i["review"]) for i in seen["items"]]), (1, [("b", 4)]))
        # Only the review that accepted counts: one that asked for changes before it does not.
        seen = count(row("CHANGES", [("a", "not_checked")]), row(requirements=[("a", "met")]))
        self.assertEqual(seen["count"], 0)
        # More than the report lists are counted.
        many = count(row(requirements=[("r%d" % n, "not_checked") for n in range(64)]))
        self.assertEqual((many["count"], len(many["items"]), many["more"]), (64, 64, 0))
        tasks = [held(row(requirements=[("r%d" % m, "not_checked") for m in range(64)]), id="t%d" % n) for n in range(5)]
        many = report.requirements_unchecked(tasks)
        self.assertEqual((many["count"], len(many["items"]), many["more"]), (320, 256, 64))
        self.assertIn("320.", report.unchecked_words(many))
        # Odd shapes are never raised on, and a list said to be asked for and not kept is not known, not none.
        self.assertEqual(report.requirements_unchecked([3, dict(id="t"), dict(id="t", receipts=[dict(reference=None), 3])]),
                         dict(count=0, items=[], more=0, unasked=0, unread=0))
        self.assertEqual(report.requirements_unchecked([dict(id="t", accepted_receipt=None, receipts=[dict(reference=None), 3])]),
                         unknown)
        seen = count(row(requirements=None))
        self.assertEqual((seen["count"], seen["unread"]), (0, 1))
        # No list was asked only where the kept attempt says so: an attempt that does not say, or no attempt
        # at all, is not known and is never called a review from before the list.
        for odd in (row(asked=None), row(asked="no"), dict(row(), data=dict(outcome="ACCEPT", attempts=[])),
                    dict(row(), data=dict(outcome="ACCEPT"))):
            seen = count(odd)
            self.assertEqual((seen["count"], seen["unasked"], seen["unread"]), (0, 0, 1), odd)
            words = report.requirement_words(held(odd))
            self.assertEqual(len(words), 1)
            self.assertIn("not known", words[0])
            self.assertNotIn("before the list", words[0])
        self.assertEqual(count(row(asked=False, requirements=None))["unasked"], 1)
        kept = lambda record: report.review_kept(dict(attempts=[dict(record=record)]))["attempts"][0]
        self.assertEqual([kept(r)["requirements_asked"] for r in (
            dict(schema="sortie-kit.review.v2", requirements=[]), dict(schema="sortie-kit.review.v1"),
            dict(schema="sortie-kit.review.v3", requirements=[]), dict(schema="sortie-kit.review.v4"), dict(), None,
            dict(schema=["sortie-kit.review.v1"]))],
            [True, False, True, None, None, None, None])
        # A finding's `requirement` is kept for the third schema only, and only as a position or none.
        named = lambda schema, value: kept(dict(schema=schema, findings=[dict(id="F1", requirement=value)]))["findings"][0]
        self.assertEqual([named("sortie-kit.review.v3", value).get("requirement", "absent")
                          for value in (2, None, True, "2", 2.0, [2], 0, -1)], [2, None, None, None, None, None, None, None])
        for schema in ("sortie-kit.review.v2", "sortie-kit.review.v1", None):
            self.assertNotIn("requirement", named(schema, 2), schema)
        self.assertIsNone(report.unchecked_words(None))
        # The disclosed ones: counted from the accepting review alone, listed up to the same bound, and the
        # words take whatever a kept report holds.
        told = report.requirements_disclosed([held(row(requirements=[("a", "met"), ("b", "disclosed"), ("c", "not_checked")])),
                                              held(row("CHANGES", [("d", "disclosed")])), 3, dict(id="t")])
        self.assertEqual((told["count"], [i["asked"] for i in told["items"]], told["more"]), (1, ["b"], 0))
        many = report.requirements_disclosed(tasks[:0] + [held(row(requirements=[("r%d" % m, "disclosed") for m in range(64)]),
                                                               id="t%d" % n) for n in range(5)])
        self.assertEqual((many["count"], len(many["items"]), many["more"]), (320, 256, 64))
        self.assertIsNone(report.disclosed_words(None, None))
        self.assertIn("320.", report.disclosed_words(many, None))
        self.assertTrue(report.disclosed_words(dict(count=0), dict(count=0, unasked=0, unread=0)).endswith(": none."))
        for odd in (dict(count=None), dict(count="3"), dict(count=True), dict()):
            self.assertIn("not known", report.disclosed_words(odd, dict(count=0)), odd)
        for odd in (dict(unread=1), dict(unasked=2), dict(count=None), 3, None):
            self.assertNotIn(": none.", report.disclosed_words(dict(count=0), odd), odd)
        # A number beside accepted reviews whose list could not be read is not the whole number, and says so.
        whole, rest = dict(count=0, unasked=0, unread=0), " Not known for the accepted reviews named in the line above."
        self.assertTrue(report.disclosed_words(dict(count=2), whole).endswith("They are listed under each task below."))
        for odd in (dict(count=0, unasked=0, unread=1), dict(count=0, unasked=1, unread=0), dict(count=None), 3):
            self.assertTrue(report.disclosed_words(dict(count=2), odd).endswith("each task below." + rest), odd)
        self.assertIn("not known", report.disclosed_words(dict(count=-1), whole))
        for odd in (dict(count=None), dict(count="3"), dict(count=1, unasked="x", unread=None, items=3)):
            self.assertTrue(report.unchecked_words(odd).startswith(self.LINE), odd)
        self.assertIn("not known", report.unchecked_words(dict(count="3")))
        self.assertEqual(report.requirement_words(dict(id="t", receipts=[3], reviews=None)), [])

    def test_an_accepted_task_says_how_many_requirements_were_met_not_checked_and_disclosed(self):
        """An ACCEPT beside requirements nobody checked is honest only when the developer sees
        how much of the brief the acceptance rests on: the task gives the numbers before the list,
        and the list has every requirement that was not checked."""
        from sortie_kit import report
        def task(*needs):
            ref = dict(stage="review", status="satisfied", path="receipts/r.json", round=0)
            last = dict(status="complete", verdict="ACCEPT", requirements_asked=True,
                        requirements=[dict(asked=a, status=s, where="") for a, s in needs])
            return dict(id="task", accepted_receipt=ref, review_matched=True,
                        reviews=[dict(review=4, receipt=dict(path="receipts/r.json"))],
                        receipts=[dict(reference=ref, role="reviewer", integrity="matched",
                                       data=dict(outcome="ACCEPT", attempts=[last]))])
        words = report.requirement_words(task(("a", "met"), ("b", "not_checked"), ("c", "not_checked"),
                                              ("d", "disclosed"), ("e", "met"), ("f", "met")))
        self.assertEqual(words[0], "- Requirements of the brief when review 4 accepted: 6. Met: 3. **Not checked: 2.** "
                                   "**Said to be not delivered: 1.**")
        self.assertEqual(words[1], "- What the brief asks for, as review 4 that ended in ACCEPT answered it:")
        self.assertEqual([w for w in words if "**not_checked**" in w], ["  - **not_checked**: b", "  - **not_checked**: c"])
        # Nothing is in bold when everything was met, and a status this report does not know is counted apart.
        self.assertEqual(report.requirement_words(task(("a", "met"), ("b", "met")))[0],
                         "- Requirements of the brief when review 4 accepted: 2. Met: 2. Not checked: 0. "
                         "Said to be not delivered: 0.")
        self.assertTrue(report.requirement_words(task(("a", "met"), ("b", "odd"), ("c", 3)))[0].endswith(
            "Met: 1. Not checked: 0. Said to be not delivered: 0. **Answered otherwise: 2.**"))

    def test_a_kept_list_with_an_answer_the_report_does_not_know_is_never_counted_as_none(self):
        """A kept review whose list holds a status this report does not know was counted by
        neither number under Outcome, so both lines could say none beside "Answered otherwise"
        under the task. Such a review counts among those not known."""
        from sortie_kit import report
        def task(*needs):
            ref = dict(stage="review", status="satisfied", path="receipts/r.json", round=0)
            last = dict(status="complete", verdict="ACCEPT", requirements_asked=True,
                        requirements=[dict(asked=a, status=s, where="") for a, s in needs])
            return dict(id="task", accepted_receipt=ref, review_matched=True,
                        reviews=[dict(review=4, receipt=dict(path="receipts/r.json"))],
                        receipts=[dict(reference=ref, role="reviewer", integrity="matched",
                                       data=dict(outcome="ACCEPT", attempts=[last]))])
        for odd in ("not_met", "odd", "", 3, None, ["met"], {}):
            only = task(("a", "met"), ("b", odd))
            found = report.requirements_unchecked([only])
            self.assertEqual(found, dict(count=0, items=[], more=0, unasked=0, unread=1), odd)
            self.assertEqual(report.unchecked_words(found), "- Requirements accepted unchecked: not known for 1 accepted "
                             "review, whose receipt could not be read or relied on; none in the others.")
            self.assertEqual(report.disclosed_words(report.requirements_disclosed([only]), found),
                             "- Requirements accepted as said to be not delivered: none in the answers that could be "
                             "read; not known for the accepted reviews named in the line above.")
            # The task still shows the list and how many answers it could not place.
            self.assertTrue(report.requirement_words(only)[0].endswith("**Answered otherwise: 1.**"), odd)
        # What such a list does say is counted and listed as before, beside the review that is not known.
        mixed = task(("a", "met"), ("b", "odd"), ("c", 3), ("d", "not_checked"), ("e", "disclosed"))
        found = report.requirements_unchecked([mixed])
        self.assertEqual((found["count"], found["unread"], [i["asked"] for i in found["items"]]), (1, 1, ["d"]))
        self.assertEqual(report.unchecked_words(found), "- Requirements accepted unchecked: 1. They are listed under "
                         "each task below. Not known for 1 accepted review, whose receipt could not be read or relied on.")
        told = report.requirements_disclosed([mixed])
        self.assertEqual((told["count"], [i["asked"] for i in told["items"]]), (1, ["e"]))
        self.assertTrue(report.disclosed_words(told, found).endswith(" Not known for the accepted reviews named in the line above."))
        # One review is counted once, however many such answers it holds; a list of known answers is whole.
        self.assertEqual(report.requirements_unchecked([mixed, task(("a", "odd"))])["unread"], 2)
        whole = report.requirements_unchecked([task(("a", "met"), ("b", "not_checked"), ("c", "disclosed"))])
        self.assertEqual((whole["count"], whole["unread"]), (1, 0))


class RepeatedFindingTests(ReviewFixture):
    """A finding the reviewer answers as not resolved review after review: on a real shift one
    was carried through four reviews of a plan, each round cost a repair, and neither the
    host's answer nor the Outcome of the report said that it was the same finding again."""
    LINE = "- Findings answered as not resolved by two or more reviews running: "

    def again(self, new):
        """One more repair round whose review says CHANGES, leaves every earlier finding
        not resolved and raises one new finding."""
        self.assertEqual(self.action("repair", reason="Address the findings", next_owner="builder")["status"], "advanced")
        self.assertEqual(self.hand_in()["status"], "advanced")
        return self.end_review(changes=True, new=new, resolved=False)

    def hand_in(self):
        task = self.task_state()
        target = ({"kind": "plan", "artifact": self.artifact("briefs/plan.md", "Plan\n")} if task["stage"] == "plan"
                  else {"kind": "commit", "commit": self.candidate})
        _, path = self.phase_receipt(target=target)
        return self.action("consume", receipt=path, target=target)

    def report(self):
        self.driver.finish("the fixture ends the shift")
        return (self.shift / "report.md").read_text(), decode((self.shift / "report.json").read_bytes())

    def outcome(self, text):
        return text.split("## Outcome")[1].split("## What was asked")[0]

    def test_the_host_names_a_finding_answered_as_not_resolved_by_reviews_running(self):
        self.reach_review()
        state = self.end_review(changes=True)  # review 1 raises N1
        self.assertNotIn("reviews running", state["last_review"]["detail"])
        state = self.again("M")  # review 2: N1 not resolved once, M2 new
        task = state["tasks"]["task"]
        self.assertNotIn("reviews running", state["last_review"]["detail"] + task["reason"])
        self.assertIn("send repair", state["last_review"]["detail"])
        state = self.again("P")  # review 3: N1 for the second time running, M2 for the first
        task, last = state["tasks"]["task"], state["last_review"]
        said = "Answered as not resolved by reviews running: N1 (2 reviews)"
        self.assertIn(said, task["reason"])
        self.assertIn(said, last["detail"])
        self.assertNotIn("M2 (1", last["detail"])
        self.assertIn(task["findings"]["candidate"]["path"], last["detail"])
        self.assertIn('what driver.md says under "A finding the reviewer repeats"', last["detail"])
        self.assertNotIn("; send repair,", last["detail"])  # no longer the one thing to do
        # Words only: the state has the fields it had.
        self.assertEqual(set(last), {"task_id", "stage", "round", "outcome", "receipt", "findings", "detail"})
        self.assertEqual(set(task["reviews"][-1]), set(task["reviews"][0]))
        state = self.again("Q")  # review 4
        self.assertIn("Answered as not resolved by reviews running: N1 (3 reviews), M2 (2 reviews)",
                      state["tasks"]["task"]["reason"])
        text, doc = self.report()
        found = doc["repeated_findings"]
        self.assertEqual((found["count"], found["more"], found["unread"]), (2, 0, 0))
        self.assertEqual(found["items"], [
            dict(task="task", stage="review", id="N1", severity="major", times=3, reviews=[2, 3, 4], resolved=False,
                 task_accepted=False),
            dict(task="task", stage="review", id="M2", severity="major", times=2, reviews=[3, 4], resolved=False,
                 task_accepted=False)])
        self.assertIn(self.LINE + "2. Task task (not accepted), review of the candidate, N1 (major): 3 reviews running "
                      "(reviews 2, 3 and 4), and no later review answered it as resolved; task task (not accepted), "
                      "review of the candidate, M2 (major): 2 reviews running (reviews 3 and 4), and no later review "
                      "answered it as resolved.\n", self.outcome(text))
        self.assertEqual(doc["issues"], [])

    def test_a_finding_carried_and_then_resolved_on_an_accepted_task_is_counted_and_said_as_that(self):
        self.reach_review()
        self.end_review(changes=True)
        self.again("M")
        state = self.again("P")
        self.assertEqual(self.action("repair", reason="Address the findings", next_owner="builder")["status"], "advanced")
        self.assertEqual(self.hand_in()["status"], "advanced")
        state = self.end_review()  # ACCEPT, every earlier finding resolved
        self.assertEqual(state["last_review"]["outcome"], "ACCEPT")
        self.assertNotIn("reviews running", state["last_review"]["detail"])
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual(doc["repeated_findings"]["items"], [dict(
            task="task", stage="review", id="N1", severity="major", times=2, reviews=[2, 3], resolved=True,
            task_accepted=True)])
        self.assertIn(self.LINE + "1. Task task (accepted), review of the candidate, N1 (major): 2 reviews running "
                      "(reviews 2 and 3), and a later review answered it as resolved.\n", self.outcome(text))

    def test_one_review_that_leaves_a_finding_unresolved_is_not_a_repeat_and_the_line_says_none(self):
        self.reach_review()
        self.end_review(changes=True)
        self.again("M")
        text, doc = self.report()
        self.assertEqual(doc["repeated_findings"], dict(count=0, items=[], more=0, unread=0))
        self.assertIn(self.LINE + "none.\n", self.outcome(text))

    def test_the_plan_stage_and_the_candidate_stage_are_counted_apart(self):
        self.begin("feature")
        while self.task_state()["stage"] != "plan_review":
            self.assertEqual(self.consume_phase()["status"], "advanced")
        if self.state()["review"] is None:
            self.assertEqual(self.action("continue")["status"], "recorded")
        self.end_review(changes=True)
        self.again("M")
        state = self.again("P")
        self.assertIn("Answered as not resolved by reviews running: N1 (2 reviews)", state["tasks"]["task"]["reason"])
        self.assertIn("changes to the plan", state["tasks"]["task"]["reason"])
        text, doc = self.report()
        self.assertEqual([(i["stage"], i["id"], i["times"]) for i in doc["repeated_findings"]["items"]],
                         [("plan_review", "N1", 2)])
        self.assertIn("review of the plan, N1 (major): 2 reviews running", self.outcome(text))

    def test_the_hosts_count_passes_over_a_review_that_answered_nothing_and_ends_at_a_gap(self):
        count = Driver.repeated
        row = lambda outcome, *ids, stage="review": dict(stage=stage, outcome=outcome,
                                                          findings=[dict(id=i, severity="major") for i in ids])
        self.assertEqual(count([row("CHANGES", "F1")], "review", ["F1"]), [])  # raised, then not resolved once
        self.assertEqual(count([row("CHANGES", "F1"), row("CHANGES", "F1", "F2")], "review", ["F1", "F2"]), [("F1", 2)])
        # A review that failed or was lost answered nothing: it neither adds nor ends the count.
        self.assertEqual(count([row("CHANGES", "F1"), row("FAILED"), row("CHANGES", "F1"), row("lost")], "review", ["F1"]),
                         [("F1", 2)])
        # A review without the id ends it: an id resolved and used again later is another finding.
        self.assertEqual(count([row("CHANGES", "F1"), row("CHANGES", "F1"), row("CHANGES", "F2"), row("CHANGES", "F1")],
                               "review", ["F1"]), [])
        # Reviews of the other stage are not counted, and an ACCEPT ends the count.
        self.assertEqual(count([row("CHANGES", "F1", stage="plan_review"), row("CHANGES", "F1", stage="plan_review"),
                                row("CHANGES", "F1")], "review", ["F1"]), [])
        self.assertEqual(count([row("CHANGES", "F1"), row("CHANGES", "F1"), row("ACCEPT"), row("CHANGES", "F1")],
                               "review", ["F1"]), [])
        # A kept state may hold anything where a list belongs.
        self.assertEqual(count([3, dict(stage="review", outcome="CHANGES", findings="x"), row("CHANGES", "F1")],
                               "review", ["F1"]), [])

    def test_the_count_rests_on_the_rule_that_a_new_finding_never_takes_an_id_the_review_was_asked_about(self):
        """An id in the findings of reviews running is one finding carried, because the host
        refuses an answer whose new finding uses an id it was asked about, resolved or not."""
        from sortie_kit import review
        target = {"kind": "commit", "commit": self.candidate}
        prior = [dict(id="F1", severity="major")]
        for resolved in (True, False):
            with self.subTest(resolved=resolved):
                record = self.record(target, "CHANGES", [self.finding("F1")],
                                     [dict(id="F1", resolved=resolved, reason="Checked.")])
                with self.assertRaisesRegex(Refused, "reuses the id of an earlier one"):
                    review.validate(record, target, prior)
        told = review.instructions("commit", target, "0" * 16, prior)
        self.assertIn("Give new findings new ids.", told)
        self.assertIn('"id": "F2"', told)  # the example finding avoids the id asked about
        self.assertNotIn('"id": "F1",\n      "severity"', told)

    def answer(self, verdict, findings=(), resolved=()):
        """The review that runs now ends with this verdict, these new findings, and every
        earlier finding not resolved but those named."""
        review = self.state()["review"]
        record = self.record(review["target"], verdict, findings, [
            dict(id=item["id"], resolved=item["id"] in resolved, reason="Checked.") for item in review["prior"]])
        return self.end_review(self.said(record, review["running"]["marker"]))

    def repair(self):
        self.assertEqual(self.action("repair", reason="Address the findings", next_owner="builder")["status"], "advanced")
        self.assertEqual(self.hand_in()["status"], "advanced")

    def test_a_repeated_finding_cut_from_what_is_handed_on_is_still_named_by_the_host(self):
        """The count is of the review's own answers: a finding answered again that falls
        behind the cut of what the builder is handed is named all the same, as the report
        counts it from the receipts."""
        from sortie_kit import lifecycle
        self.reach_review()
        self.answer("CHANGES", [self.finding("F1", "minor")])
        self.repair()
        self.answer("CHANGES")  # F1 not resolved, nothing new
        self.repair()
        with mock.patch.object(lifecycle, "OUTSTANDING", 1):
            state = self.answer("CHANGES", [self.finding("Z9", "major")])
        task = state["tasks"]["task"]
        self.assertEqual(task["required"]["candidate"], [dict(id="Z9", severity="major")])  # F1 is behind the cut
        self.assertIn("Answered as not resolved by reviews running: F1 (2 reviews)", task["reason"])
        text, doc = self.report()
        self.assertEqual([(i["id"], i["times"], i["reviews"]) for i in doc["repeated_findings"]["items"]], [("F1", 2, [2, 3])])

    def test_an_accept_that_leaves_a_minor_finding_unresolved_again_is_counted_and_the_host_says_nothing(self):
        self.reach_review()
        self.answer("CHANGES", [self.finding("F1", "minor"), self.finding("F2", "major")])
        self.repair()
        self.answer("CHANGES", resolved=["F2"])  # F1 not resolved for the first time
        self.repair()
        state = self.answer("ACCEPT")  # F1, a minor one, not resolved for the second time running
        last, task = state["last_review"], state["tasks"]["task"]
        self.assertEqual((last["outcome"], task["stage"]), ("ACCEPT", "accepted"))
        self.assertNotIn("reviews running", last["detail"] + task["reason"])
        self.assertEqual(self.action("close")["status"], "closed")
        text, doc = self.report()
        self.assertEqual(doc["repeated_findings"]["items"], [dict(
            task="task", stage="review", id="F1", severity="minor", times=2, reviews=[2, 3], resolved=False,
            task_accepted=True)])
        self.assertEqual([(i["id"], i["earlier"]) for i in doc["open_findings"]["items"]], [("F1", True)])

    def test_driver_md_says_what_to_do_with_a_finding_the_reviewer_repeats(self):
        from importlib.resources import files
        from sortie_kit.lifecycle import REPEATED
        text = " ".join(files("sortie_kit").joinpath("data/templates/driver.md").read_text().split())
        part = text.split("A finding the reviewer repeats.")[1].split("`finish` before wind-down")[0]
        for words in (
                REPEATED.strip(),  # the host's own words, so that a driver finds the rule from the answer
                "read the findings file and the builder's last receipt",
                "it is an ordinary repair",
                "put the proof where the reviewer sees it",
                # The showing is conditional, and a repair that points at a log again can leave the proof unseen.
                "The receipts are shown within the host's bounds of time and files: `evidence/HOST.md` in the "
                "review's directory says what was not shown, the report says when a review was shown none, and "
                "short proof can go into the plan, the builder's answers or the committed tree.",
                "`set-aside` it with the developer as `next_owner`",
                "names the finding and what the builder and the reviewer each say",
                "You never waive a finding and never call it resolved",
                "in a `note`"):
            self.assertIn(words, part)
        # And the bullet for CHANGES sends a driver there.
        changes = text.split("- `CHANGES`: the stage is `repair_needed`")[1].split("- `FAILED`")[0]
        self.assertIn("A finding the reviewer repeats", changes)

    def test_which_answers_make_a_run_in_the_report(self):
        from sortie_kit import report
        def kept(stage, number, outcome, findings=(), answered=(), status="complete", asked=None):
            last = dict(status=status, findings=[dict(id=i, severity="minor", location="a.py:1", claim="c") for i in findings],
                        findings_count=len(findings), prior=[dict(id=i, resolved=r, reason="why") for i, r in answered])
            return dict(reference=dict(path="receipts/r%d.json" % number, stage=stage, round=0,
                                       status="satisfied" if outcome == "ACCEPT" else "blocked"),
                        integrity="matched", role="reviewer",
                        data=dict(outcome=outcome, attempts=[last], prior=[
                            dict(id=i, severity="minor") for i in (asked if asked is not None else [a for a, _ in answered])]))
        def found(*receipts, numbered=True, accepted=False):
            reviews = [dict(review=n + 1, receipt=dict(path=item["reference"]["path"])) for n, item in enumerate(receipts)]
            return report.repeated_findings([dict(id="task", receipts=list(receipts), reviews=reviews if numbered else [],
                                                  acceptance=report.ACCEPTED if accepted else "acceptance missing")])
        runs = lambda *receipts, **kw: [(i["id"], i["times"], i["reviews"], i["resolved"]) for i in found(*receipts, **kw)["items"]]
        no, yes = [("F1", False)], [("F1", True)]
        first = kept("review", 1, "CHANGES", ["F1"])
        self.assertEqual(runs(first, kept("review", 2, "CHANGES", answered=no)), [])
        self.assertEqual(runs(first, kept("review", 2, "CHANGES", answered=no), kept("review", 3, "CHANGES", answered=no)),
                         [("F1", 2, [2, 3], False)])
        # A later review that answers it as resolved ends the run, which is still counted.
        self.assertEqual(runs(first, kept("review", 2, "CHANGES", answered=no), kept("review", 3, "CHANGES", answered=no),
                              kept("review", 4, "ACCEPT", answered=yes)), [("F1", 2, [2, 3], True)])
        # An ACCEPT may leave a minor one unresolved: that answer counts as any other.
        self.assertEqual(runs(first, kept("review", 2, "CHANGES", answered=no), kept("review", 3, "ACCEPT", answered=no)),
                         [("F1", 2, [2, 3], False)])
        # A review whose last attempt did not complete, or that ended in neither verdict, answered nothing.
        self.assertEqual(runs(first, kept("review", 2, "CHANGES", answered=no),
                              kept("review", 3, "FAILED", answered=no, status="failed"),
                              kept("review", 4, "FAILED", answered=no),
                              kept("review", 5, "CHANGES", answered=no)), [("F1", 2, [2, 5], False)])
        # An answer nobody asked for, and a review of the other stage, are not part of a run.
        self.assertEqual(runs(first, kept("review", 2, "CHANGES", answered=no),
                              kept("review", 3, "CHANGES", answered=no, asked=[])), [])
        self.assertEqual(runs(first, kept("review", 2, "CHANGES", answered=no),
                              kept("plan_review", 3, "CHANGES", answered=no)), [])
        # No longer asked about (cut from what was handed on): the run ends there, not resolved.
        self.assertEqual(runs(first, kept("review", 2, "CHANGES", answered=no), kept("review", 3, "CHANGES", answered=no),
                              kept("review", 4, "CHANGES", ["F9"]), kept("review", 5, "CHANGES", answered=no, asked=["F1"])),
                         [("F1", 2, [2, 3], False)])
        # A receipt that cannot be read ends the run: whether it was resolved after is not known, and a
        # run of one before it is not said to be none.
        lost = kept("review", 4, "ACCEPT", answered=yes)
        lost["integrity"] = "damaged"
        seen = found(first, kept("review", 2, "CHANGES", answered=no), kept("review", 3, "CHANGES", answered=no), lost)
        self.assertEqual(([(i["id"], i["times"], i["resolved"]) for i in seen["items"]], seen["unread"]),
                         ([("F1", 2, None)], 1))
        line = report.repeated_words(seen)
        self.assertIn("whether a later review answered it as resolved is not known", line)
        self.assertIn("Not known for 1 review whose receipt could not be read.", line)
        middle = kept("review", 2, "CHANGES", answered=no)
        middle["data"] = None
        seen = found(first, middle, kept("review", 3, "CHANGES", answered=no))
        self.assertEqual((seen["count"], seen["unread"]), (0, 1))
        self.assertIn("not known for 1 review whose receipt could not be read; none in the others.",
                      report.repeated_words(seen))
        # Without the numbers of the reviews the line names none of them, and never `None`.
        bare = found(first, kept("review", 2, "CHANGES", answered=no), kept("review", 3, "CHANGES", answered=no),
                     numbered=False, accepted=True)
        self.assertEqual((bare["items"][0]["reviews"], bare["items"][0]["task_accepted"]), ([None, None], True))
        line = report.repeated_words(bare)
        self.assertIn("Task task (accepted), review of the candidate, F1 (minor): 2 reviews running, and no later", line)
        self.assertNotIn("None", line)
        # With one number missing none is named: a part of them would read as all of them.
        part = report.repeated_words(dict(count=1, more=0, unread=0, items=[dict(
            task="task", stage="review", id="F1", severity="minor", times=3, reviews=[2, 3, None], resolved=False,
            task_accepted=False)]))
        self.assertIn("F1 (minor): 3 reviews running, and no later", part)
        self.assertNotIn("(reviews", part)
        # A review with no receipt on record gives no number to a receipt without a path.
        odd = kept("review", 2, "CHANGES", answered=no)
        again = kept("review", 3, "CHANGES", answered=no)
        del odd["reference"]["path"], again["reference"]["path"]
        seen = report.repeated_findings([dict(id="task", receipts=[first, odd, again],
                                              reviews=[dict(review=7, receipt=None)])])
        self.assertEqual(seen["items"][0]["reviews"], [None, None])
        # More runs than report.json keeps: the line does not say that report.json lists them.
        with mock.patch.object(report, "OPEN_KEPT", 1):
            cut = report.repeated_findings([dict(id="task", reviews=[], receipts=[
                kept("review", 1, "CHANGES", ["F1", "F2", "F3"]),
                kept("review", 2, "CHANGES", answered=[("F1", False), ("F2", False), ("F3", False)]),
                kept("review", 3, "CHANGES", answered=[("F1", False), ("F2", False), ("F3", False)])])])
        self.assertEqual((cut["count"], len(cut["items"]), cut["more"]), (3, 1, 2))
        line = report.repeated_words(cut)
        self.assertIn(": 3. Task task", line)
        self.assertTrue(line.endswith("; and 2 more, which report.json does not list: the review receipts have them."), line)
        many = dict(count=12, more=2, unread=0, items=[dict(task="t", stage="review", id="F%d" % n, severity="minor",
                                                            times=2, reviews=[2, 3], resolved=False, task_accepted=False)
                                                       for n in range(10)])
        self.assertTrue(report.repeated_words(many).endswith(
            "; and 4 more: report.json lists 2 of them and the review receipts have the other 2."))
        self.assertTrue(report.repeated_words(dict(count=2, more=2, unread=0, items=[])).endswith(
            ": 2, which report.json does not list: the review receipts have them."))
        # More than the line names are counted with a pointer; a reviewer's or driver's words stay inert.
        many = dict(count=11, more=0, unread=0, items=[dict(task="t<b>", stage="review", id="F%d" % n, severity="minor",
                                                            times=2, reviews=[2, 3], resolved=False, task_accepted=False)
                                                       for n in range(11)])
        line = report.repeated_words(many)
        self.assertIn("; and 3 more, which report.json lists.", line)
        self.assertNotIn("<b>", line)
        # Not known, a kept report from before the number, and a shift without a reviewer seat.
        self.assertIn("not known, because the tasks could not be read in full", report.repeated_words(report.REPEATED_UNKNOWN))
        self.assertIsNone(report.repeated_words(None))
        # Shapes that are not what the host writes are passed over, never raised on.
        self.assertEqual(report.repeated_findings([dict(id="task", receipts=[dict(reference=None), 3, dict(
            reference=dict(stage="review", status="blocked"), role="reviewer", integrity="matched",
            data=dict(outcome="CHANGES", attempts="x", prior="y"))], reviews="x"), "task"])["count"], 0)

    def test_the_number_is_not_known_when_the_tasks_were_not_read_and_absent_from_an_earlier_report(self):
        from unittest.mock import patch
        from sortie_kit import receipts, report
        self.reach_review()
        self.end_review(changes=True)
        self.again("M")
        self.again("P")
        text, doc = self.report()
        self.assertEqual(doc["repeated_findings"]["count"], 1)
        # A kept report from before the number existed says nothing of it.
        earlier = {k: v for k, v in doc.items() if k != "repeated_findings"}
        self.assertNotIn(self.LINE, report.markdown(earlier, "0" * 64).decode())
        self.assertIn(self.LINE + "1. ", report.markdown(doc, "0" * 64).decode())
        size = max((self.shift / name).stat().st_size for name in ("report.json", "report.md"))
        with patch.object(receipts, "DOCUMENT_LIMIT", size - 1):
            cut = report.finalize(self.shift, 3)
        self.assertEqual(cut["repeated_findings"], dict(count=None, items=[], more=0, unread=0))
        path = self.shift / "driver-state.json"
        path.chmod(0o600)
        path.write_bytes(b"{not json")
        doc = report.finalize(self.shift, 3)
        self.assertEqual(doc["repeated_findings"], dict(count=None, items=[], more=0, unread=0))
        self.assertIn(self.LINE + "not known, because the tasks could not be read in full or were left out of this "
                      "report.\n", (self.shift / "report.md").read_text())


for _class in (HostReviewReportTests, OpenFindingsTests, RequirementsReportTests, RepeatedFindingTests):
    for _name in tuple(vars(test_lifecycle.LifecycleFixture)):
        if _name.startswith("test_"):
            setattr(_class, _name, None)
# Left bound, the loop's variable is a second name for the last class and discovery
# loads that class twice.
del _class, _name
