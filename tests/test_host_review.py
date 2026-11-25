"""The review the host runs itself: what reaches the builder and the next review, what
counts as a failed attempt, what a failure does to the task, and wind-down."""
import os
import json
import subprocess
import unittest

import test_lifecycle
from sortie_kit.lifecycle import Driver, HELD, LOST_REVIEW
from sortie_kit.layout import Refused
from sortie_kit.receipt_schema import decode


class HostReviewTests(test_lifecycle.LifecycleFixture):
    def review_receipt(self, state=None):
        last = (state or self.state())["last_review"]
        return decode((self.shift / last["receipt"]["path"]).read_bytes())

    def directory(self):
        return self.shift / self.state()["review"]["running"]["directory"]

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
        # time: the record of the failure is written on a real clock, and with half a
        # second of room it was given 50 ms, which a loaded machine does not always meet.
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


class HostReviewReportTests(test_lifecycle.LifecycleFixture):
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


class OpenFindingsTests(test_lifecycle.LifecycleFixture):
    """Findings of a review that ended in ACCEPT: nobody is asked to act on them, so the
    report lists them."""
    HEAD = "## Findings left open on accepted work"

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
            id="F1", severity="minor", location="source.txt:1", claim="wrong word", earlier=False))
        outcome = text.split("## Outcome")[1].split("## What was asked")[0]
        self.assertIn("- Findings left open on accepted work: 2. They are listed under that heading below.\n", outcome)
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
        self.assertEqual(doc["open_findings"], dict(count=0, items=[], more=0, unread=0))
        self.assertIn("- Findings left open on accepted work: none.\n", text)
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
        self.assertEqual(doc["open_findings"], dict(count=0, items=[], more=0, unread=1))
        self.assertIn("- Findings left open on accepted work: not known for 1 accepted review, whose receipt could not "
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
            id="F2", severity="minor", location="tests/test_x.py:9", claim="tests less than it says", earlier=True)])
        self.assertIn("- Findings left open on accepted work: 1. ", text)
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
        self.assertEqual(doc["open_findings"], dict(count=None, items=[], more=0, unread=0))
        self.assertIn("- Findings left open on accepted work: not known, because the tasks could not be read in "
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
        self.assertEqual(doc["open_findings"], dict(count=None, items=[], more=0, unread=0))
        self.assertIn("- Findings left open on accepted work: not known", (self.shift / "report.md").read_text())

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
        line, section = report.open_words(dict(count=None, items=[], more=0, unread=0))
        self.assertIn("not known", line)
        self.assertNotIn("None:", "\n".join(section))


for _class in (HostReviewReportTests, OpenFindingsTests):
    for _name in tuple(vars(test_lifecycle.LifecycleFixture)):
        if _name.startswith("test_"):
            setattr(_class, _name, None)
# Left bound, the loop's variable is a second name for the last class and discovery
# loads that class twice.
del _class, _name
