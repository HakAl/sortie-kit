"""On a real shift, wind-down is not the end of the shift, a refused driver command
costs the task nothing, and what a stopped turn left in the repository is seen."""
import subprocess
import time

from sortie_kit import channels, host, receipts, repo_state
from sortie_kit.host import run_shift
from sortie_kit.receipt_schema import decode
from support import LocalTest
from test_lifecycle import LifecycleFixture


class WindDownTests(LifecycleFixture):
    """The fixture's window is 1000 s with a 10 s wind-down: new work until 990,
    and everything stopped at 995."""
    def test_work_stops_a_short_margin_before_the_deadline(self):
        from sortie_kit.bounds import closing
        self.assertEqual([closing(w) for w in (1200, 200, 10, 1)], [10, 10, 5, .5])
        self.assertEqual((self.driver.active_seconds(), self.driver.work_seconds()), (990, 995))
        self.clock.sleep(993)
        self.assertEqual((self.driver.active_seconds(), self.driver.work_seconds()), (0, 2))

    def test_a_turn_started_before_wind_down_may_run_until_work_stops(self):
        self.clock.sleep(980)
        self.action("brief", task_id="task", kind="defect", brief=self.artifact("briefs/task.md"),
                    next_owner="builder", turn_seconds=7200)
        self.assertEqual(self.state()["turn"]["limit_seconds"], 15)

    def test_wind_down_starts_nothing_new_and_still_takes_what_the_running_turn_hands_in(self):
        self.begin(running=True)
        self.clock.sleep(991)
        self.driver.poll_turn()
        self.assertIsNotNone(self.state()["turn"])  # wind-down alone ends no turn
        self.end_turn()
        self.assertEqual(self.consume_phase()["status"], "advanced")
        result = self.action("continue")
        self.assertEqual(result["status"], "blocked", result)
        for part in ("wind-down", "consume", "finish"):
            self.assertIn(part, result["detail"])
        self.assertEqual(self.state()["mode"], "active")
        result = self.action("finish", reason="out of time before the plan", next_owner="developer")
        self.assertEqual(result["status"], "closed", result)

    def test_a_brief_or_repair_in_wind_down_is_refused_and_the_shift_goes_on(self):
        self.clock.sleep(991)
        result = self.action("brief", task_id="late", kind="defect", brief=self.artifact("briefs/task.md"),
                             next_owner="builder")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("wind-down", result["detail"])
        self.assertEqual((self.state()["mode"], self.state()["tasks"], self.spawn.started), ("active", {}, []))
        self.clock.value = 0.0
        self.reach_review()
        self.assertEqual(self.consume_phase(changes=True)["status"], "blocked")
        self.clock.sleep(991)
        result = self.action("repair", reason="Fix F1", next_owner="builder")
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("wind-down", result["detail"])
        self.assertEqual((self.state()["mode"], self.task_state()["round"], self.task_state()["stage"]),
                         ("active", 0, "repair_needed"))
        self.assertEqual(self.action("finish", reason="F1 is open", next_owner="developer")["status"], "closed")

    def test_the_host_serves_through_wind_down_and_stops_work_before_the_deadline(self):
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        seen = []

        def sleep(seconds):
            self.clock.sleep(seconds)
            state = self.state()
            seen.append((self.clock.now(), state["turn"] is not None, state["mode"]))
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=1, sleep=sleep)
        self.assertEqual((code, why), (0, host.ENDING))
        self.assertTrue([row for row in seen if 91 < row[0] < 95 and row[1:] == (True, "active")], seen)
        self.assertTrue(95 <= self.clock.now() < 100, self.clock.now())
        row, = self.state()["tasks"]["task"]["turns"]
        self.assertEqual(row["outcome"], "stopped: " + host.ENDING)
        self.assertIn("before the signed deadline", (self.shift / "report.md").read_text())


    def test_a_command_is_given_no_more_than_the_working_time_left(self):
        """Lock waits and receipt validation end with the working time."""
        from unittest.mock import patch
        from sortie_kit import channels
        self.begin("defect")
        _, path = self.phase_receipt()
        self.clock.sleep(992)  # 3 s of working time left, 8 s to the deadline
        seen, lock, validate = [], channels.locked, receipts.validate_receipt

        def locked(*args, **kwargs):
            seen.append(("lock", kwargs["seconds"]))
            return lock(*args, **kwargs)

        def validated(*args, **kwargs):
            seen.append(("validate", kwargs["seconds"]))
            return validate(*args, **kwargs)
        with patch.object(channels, "locked", locked), patch.object(receipts, "validate_receipt", validated):
            result = self.action("consume", receipt=path)
        self.assertEqual(result["status"], "advanced", result)
        self.assertEqual(sorted({name for name, _ in seen}), ["lock", "validate"])
        self.assertTrue(all(0 < seconds <= 3 for _, seconds in seen), seen)

    def test_the_host_waits_on_nothing_past_the_stop_and_a_wait_that_ran_out_there_is_a_normal_ending(self):
        from unittest.mock import patch
        from sortie_kit import channels
        from sortie_kit.layout import Elapsed
        seen, real, cut = [], channels.locked, []

        def locked(shift, name, **kwargs):
            left = 95 - self.clock.now()  # the stop of a 100 s shift with a 10 s wind-down
            if left > 0:
                seen.append((kwargs["seconds"], left))
            if self.clock.now() >= 93 and not cut:
                cut.append(1)
                self.clock.sleep(kwargs["seconds"])  # held by someone else for all the time it was given
                raise Elapsed("coordination lock wait ran out")
            return real(shift, name, **kwargs)
        looks, look = [], repo_state.observe

        def observe(repo, seconds=3.0, **kwargs):
            looks.append(seconds)
            return look(repo, seconds, **kwargs)
        with patch.object(channels, "locked", locked), patch.object(repo_state, "observe", observe):
            code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=1, sleep=self.clock.sleep)
        self.assertEqual((code, why), (0, host.ENDING))
        self.assertTrue(cut and all(given <= left + 1e-6 for given, left in seen), seen)
        self.assertTrue(95 <= self.clock.now() < 100, self.clock.now())
        with patch.object(repo_state, "observe", observe):
            del looks[:]
            self.clock.value = 0.0
            run_shift(self.admission, self.spawn, seconds=2, wind_down=1, poll=1, sleep=self.clock.sleep)
        self.assertLessEqual(looks[0], 1.5)  # the look at the start, inside the working time of a 2 s shift

    def test_the_look_at_the_end_is_skipped_when_it_would_take_the_time_the_report_needs(self):
        self.assertEqual(host.end_look(host.REPORT_RESERVE + 3.0), 3.0)
        self.assertEqual(host.end_look(host.REPORT_RESERVE + 2.0), 2.0)
        self.assertIsNone(host.end_look(host.REPORT_RESERVE + 0.2))


    def served(self, **command):
        """A command the driver published, taken by the host as it serves the shift."""
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(dict(version=1, **command)))
        self.driver.poll()
        return decode((self.shift / "driver-response.json").read_bytes())

    def test_the_serving_host_takes_no_command_once_the_working_time_is_over(self):
        """`finish` included; only a direct caller keeps the finalization time."""
        from sortie_kit.layout import Elapsed
        self.assertEqual(self.driver.live_seconds(), 10)
        self.clock.sleep(994)
        apply = self.driver.apply

        def late(command, **how):
            self.clock.sleep(2)  # the stop falls between the reading of the command and its taking
            return apply(command, **how)
        self.driver.apply = late
        result = self.served(id="late", op="finish", reason="done", next_owner="developer")
        self.driver.apply = apply
        self.assertEqual(self.driver.live_seconds(), 0)
        self.assertEqual(result["status"], "invalid", result)
        self.assertIn("working time over", result["detail"])
        self.assertFalse((self.shift / "report.md").exists())
        # After the stop the serving host does not begin to read a command at all.
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(dict(
            version=1, id="later", op="finish", reason="done", next_owner="developer")))
        with self.assertRaises(Elapsed):
            self.driver.poll()
        self.assertEqual(decode((self.shift / "driver-response.json").read_bytes())["id"], "late")
        self.assertEqual(self.action("finish", reason="done", next_owner="developer")["status"], "closed")

    def test_a_turn_that_ends_near_the_stop_is_recorded_within_the_working_time_or_at_shutdown(self):
        from unittest.mock import patch
        from sortie_kit import channels
        self.begin(running=True)
        self.clock.sleep(993)
        self.spawn.child.code = 0
        seen, real = [], channels.locked

        def locked(*args, **kwargs):
            seen.append(kwargs["seconds"])
            return real(*args, **kwargs)
        with patch.object(channels, "locked", locked):
            self.driver.poll_turn()
        self.assertTrue(seen and all(0 < seconds <= 2 for seconds in seen), seen)
        self.assertIsNone(self.state()["turn"])

    def test_at_shutdown_the_driver_is_stopped_before_the_host_waits_to_record_the_turn(self):
        from unittest.mock import patch
        from sortie_kit import lifecycle
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        order, record, pump = [], lifecycle.Driver.record_turn, lifecycle.Driver.pump

        def pumped(driver):
            order.append("output drained")  # A write to the log is a wait for the disk
            return pump(driver)

        class Seat:
            def ended(self):
                return False

            def close(self):
                order.append("driver stopped")
                return "stopped by the host"

        def recorded(driver):
            order.append("turn recorded")
            return record(driver)
        with patch.object(lifecycle.Driver, "record_turn", recorded), patch.object(lifecycle.Driver, "pump", pumped):
            code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=1,
                                  sleep=self.clock.sleep, driver_seat=Seat())
        self.assertEqual((code, why), (0, host.ENDING))
        self.assertEqual(order[-3:], ["driver stopped", "turn recorded", "output drained"])
        row, = self.state()["tasks"]["task"]["turns"]
        self.assertEqual((row["outcome"], row["reaped"]), ("stopped: " + host.ENDING, True))

    def test_a_turn_end_is_not_recorded_live_when_the_working_time_runs_out_at_the_lock(self):
        """No fall back to the finalization time while the driver still runs."""
        from unittest.mock import patch
        from sortie_kit import channels
        self.begin(running=True)
        self.clock.sleep(993)
        self.spawn.child.code = 0
        left, taken = iter([2, 2]), []  # time is left when the end is seen, none at the lock

        def locked(*args, **kwargs):
            taken.append(kwargs["seconds"])
            raise AssertionError("no lock is waited for")
        with patch.object(self.driver, "work_seconds", lambda: next(left, 0)), patch.object(channels, "locked", locked):
            self.driver.poll_turn()
        self.assertEqual(taken, [])
        self.assertIsNotNone(self.driver.observed)
        self.assertIsNotNone(self.state()["turn"])
        self.driver.record_turn()  # the host's own stop writes it
        self.assertIsNone(self.state()["turn"])

    def test_an_answer_is_given_no_more_than_the_finalization_time_left(self):
        self.assertEqual(self.driver.answer_seconds(), 5)
        self.clock.sleep(996)
        self.assertEqual(self.driver.answer_seconds(), 0.25)
        self.clock.sleep(13.9)
        self.assertLessEqual(self.driver.answer_seconds(), 0.11)
        self.clock.sleep(1)
        self.assertEqual(self.driver.answer_seconds(), 0)

    def test_a_storage_failure_at_the_stop_is_not_passed_off_as_the_ordinary_ending(self):
        from unittest.mock import patch
        from sortie_kit import channels
        real, cut = channels.locked, []

        def locked(shift, name, **kwargs):
            if self.clock.now() >= 93 and not cut:
                cut.append(1)
                self.clock.sleep(kwargs["seconds"])
                raise OSError("input/output error")
            return real(shift, name, **kwargs)
        with patch.object(channels, "locked", locked):
            code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=1, sleep=self.clock.sleep)
        self.assertEqual(code, 3, why)
        self.assertIn("input/output error", why)


class RefusedCommandTests(LifecycleFixture):
    def test_a_file_that_is_no_receipt_of_the_shift_is_not_consumed_as_one(self):
        """A readable file under another name costs no repair round."""
        self.begin("defect")
        (self.shift / "logs").mkdir(exist_ok=True)
        (self.shift / "logs/turn.log").write_text("not a receipt\n")
        (self.shift / "receipts/.hidden.json").write_text("{}")
        before = self.task_state()
        for path in ("logs/turn.log", "driver-state.json", "receipts/.hidden.json", "receipts/sub/x.json"):
            with self.subTest(path=path):
                result = self.action("consume", receipt=path)
                self.assertEqual(result["status"], "invalid", result)
                self.assertEqual(self.task_state(), before)

    def test_a_failed_write_is_answered_as_uncertain_whichever_write_it_was(self):
        """The replacement may have happened before the failure."""
        self.begin("defect")
        real, calls = self.driver.save, []

        def save(view, state):
            calls.append(1)
            if len(calls) == 1:
                real(view, state)  # on disk, and then the write reports a failure
                raise OSError("sync failed")
            return real(view, state)
        self.driver.save = save
        result = self.action("close")
        self.driver.save = real
        self.assertEqual(result["status"], "blocked", result)
        for part in ("could not be recorded", "not certain", "developer"):
            self.assertIn(part, result["detail"])
        self.assertIsNotNone(self.state()["pending"])

    def test_a_finish_that_was_recorded_is_answered_closed_though_the_early_report_failed(self):
        self.begin("defect")
        self.driver.report = lambda view, state, **how: (_ for _ in ()).throw(OSError("no space left"))
        result = self.action("finish", reason="done", next_owner="developer")
        self.assertEqual(result["status"], "closed", result)
        self.assertEqual((self.state()["mode"], self.state()["pending"]), ("closed", None))

    def test_a_ledger_write_that_failed_after_it_landed_is_not_a_receipt_to_repair(self):
        from unittest.mock import patch
        from sortie_kit.layout import Refused
        self.begin("defect")
        _, path = self.phase_receipt()
        real = receipts.validate_receipt

        def validated(*args, **kwargs):
            real(*args, **kwargs)
            raise Refused("evidence read budget elapsed")
        with patch.object(receipts, "validate_receipt", validated):
            result = self.action("consume", receipt=path)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("effect incomplete", result["detail"])
        state = self.state()
        self.assertEqual((state["mode"], state["tasks"]["task"]["stage"]), ("blocked", "ack"))

    def replaced_while_checked(self, fails):
        from unittest.mock import patch
        from sortie_kit.layout import Refused
        self.begin("defect")
        _, path = self.phase_receipt()
        real = receipts.validate_receipt

        def validated(*args, **kwargs):
            file = self.shift / path
            file.write_bytes(file.read_bytes() + b"\n")  # another version of the same receipt
            result = real(*args, **kwargs)
            self.assertTrue(result["recorded"])
            if fails:
                raise Refused("evidence read budget elapsed")
            return result
        with patch.object(receipts, "validate_receipt", validated):
            result = self.action("consume", receipt=path)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("effect incomplete", result["detail"])
        state = self.state()
        self.assertEqual((state["mode"], state["tasks"]["task"]["stage"]), ("blocked", "ack"))

    def test_a_receipt_replaced_while_it_was_checked_is_not_a_receipt_to_repair(self):
        """The ledger slot went to bytes the host did not read."""
        self.replaced_while_checked(fails=False)

    def test_a_receipt_replaced_while_a_check_that_then_failed_is_not_a_receipt_to_repair(self):
        self.replaced_while_checked(fails=True)

    def test_a_receipt_check_the_serving_host_ran_out_of_time_for_costs_no_repair_round(self):
        """Out of working time with nothing in the ledger: not the builder's to repair.
        The same with working time left is not the builder's either: the check ran out
        of its own time, nothing changed, and the same consume is sent again."""
        from unittest.mock import patch
        from sortie_kit.layout import Elapsed
        self.begin("defect")
        _, path = self.phase_receipt()
        before = self.task_state()

        def validated(*args, **kwargs):
            self.clock.sleep(self.late)
            raise Elapsed("duration elapsed")
        self.clock.sleep(990)
        self.late = 10
        task = dict(task_id=before["id"], request_id=before["request_id"])
        with patch.object(receipts, "validate_receipt", validated):
            result = self.driver.apply(dict(version=1, id="served", op="consume", receipt=path, **task), serving=True)
        self.assertEqual(result["status"], "invalid", result)
        self.assertIn("working time over", result["detail"])
        self.assertEqual(self.task_state(), before)
        self.clock.value, self.late = 0.0, 0
        with patch.object(receipts, "validate_receipt", validated):
            result = self.action("consume", receipt=path)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("ran out of time checking the receipt", result["detail"])
        self.assertEqual(self.task_state(), before)
        state = self.state()
        self.assertEqual((state["mode"], state["pending"]), ("active", None))
        # With the check answering again, the same receipt is taken under a new id.
        self.assertEqual(self.action("consume", receipt=path)["status"], "advanced")

    def test_a_target_of_the_wrong_kind_for_the_phase_changes_nothing(self):
        """A well-formed command that names the wrong thing is the
        driver's to correct, as a malformed one is."""
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        good = {"kind": "commit", "commit": self.candidate}
        _, path = self.phase_receipt(target=good)
        (self.shift / "receipts/link.json").symlink_to(self.shift / path)
        before = self.task_state()
        for bad in (dict(receipt=path),
                    dict(receipt=path, target={"kind": "plan", "artifact": self.artifact("briefs/plan.md", "Plan\n")}),
                    dict(receipt="receipts/link.json", target=good)):
            with self.subTest(bad=bad):
                result = self.action("consume", **bad)
                self.assertEqual(result["status"], "invalid", result)
                self.assertEqual(self.task_state(), before)
        self.assertFalse(list((self.shift / "briefs").glob("expected-" + before["request_id"] + "*")))
        self.assertEqual(self.action("consume", receipt=path, target=good)["status"], "advanced")

    def test_an_answer_that_could_not_be_recorded_is_not_called_a_mistake_to_correct(self):
        """The intent is still on disk, so the next command will block."""
        self.begin("defect")
        real, calls = self.driver.save, []

        def save(view, state):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("no space left")
            return real(view, state)
        self.driver.save = save
        result = self.action("close")
        self.driver.save = real
        self.assertEqual(result["status"], "blocked", result)
        for part in ("could not be recorded", "developer"):
            self.assertIn(part, result["detail"])
        self.assertNotIn("Correct the command", result["detail"])
        self.assertEqual(self.action("close")["status"], "blocked")

    def test_authority_lost_after_the_ledger_was_written_is_not_a_receipt_to_repair(self):
        from unittest.mock import patch
        self.begin("defect")
        _, path = self.phase_receipt()
        real = receipts.validate_receipt

        def validated(*args, **kwargs):
            result = real(*args, **kwargs)
            self.denial = "grant withdrawn"
            return result
        with patch.object(receipts, "validate_receipt", validated):
            result = self.action("consume", receipt=path)
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("effect incomplete", result["detail"])
        state = self.state()
        self.assertEqual((state["mode"], state["tasks"]["task"]["stage"], state["tasks"]["task"]["round"]),
                         ("blocked", "ack", 0))

    def test_a_consume_the_driver_got_wrong_changes_nothing(self):
        self.begin("defect")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        good = {"kind": "commit", "commit": self.candidate}
        _, path = self.phase_receipt(target=good)
        before = self.task_state()
        for bad in (dict(receipt=path, target={"kind": "commit"}),
                    dict(receipt=path, target={"kind": "commit", "commit": "HEAD"}),
                    dict(receipt="../" + path, target=good),
                    dict(receipt="/" + path, target=good),
                    dict(receipt="receipts/not-there.json", target=good)):
            with self.subTest(bad=bad):
                result = self.action("consume", **bad)
                self.assertEqual(result["status"], "invalid", result)
                self.assertIn("Nothing changed", result["detail"])
                self.assertEqual(self.task_state(), before)
                self.assertEqual(self.state()["mode"], "active")
        self.assertEqual(self.action("consume", receipt=path, target=good)["status"], "advanced")
        self.assertEqual(self.task_state()["round"], 0)

    def test_the_driver_instructions_show_each_command_once_in_a_form_the_host_takes(self):
        """On a real shift, three of eighteen driver commands were refused for their
        shape. driver.md carries one exact example of each; these are those."""
        import json
        import re
        from importlib.resources import files
        from sortie_kit import channels, lifecycle
        from sortie_kit.receipt_schema import validate_shape
        text = files("sortie_kit").joinpath("data", "templates", "driver.md").read_text()
        examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.S)]
        self.assertEqual(sorted(e["op"] for e in examples),
                         sorted(list(lifecycle.COMMANDS) + ["consume", "consume"]))
        self.assertEqual(len({e["id"] for e in examples}), len(examples))
        self.begin("feature")
        targets = []
        for example in examples:
            with self.subTest(op=example["op"]):
                required, optional = lifecycle.COMMANDS[example["op"]]
                channels.fields(example, {"version", "id", "op"} | required, optional)
                self.assertEqual((example["version"], channels.token(example["id"])), (1, example["id"]))
                if "turn_seconds" in example:
                    self.driver.turn_limit(example)
                if "receipt" in example:
                    receipts.relative(example["receipt"])
                for key in ("brief", "evidence"):
                    if key in example:
                        receipts.relative(example[key]["path"])
                        self.assertRegex(example[key]["sha256"], "^[0-9a-f]{64}$")
                if "target" in example:
                    targets.append(example["target"]["kind"])
                    task = dict(self.task_state(), stage="plan" if example["target"]["kind"] == "plan" else "build")
                    validate_shape(self.driver.expected(task, example["target"]), "expected")
        self.assertEqual(sorted(targets), ["commit", "plan"])

    def test_a_command_refused_for_what_it_says_leaves_the_task_and_the_shift_as_they_were(self):
        self.begin("defect")
        before = self.task_state()
        brief = self.artifact("briefs/task.md")
        for name, call in (
                ("a request id from an earlier phase", lambda: self.action("consume", request_id="0" * 32, receipt="receipts/x.json")),
                ("a repair nothing asked for", lambda: self.action("repair", reason="x", next_owner="builder")),
                ("a close before acceptance", lambda: self.action("close")),
                ("a second task", lambda: self.action("brief", task_id="two", kind="defect", brief=brief, next_owner="builder")),
                ("progress without its file", lambda: self.action("progress", evidence=dict(path="logs/none.txt", sha256="0" * 64))),
                ("a finish without a reason", lambda: self.action("finish", reason="", next_owner="developer"))):
            with self.subTest(name=name):
                result = call()
                self.assertEqual(result["status"], "invalid", result)
                self.assertIn("Nothing changed", result["detail"])
                state = self.state()
                self.assertEqual((state["mode"], state["pending"], self.task_state()), ("active", None, before))
        self.assertEqual(len(self.spawn.started), 1)
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.assertEqual(self.task_state()["round"], 0)

    def test_a_first_brief_that_is_wrong_can_be_sent_again_corrected(self):
        brief = self.artifact("briefs/task.md")
        for bad in (dict(kind="chore", brief=brief), dict(kind="defect", brief=dict(brief, sha256="0" * 64))):
            result = self.action("brief", task_id="task", next_owner="builder", **bad)
            self.assertEqual(result["status"], "invalid", result)
            self.assertEqual((self.state()["mode"], self.state()["tasks"]), ("active", {}))
        self.assertEqual(self.spawn.started, [])
        self.begin("defect")

    def test_a_receipt_the_builder_got_wrong_still_needs_a_repair(self):
        self.begin("defect")
        (self.shift / "receipts/broken.json").write_text("{")
        result = self.action("consume", receipt="receipts/broken.json")
        self.assertEqual((result["status"], self.task_state()["stage"]), ("blocked", "repair_needed"))

    def test_a_reason_from_an_earlier_repair_does_not_outlive_the_next_advance(self):
        self.reach_review()
        self.assertEqual(self.consume_phase(changes=True)["status"], "blocked")
        self.assertEqual(self.action("repair", reason="Fix F1", next_owner="builder")["status"], "advanced")
        self.assertEqual(self.task_state()["reason"], "Fix F1")
        self.assertEqual(self.consume_phase()["status"], "advanced")
        self.assertEqual((self.task_state()["stage"], self.task_state()["reason"]), ("review", ""))


class HostRepoTests(LifecycleFixture):
    def test_the_closeout_and_report_name_what_a_stopped_turn_left_in_the_repository(self):
        (self.target / "there-before.txt").write_text("the developer's own\n")
        subprocess.run(["git", "-C", str(self.target), "remote", "add", "origin",
                        "https://example.invalid/team/project.git"], check=True, capture_output=True, timeout=10)
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))

        def sleep(seconds):
            self.clock.sleep(seconds)
            (self.target / "tests").mkdir(exist_ok=True)
            (self.target / "tests/test_half.py").write_text("half a change\n")
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=10, sleep=sleep)
        self.assertEqual(code, 0, why)
        kept = decode((self.shift / "closeout.json").read_bytes())["repo"]
        self.assertEqual(kept["start"]["head"], kept["end"]["head"])
        # Both looks name the remotes and keep a digest of their URLs and no URL; the report reads
        # a closeout with them as it read one without.
        self.assertEqual([(row["name"], len(row["sha256"])) for row in kept["start"]["remotes"]], [("origin", 64)])
        self.assertEqual(kept["start"]["remotes"], kept["end"]["remotes"])
        self.assertNotIn(b"example.invalid", (self.shift / "closeout.json").read_bytes())
        self.assertEqual(decode((self.shift / "report.json").read_bytes())["issues"], [])
        new = dict(path="tests/test_half.py", state="untracked")
        self.assertNotIn(new, kept["start"]["changes"])
        self.assertIn(new, kept["end"]["changes"])
        doc = decode((self.shift / "report.json").read_bytes())
        self.assertIn(new, doc["leftovers"]["new"])
        self.assertNotIn("there-before.txt", [row["path"] for row in doc["leftovers"]["new"]])
        text = (self.shift / "report.md").read_text()
        undone = text.split("## Not done", 1)[1].split("## ", 1)[0]
        self.assertIn("  - tests/test_half.py (untracked)", undone)
        self.assertNotIn("there-before", undone)
        # HEAD did not move, so no commit holds the file that was there before.
        self.assertEqual(kept["held"], dict(paths=[], complete=True))
        self.assertRegex(text, "No commit between the HEAD at the start and the HEAD at the end holds (it|any of them)[.]")
        self.assertIn("HEAD", text.split("## Outcome", 1)[1].split("## ", 1)[0])


    def test_the_report_names_linked_worktrees_a_turn_left_under_the_shift(self):
        command = dict(version=1, id="first", op="brief", task_id="task", kind="defect",
                       brief=self.artifact("briefs/task.md"), next_owner="builder")
        (self.shift / "driver-command.json").write_bytes(receipts.encoded(command))
        made = []

        def sleep(seconds):
            self.clock.sleep(seconds)
            if not made:  # what a builder does for a reproduction: a checkout of a commit under scratch/
                made.append(self.shift / "scratch" / "check-base")
                subprocess.run(["git", "-C", str(self.target), "worktree", "add", "-q", "--detach", str(made[0]), self.base],
                               check=True, capture_output=True, timeout=10)
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=10, sleep=sleep)
        self.assertEqual(code, 0, why)
        inside = ".sortie/shifts/" + self.shift.name + "/scratch/check-base"
        kept = decode((self.shift / "closeout.json").read_bytes())["repo"]
        self.assertNotIn("worktrees", kept["start"])
        self.assertEqual(kept["end"]["worktrees"], [inside])
        self.assertEqual(decode((self.shift / "report.json").read_bytes())["leftovers"]["worktrees"], [inside])
        undone = (self.shift / "report.md").read_text().split("## Not done", 1)[1].split("## ", 1)[0]
        self.assertIn("  - " + inside, undone)
        self.assertIn("git worktree prune", undone)
        self.assertTrue((made[0] / "source.txt").is_file())  # named, not removed: receipts point into it


class RepoStateTests(LocalTest):
    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                               "-c", "user.email=fixture@example.invalid", *args],
                              check=True, capture_output=True, timeout=10).stdout

    def test_a_linked_worktree_whose_shared_metadata_was_pointed_elsewhere_is_not_read(self):
        """The same per-worktree Git directory, another repository behind it."""
        import os
        repo, other = self.repo(), self.root / "other"
        (repo / "a.txt").write_text("a\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "first")
        linked = self.root / "linked"
        self.git(repo, "worktree", "add", "-q", str(linked))
        other.mkdir()
        self.git(other, "init", "-q")
        (other / "b.txt").write_text("b\n")
        self.git(other, "add", ".")
        self.git(other, "commit", "-qm", "unrelated")
        first = repo_state.observe(linked, 5)
        self.assertEqual(first["common_dir"], os.path.realpath(repo / ".git"))
        self.assertNotEqual(first["git_dir"], first["common_dir"])
        pin = dict(git_dir=first["git_dir"], common_dir=first["common_dir"])
        self.assertEqual(repo_state.observe(linked, 5, **pin)["head"], first["head"])
        (os.path.realpath(repo / ".git") / __import__("pathlib").Path("worktrees/linked/commondir")).write_text(
            os.path.realpath(other / ".git") + "\n")
        self.assertIn("error", repo_state.observe(linked, 5, **pin))

    def test_paths_uncommitted_at_the_start_are_looked_for_in_the_commits_made_since(self):
        """The report guessed `may hold them`; neither commit did."""
        from unittest.mock import patch
        repo = self.repo()
        (repo / "a.txt").write_text("a\n")
        self.git(repo, "add", "a.txt")
        self.git(repo, "commit", "-qm", "first")
        for name in ("loose one.txt", "kept out.pyc", "a.txt"):
            (repo / name).write_text("the developer's own\n")
        start = repo_state.observe(repo, 5)
        self.assertEqual(sorted(row["path"] for row in start["changes"]), ["direction.md", "kept out.pyc", "loose one.txt"])
        self.assertEqual(repo_state.held(repo, start, start, 5), dict(paths=[], complete=True))
        (repo / "b.txt").write_text("b\n")
        self.git(repo, "add", "b.txt")
        self.git(repo, "commit", "-qm", "the shift's own file")
        end = repo_state.observe(repo, 5, since=0)
        self.assertEqual(repo_state.held(repo, start, end, 5), dict(paths=[], complete=True))
        self.git(repo, "add", "loose one.txt")
        self.git(repo, "commit", "-qm", "takes the developer's file")
        end = repo_state.observe(repo, 5, since=0)
        self.assertEqual(repo_state.held(repo, start, end, 5), dict(paths=["loose one.txt"], complete=True))
        # A start look that was cut left paths out: what was found stands, the rest is not known.
        self.assertEqual(repo_state.held(repo, dict(start, truncated=True), end, 5),
                         dict(paths=["loose one.txt"], complete=False))
        # Nothing was uncommitted at the start: nothing to look for.
        self.assertIsNone(repo_state.held(repo, dict(start, changes=[]), end, 5))
        self.assertIsNone(repo_state.held(repo, dict(error="x"), end, 5))
        # No commit at the end, or a head that is no object ID: no command is run.
        with patch("subprocess.Popen", side_effect=AssertionError("no command")):
            for other in (dict(end, head=None), dict(end, head="--output=/x"), dict(error="x")):
                found = repo_state.held(repo, start, other, 5)
                self.assertTrue(found is None or "error" in found, found)
            long = dict(start, changes=[dict(path="p" * 400, state="untracked")])
            self.assertEqual(repo_state.held(repo, long, end, 5), dict(paths=[], complete=False))
        self.assertIn("error", repo_state.held(repo, dict(start, git_dir=str(repo / "nowhere")), end, 5))

    def test_a_start_with_no_commit_is_looked_at_from_the_whole_history(self):
        repo = self.repo()
        (repo / "loose.txt").write_text("x\n")
        start = repo_state.observe(repo, 5)
        self.assertIsNone(start["head"])
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "first")
        end = repo_state.observe(repo, 5)
        self.assertEqual(repo_state.held(repo, start, end, 5), dict(paths=["direction.md", "loose.txt"], complete=True))
        # With root diffs switched off in the repository, the first commit must still be read.
        self.git(repo, "config", "log.showRoot", "false")
        self.assertEqual(repo_state.held(repo, start, end, 5), dict(paths=["direction.md", "loose.txt"], complete=True))

    def test_the_history_look_reads_no_mailmap_and_keeps_path_bytes_as_they_are(self):
        """`git log` loads .mailmap and mailmap.file unless told not to; and a
        name is not trimmed to make it match."""
        from unittest.mock import patch
        repo = self.repo()
        (repo / "a.txt").write_text("a\n")
        self.git(repo, "add", "a.txt")
        self.git(repo, "commit", "-qm", "first")
        (repo / "loose.txt").write_text("x\n")
        start = repo_state.observe(repo, 5)
        self.git(repo, "add", "loose.txt")
        self.git(repo, "commit", "-qm", "second")
        end = repo_state.observe(repo, 5)
        seen, run = [], repo_state.run

        def recorded(argv, *rest):
            seen.append(argv)
            return run(argv, *rest)
        with patch.object(repo_state, "run", recorded):
            repo_state.held(repo, start, end, 5)
        argv, = seen
        for part in ("--no-mailmap", "--root"):
            self.assertIn(part, argv[argv.index("log"):argv.index("--")])
        for setting in ("log.mailmap=false", "mailmap.file=", "mailmap.blob="):
            self.assertEqual(argv[argv.index(setting) - 1], "-c")
            self.assertLess(argv.index(setting), argv.index("log"))
        # An answer that names something other than a path that was asked about is not a whole answer.
        with patch.object(repo_state, "run", lambda *a: (b"\nloose.txt\0direction.md\0", 0, False)):
            self.assertEqual(repo_state.held(repo, start, end, 5), dict(paths=["direction.md"], complete=False))

    def test_an_unborn_branch_has_no_head_and_everything_added_is_staged(self):
        repo = self.repo()
        self.git(repo, "add", "direction.md")
        (repo / "loose.txt").write_text("x\n")
        seen = repo_state.observe(repo, 5)
        self.assertIsNone(seen["head"])
        self.assertEqual(sorted((row["path"], row["state"]) for row in seen["changes"]),
                         [("direction.md", "staged"), ("loose.txt", "untracked")])

    def test_linked_worktrees_under_the_shift_folder_are_named_and_others_are_not(self):
        repo = self.repo()
        (repo / "a.txt").write_text("a\n")
        (repo / ".git/info/exclude").write_text("/.sortie/\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "first")
        self.assertNotIn("worktrees", repo_state.observe(repo, 5))
        self.git(repo, "worktree", "add", "-q", "--detach", str(repo / ".sortie/shifts/one/scratch/check b"), "HEAD")
        self.git(repo, "worktree", "add", "-q", "--detach", str(self.root / "the developer's own"), "HEAD")
        seen = repo_state.observe(repo, 5)
        self.assertEqual((seen["worktrees"], seen["changes"]), ([".sortie/shifts/one/scratch/check b"], []))

    def test_untracked_changed_and_staged_files_are_listed_and_nothing_from_the_repository_runs(self):
        repo = self.repo()
        for name in ("gone.txt", "same.txt", "touched.txt"):
            (repo / name).write_text(name + "\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "first")
        head = self.git(repo, "rev-parse", "HEAD").decode().strip()
        import os
        where = os.path.realpath(repo / ".git")
        self.assertEqual(repo_state.observe(repo, 5), dict(head=head, changes=[], truncated=False, git_dir=where,
                                                           common_dir=where, pinned=False, remotes=[]))
        self.assertTrue(repo_state.observe(repo, 5, git_dir=where, common_dir=where)["pinned"])
        # The Git directory seen at the start is the one read at the end, or nothing is.
        self.assertEqual(repo_state.observe(repo, 5, git_dir=where)["head"], head)
        self.assertIn("error", repo_state.observe(repo, 5, git_dir=str(self.root / "another/.git")))
        began = time.time()
        (repo / "staged.txt").write_text("staged\n")
        self.git(repo, "add", "staged.txt")
        (repo / "direction.md").write_text("changed by a turn that was stopped\n")
        (repo / "touched.txt").write_text("touched.txt\n")  # written again with the same bytes
        (repo / "odd name\n.txt").write_text("x\n")
        (repo / ".sortie").mkdir()
        (repo / ".sortie/ignored").write_text("x\n")
        (repo / ".git/info/exclude").write_text("/.sortie/\n")
        # Whatever the repository's own configuration names must not be run by the host.
        marker = self.root / "ran"
        trap = self.root / "trap.sh"
        trap.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\ncat\n")
        trap.chmod(0o755)
        (repo / ".gitattributes").write_text("* filter=trap\n")
        for key in ("core.fsmonitor", "filter.trap.clean", "filter.trap.smudge", "diff.external"):
            self.git(repo, "config", key, str(trap))
        hooks = repo / ".git/hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("post-index-change", "pre-commit"):
            (hooks / name).write_text(trap.read_text())
            (hooks / name).chmod(0o755)
        self.assertFalse(marker.exists(), 'the fixture itself ran the trap')
        (repo / "gone.txt").unlink()
        seen = repo_state.observe(repo, 5, since=began)
        self.assertFalse(marker.exists())
        self.assertEqual((seen["head"], seen["truncated"]), (head, False))
        self.assertEqual(sorted((row["path"], row["state"]) for row in seen["changes"]),
                         [(".gitattributes", "untracked"), ("direction.md", "changed"), ("gone.txt", "deleted"),
                          ("odd name\n.txt", "untracked"), ("staged.txt", "staged")])
        # Without a time to compare against, no tracked file is read.
        self.assertNotIn(("direction.md", "changed"),
                         [(row["path"], row["state"]) for row in repo_state.observe(repo, 5)["changes"]])

    def test_a_long_listing_is_cut_and_says_so_and_a_failure_is_an_error_not_an_exception(self):
        repo = self.repo()
        for n in range(repo_state.LIMIT + 5):
            (repo / ("f%03d" % n)).write_text("x")
        seen = repo_state.observe(repo, 5)
        self.assertEqual((len(seen["changes"]), seen["truncated"]), (repo_state.LIMIT, True))
        self.assertIn("error", repo_state.observe(self.root / "no-repository", 5))
        self.assertEqual(repo_state.new(dict(error="x"), seen), None)
        self.assertEqual(repo_state.new(dict(head=None, changes=seen["changes"][:-2], truncated=False),
                                        dict(seen, truncated=False)), seen["changes"][-2:])

    def test_a_working_tree_named_in_the_repository_configuration_is_not_listed(self):
        """The listing is of the admitted directory, wherever the
        repository's own configuration points."""
        repo = self.repo()
        (repo / "tracked.txt").write_text("x\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "first")
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "not-yours.txt").write_text("another directory's file\n")
        (repo / "left.txt").write_text("x\n")
        self.git(repo, "config", "core.worktree", str(other))
        seen = repo_state.observe(repo, 5, since=time.time() - 60)
        self.assertEqual([(row["path"], row["state"]) for row in seen["changes"]], [("left.txt", "untracked")], seen)

    def test_a_directory_swapped_for_a_link_is_not_read_through(self):
        repo = self.repo()
        (repo / "pkg").mkdir()
        (repo / "pkg/mod.py").write_text("same bytes\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "first")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "mod.py").write_text("same bytes\n")  # read through the link, this would look unchanged
        (repo / "pkg/mod.py").unlink()
        (repo / "pkg").rmdir()
        (repo / "pkg").symlink_to(outside)
        seen = repo_state.observe(repo, 5, since=time.time() - 60)
        # Git itself calls the file deleted and the link untracked; which of the two
        # words the platform's refusal to open a link gives is not the point.
        states = {row["path"]: row["state"] for row in seen["changes"]}
        self.assertIn(states.get("pkg/mod.py"), ("changed", "deleted"), seen)

    def test_git_output_is_read_no_further_than_its_bound_and_the_closeout_share_stays_small(self):
        from unittest.mock import patch
        repo = self.repo()
        for n in range(60):
            (repo / ("%03d-" % n + "\u00e9" * 80)).write_text("x")
        with patch.object(repo_state, "OUTPUT", 2000):
            out, code, cut = repo_state.run(["git", "ls-files", "-z", "--others"], str(repo), 5)
            self.assertEqual((len(out) <= 2000, code, cut), (True, None, True))
            seen = repo_state.observe(repo, 5)
        self.assertTrue(seen["truncated"])
        self.assertTrue(seen["changes"])
        for row in seen["changes"]:  # no entry that was cut in the middle
            self.assertTrue((repo / row["path"]).is_file(), row)
        seen = repo_state.observe(repo, 5)
        self.assertTrue(seen["truncated"])
        self.assertLessEqual(len(receipts.encoded(seen["changes"])), repo_state.ENCODED + 1024)
        started = time.monotonic()
        out, code, cut = repo_state.run(["sh", "-c", "sleep 5"], str(repo), 0.4)
        self.assertEqual((out, code, cut), (b"", None, True))
        # Stopping and collecting the command is inside the time given.
        self.assertLess(time.monotonic() - started, 0.4 + 0.15)

    def test_a_head_that_could_not_be_read_is_an_error_and_not_an_empty_repository(self):
        from unittest.mock import patch
        repo = self.repo()
        real = repo_state.run

        def run(argv, cwd, seconds, common=None):
            return (b"", 128, False) if "HEAD^{commit}" in argv else real(argv, cwd, seconds, common)
        with patch.object(repo_state, "run", run):
            self.assertIn("error", repo_state.observe(repo, 5))


class FakeWake:
    """Stands in for sortie_kit.wake.Wake and notes, in order, what the host asks of it."""
    def __init__(self, shift, whole=True):
        self.shift, self.whole, self.calls, self.files, self.limits = shift, whole, [], None, []

    def acquire(self):
        self.calls.append("acquire")

    def renew(self, limit=None):
        self.calls.append("renew")
        self.limits.append(limit)

    def release(self):
        self.calls.append("release")
        self.files = [(self.shift / name).exists() for name in ("closeout.json", "report.md")]

    def record(self):
        self.calls.append("record")
        at = "2030-01-01T00:00:00+00:00"
        refused = [] if self.whole else [dict(at=at, what="create", code=-5)]
        return dict(asked=True, means="a fixture", held=True, held_from=at, seen=at, whole=self.whole,
                    refusals=refused, refusals_count=len(refused), gaps=[], gaps_count=0)


class WakeHostTests(LifecycleFixture):
    def served(self, held):
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=10, sleep=self.clock.sleep,
                              **({} if held is None else {"wake": held}))
        self.assertEqual((code, why), (0, host.ENDING))
        return (decode((self.shift / "closeout.json").read_bytes()), decode((self.shift / "report.json").read_bytes()),
                (self.shift / "report.md").read_text())

    def test_the_host_renews_the_hold_each_pass_keeps_it_in_the_closeout_and_lets_go_after_the_report(self):
        held = FakeWake(self.shift)
        closeout, doc, text = self.served(held)
        self.assertNotIn("acquire", held.calls)  # taken by the launcher, right after admission
        self.assertGreater(held.calls.count("renew"), 3)
        self.assertEqual((held.calls[-1], held.calls.count("release")), ("release", 1))
        self.assertEqual(held.files, [True, True])  # the closeout and the report were written while it was held
        self.assertEqual((closeout["wake"]["means"], closeout["wake"]["whole"]), ("a fixture", True))
        self.assertEqual(doc["actual_window"]["wake"], closeout["wake"])
        self.assertIn("\n- Kept awake: for the whole shift, by a fixture.\n", text)

    def test_a_hold_that_was_refused_changes_neither_the_ending_nor_the_exit_status_and_is_shown(self):
        closeout, doc, text = self.served(FakeWake(self.shift, whole=False))
        self.assertFalse(closeout["wake"]["whole"])
        self.assertIn("- **Kept awake: NOT for the whole shift**, by a fixture.", text)
        self.assertIn("Refused 1 time: at 2030-01-01T00:00:00+00:00, create returned -5.", text)

    def test_a_shift_that_is_to_stop_is_not_kept_from_it_by_a_renewal(self):
        # Asking the system can take seconds; the stop, the seats' ends and the report do not wait on that.
        held = FakeWake(self.shift)
        code, why = run_shift(self.admission, self.spawn, seconds=100, wind_down=10, poll=10, sleep=self.clock.sleep,
                              interrupted=[15], wake=held)
        self.assertEqual((code, why), (3, "foreground host interrupted"))
        self.assertNotIn("renew", held.calls)
        self.assertEqual(held.calls[-2:], ["record", "release"])

    def test_no_renewal_comes_after_the_pass_that_saw_the_working_time_over(self):
        held, seen, record = FakeWake(self.shift), [], None
        held.renew = lambda limit=None: seen.append(self.clock.value)
        record, held.record = held.record, lambda: (seen.append(("record", self.clock.value)), record())[1]
        self.served(held)
        renewed = [at for at in seen if not isinstance(at, tuple)]
        closed = [at[1] for at in seen if isinstance(at, tuple)]
        self.assertTrue(renewed and closed)
        self.assertLess(max(renewed), closed[-1])  # the last renewal was a pass before the one that stopped

    def test_a_renewal_is_told_the_working_time_left_so_it_cannot_run_past_the_stop(self):
        held, left = FakeWake(self.shift), []
        renew = held.renew
        held.renew = lambda limit=None: (left.append((limit, self.clock.value)), renew(limit))[1]
        self.served(held)
        self.assertTrue(left)
        # The shift is 100 s long and the seats are stopped 5 s before its end (half of a
        # wind-down of 10 s): what the host hands over is the time until that stop, not the
        # time until the deadline.
        from sortie_kit import bounds
        stop = 100 - bounds.closing(10)
        self.assertEqual(stop, 95)
        for limit, at in left:
            self.assertAlmostEqual(limit, stop - at, places=6, msg=left)
            self.assertGreater(limit, 0)

    def test_the_report_a_closing_command_writes_at_once_does_not_ask_about_the_hold(self):
        # That report is written while both seats still run; the question can take half a
        # second. The host's own report, after the stop, asks and says.
        held = FakeWake(self.shift)
        self.driver.wake = held.record
        self.assertEqual(self.action("finish", reason="done", next_owner="developer")["status"], "closed")
        self.assertEqual(held.calls, [])
        self.assertNotIn("wake", decode((self.shift / "closeout.json").read_bytes()))
        self.assertTrue((self.shift / "report.md").exists())
        self.driver.finish("the fixture ends the shift")
        self.assertEqual(held.calls, ["record"])
        self.assertTrue(decode((self.shift / "closeout.json").read_bytes())["wake"]["whole"])

    def test_no_report_a_command_writes_asks_about_the_hold_whichever_way_it_blocks_the_shift(self):
        # An effect left unresolved by an earlier command blocks the shift at the next one.
        held = FakeWake(self.shift)
        self.driver.wake = held.record
        self.driver.publish_initial()
        with channels.locked(self.shift, "driver.lock", seconds=5, authorize=self.driver.guard) as view:
            state = self.driver.load(view)
            state["pending"] = dict(id="command-0", digest="0" * 64, op="brief")
            self.driver.save(view, state)
        result = self.action("finish", reason="done", next_owner="developer")
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(held.calls, [])
        self.assertNotIn("wake", decode((self.shift / "closeout.json").read_bytes()))

    def test_the_report_keeps_its_two_seconds_beside_the_question_about_the_hold(self):
        from sortie_kit import wake
        self.assertGreaterEqual(host.REPORT_RESERVE, 2.0 + wake.ASK)
        self.assertLess(wake.ASK, wake.PATIENCE)

    def test_without_a_hold_the_closeout_has_no_entry_and_the_report_no_line(self):
        closeout, doc, text = self.served(None)
        self.assertNotIn("wake", closeout)
        self.assertNotIn("Kept awake", text)
