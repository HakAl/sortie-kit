"""`sortie wait`: bounded, read-only, and never past the signed deadline."""
import contextlib
import io
import json
import unittest.mock

from sortie_kit import cli, receipts, shift_wait
from sortie_kit.layout import Refused
from sortie_kit.receipt_schema import decode
from test_lifecycle import LifecycleFixture
from support import replaced_while_read


class WaitTests(LifecycleFixture):
    def wait(self, seconds=shift_wait.CAP, during=None):
        self.sleeps = []
        def sleep(n):
            self.sleeps.append(n)
            self.clock.sleep(n)
            if during and len(self.sleeps) == 3:
                during()
        return shift_wait.wait(self.shift, seconds, clock=self.clock.now, sleep=sleep, now=self.clock.utc)

    def command(self, **data):
        raw = receipts.encoded(dict(version=1, **data))
        (self.shift / "driver-command.json").write_bytes(raw)
        return raw

    def brief(self):
        return self.command(id="first", op="brief", task_id="task", kind="defect",
                            brief=self.artifact("briefs/task.md"), next_owner="builder")

    def files(self):
        return {p.relative_to(self.shift): p.read_bytes() for p in self.shift.rglob("*") if p.is_file()}

    def test_nothing_outstanding_returns_at_once_and_writes_nothing(self):
        before = self.files()
        result = self.wait()
        self.assertEqual((result["status"], result["answered"], result["turn"], result["response"]),
                         ("ready", True, None, None))
        self.assertEqual(result["seconds_left"], 990)  # until work stops, ten seconds before the deadline
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.files(), before)

    def test_time_left_is_until_work_stops_and_wind_down_is_said(self):
        """The fixture grant runs 1000 s: no new work in its last third, all work
        stopped ten seconds before its end."""
        result = self.wait()
        self.assertEqual((result["seconds_left"], result["wind_down"], result["seconds_before_wind_down"]),
                         (990, False, 667))  # 666.67 s: never 0 while wind-down has not begun
        self.clock.sleep(700)
        result = self.wait()
        self.assertEqual((result["status"], result["seconds_left"], result["wind_down"],
                          result["seconds_before_wind_down"]), ("ready", 290, True, 0))
        self.clock.sleep(290.5)
        result = self.wait()
        self.assertEqual((result["status"], result["seconds_left"], result["wind_down"]), ("expired", 0, True))

    def test_a_wait_on_a_running_turn_returns_when_wind_down_begins(self):
        self.brief()
        self.driver.poll()
        self.clock.sleep(660)
        result = self.wait()
        self.assertEqual((result["status"], result["wind_down"]), ("waiting", True))
        self.assertTrue(666 < self.clock.now() < 668, self.clock.now())
        self.assertIsNotNone(result["turn"])
        result = self.wait(3)  # in wind-down a wait is an ordinary wait again
        self.assertEqual(result["status"], "waiting")
        self.assertGreater(self.clock.now(), 669)

    def test_blocks_until_the_host_answers_and_the_started_turn_ends(self):
        self.brief()
        result = self.wait(during=self.driver.poll)  # answered, but the turn it started still runs
        self.assertEqual((result["status"], result["answered"]), ("waiting", True))
        self.assertEqual(result["turn"]["turn"], 1)
        self.assertEqual(result["response"]["status"], "recorded")
        result = self.wait(during=lambda: self.end_turn(4))
        self.assertEqual((result["status"], result["turn"], result["stage"]), ("ready", None, "ack"))
        self.assertEqual((result["last_turn"]["outcome"], result["last_turn"]["exit"]), ("exit", 4))
        self.assertEqual(len(self.sleeps), 3)

    def test_a_turn_that_left_no_receipt_is_named_and_one_that_left_one_is_not(self):
        self.brief()
        self.driver.poll()
        self.assertNotIn("note", self.wait(2))  # a running turn has not failed to write anything
        self.end_turn(0)
        result = self.wait()
        self.assertEqual((result["status"], result["last_turn"]["receipts_changed"]), ("ready", False))
        for part in ("builder turn 1", "exit 0", "looked the same before and after", result["last_turn"]["log"],
                     "sandbox or permission", "developer"):
            self.assertIn(part, result["note"])
        self.assertNotIn("will end the same way", result["note"])  # the host saw a directory, not a cause
        self.assertEqual(self.action("continue")["status"], "recorded")
        (self.shift / "receipts/ack.json").write_text("{}")
        self.end_turn(0)
        result = self.wait()
        self.assertEqual((result["status"], result["last_turn"]["turn"]), ("ready", 2))
        self.assertNotIn("note", result)

    def test_an_answer_to_an_earlier_command_is_not_an_answer(self):
        self.brief()
        self.driver.poll()
        self.end_turn()
        task = self.task_state()
        self.command(id="second", op="continue", task_id=task["id"], request_id=task["request_id"])
        result = self.wait(5)
        self.assertEqual((result["status"], result["answered"], result["response"]), ("waiting", False, None))
        self.assertEqual(self.clock.now(), 5)

    def test_an_answer_needs_the_command_id_as_well_as_its_digest(self):
        self.driver.poll()  # nothing to answer yet; the host publishes its initial state
        raw = self.brief()
        reply = dict(version=1, id="another", digest=receipts.digest(raw), status="recorded", detail="x",
                     state=None, evidence=None)
        (self.shift / "driver-response.json").write_bytes(receipts.encoded(reply))
        self.assertEqual((self.wait(2)["status"], self.wait(2)["answered"]), ("waiting", False))
        (self.shift / "driver-response.json").write_bytes(receipts.encoded(dict(reply, id="first")))
        self.assertEqual(self.wait(2)["answered"], True)

    def test_unparseable_command_is_answered_by_the_hosts_invalid_response(self):
        (self.shift / "driver-command.json").write_text("{")
        self.assertEqual(self.wait(2)["status"], "waiting")
        self.driver.poll()
        result = self.wait(2)
        self.assertEqual((result["status"], result["response"]["status"], result["response"]["id"]),
                         ("ready", "invalid", None))

    def test_each_reading_is_bounded_by_the_time_left(self):
        seen = []
        real = receipts.session
        def session(shift, seconds, **kw):
            seen.append(seconds)
            return real(shift, seconds, **kw)
        self.brief()
        with unittest.mock.patch("sortie_kit.shift_wait.receipts.session", side_effect=session):
            self.wait(1)
        self.assertLessEqual(max(seen), 1)
        self.assertGreater(len(seen), 2)

    def test_wait_is_capped_and_never_passes_the_signed_deadline(self):
        self.brief()
        self.assertEqual(self.wait(100000)["status"], "waiting")
        self.assertAlmostEqual(self.clock.now(), shift_wait.CAP)
        self.clock.sleep(990 - shift_wait.CAP - 3)
        result = self.wait()
        self.assertEqual((result["status"], result["seconds_left"]), ("expired", 0))
        self.assertAlmostEqual(self.clock.now(), 990)  # work stops then; the signed deadline is ten seconds later

    def test_closed_or_stopped_shift_returns_at_once_even_when_unanswered(self):
        self.brief()
        (self.shift / "STOP").write_text("{}")
        self.assertEqual(self.wait()["status"], "stopped")
        (self.shift / "STOP").unlink()
        self.driver.finish("done")
        result = self.wait()
        self.assertEqual((result["status"], result["mode"]), ("closed", "closed"))
        self.assertEqual(self.sleeps, [])

    def test_damaged_state_is_never_ready_and_missing_grant_refuses(self):
        self.brief()
        self.driver.poll()
        self.end_turn()
        self.assertEqual(self.wait()["status"], "ready")
        good = (self.shift / "driver-state.json").read_bytes()
        for bad in (b"{", b"[]", b'{"mode":"active","tasks":[]}', b'{"mode":"open","tasks":{}}',
                    good.replace(b'"turn": null', b'"turn": 7')):
            with self.subTest(bad=bad[:30]):
                self.assertNotEqual(bad, good)
                (self.shift / "driver-state.json").write_bytes(bad)
                result = self.wait()
                self.assertEqual((result["status"], result["answered"], result["turn"], result["last_turn"]),
                                 ("damaged", True, None, None))
                self.assertIn("Do not treat this as ready", result["detail"])
                self.assertIn("Read again before concluding anything", result["detail"])
                self.assertEqual(self.sleeps, [])
        (self.shift / "grant.json").unlink()
        with self.assertRaisesRegex(Refused, "not a signed shift"):
            self.wait()

    def test_a_file_replaced_while_it_is_read_is_read_again_and_is_not_damage(self):
        """The host replaces driver-state.json and driver-response.json with every write, and
        the driver replaces its command file. A read that such a replacement cut across is
        the ordinary work of the shift and is made again within the same reading."""
        self.brief()
        self.driver.poll()
        self.end_turn()
        calm = shift_wait.observe(self.shift)
        self.assertEqual((calm[0]["status"], calm[0]["answered"]), ("ready", True))
        for name in ("driver-state.json", "driver-response.json", "driver-command.json"):
            with self.subTest(name=name), replaced_while_read(self.shift / name) as done:
                self.assertEqual(shift_wait.observe(self.shift), calm)
                self.assertEqual(done, [1])
        with replaced_while_read(self.shift / "driver-state.json", times=3) as done:
            result = self.wait()
        self.assertEqual((result["status"], done, self.sleeps), ("ready", [1, 2, 3], []))
        # STOP says `stopped` whether or not it could be read; that it was read again is
        # seen in the second replacement, which only a second read brings about.
        (self.shift / "STOP").write_text("{}")
        with replaced_while_read(self.shift / "STOP", times=2) as done:
            self.assertEqual(shift_wait.observe(self.shift)[0]["status"], "stopped")
        self.assertEqual(done, [1, 2])

    def test_a_state_file_that_never_holds_still_is_damaged_after_a_bounded_number_of_reads(self):
        self.brief()
        self.driver.poll()
        self.end_turn()
        with replaced_while_read(self.shift / "driver-state.json", times=None) as done:
            result = self.wait()
        self.assertEqual(result["status"], "damaged")
        self.assertEqual(len(done), receipts.SETTLE_TRIES)
        self.assertEqual(self.sleeps, [])

    def test_state_that_cannot_establish_idle_is_damaged_not_ready(self):
        self.brief()
        self.driver.poll()
        self.end_turn()
        self.assertEqual(self.wait()["status"], "ready")
        path = self.shift / "driver-state.json"
        good = decode(path.read_bytes())
        for name, bad in (("empty", {}), ("active list", dict(good, active=[])),
                          ("active unknown", dict(good, active="other")),
                          ("other grant", dict(good, grant_sha256="0" * 64)),
                          ("tasks list", dict(good, tasks=[])),
                          ("turn names another task", dict(good, turn=dict(task_id="other"))),
                          ("no active", {k: v for k, v in good.items() if k != "active"}),
                          ("no turn", {k: v for k, v in good.items() if k != "turn"}),
                          ("no mode", {k: v for k, v in good.items() if k != "mode"}),
                          ("turns not a list", dict(good, tasks={"task": dict(good["tasks"]["task"], turns=7)}))):
            with self.subTest(name=name):
                path.write_bytes(receipts.encoded(bad))
                result = self.wait()
                self.assertEqual((result["status"], result["mode"], result["turn"]), ("damaged", "unknown", None))
        path.unlink()
        # The command was answered as processed, so its state must exist.
        self.assertEqual(self.wait()["status"], "damaged")
        path.symlink_to(self.shift / "grant.json")
        self.assertEqual(self.wait()["status"], "damaged")
        path.unlink()
        path.mkdir()
        self.assertEqual(self.wait()["status"], "damaged")
        # Stop and expiry are reported first: they need no state.
        (self.shift / "STOP").write_text("{}")
        self.assertEqual(self.wait()["status"], "stopped")
        self.clock.sleep(1000)
        self.assertEqual(self.wait()["status"], "expired")

    def test_a_review_the_host_runs_is_waited_for_as_a_turn_is_and_its_outcome_is_told(self):
        self.reach_review()
        result = self.wait(1)
        self.assertEqual((result["status"], result["turn"], result["last_review"]), ("waiting", None, None))
        self.assertEqual(result["review"], dict(task_id="task", stage="review", round=0, attempt=1,
                                                started=self.state()["review"]["running"]["started"], limit_seconds=600))
        result = self.wait(during=lambda: self.end_review(changes=True))
        self.assertEqual((result["status"], result["review"], result["stage"], len(self.sleeps)),
                         ("ready", None, "repair_needed", 3))
        self.assertEqual((result["last_review"]["outcome"], result["last_review"]["task_id"]), ("CHANGES", "task"))
        self.assertIn("N1", result["last_review"]["detail"])

    def test_a_review_begun_with_no_attempt_running_is_not_waited_for(self):
        """After a failed attempt that was not run again the driver's `continue` runs it on."""
        self.reach_review()
        path = self.shift / "driver-state.json"
        state = decode(path.read_bytes())
        state["review"]["running"] = None
        path.write_bytes(receipts.encoded(state))
        result = self.wait()
        self.assertEqual((result["status"], result["review"], self.sleeps), ("ready", None, []))

    def test_a_state_from_before_the_host_ran_reviews_still_reads_and_a_damaged_review_record_does_not(self):
        self.brief()
        self.driver.poll()
        self.end_turn()
        path = self.shift / "driver-state.json"
        good = decode(path.read_bytes())
        path.write_bytes(receipts.encoded({k: v for k, v in good.items() if k not in ("review", "last_review")}))
        result = self.wait()
        self.assertEqual((result["status"], result["review"], result["last_review"]), ("ready", None, None))
        for name, bad in (("review not an object", dict(good, review=7)),
                          ("review names another task", dict(good, review=dict(task_id="other", running=None))),
                          ("running not an object", dict(good, review=dict(task_id="task", running=7))),
                          ("last review not an object", dict(good, last_review="ACCEPT"))):
            with self.subTest(name=name):
                path.write_bytes(receipts.encoded(bad))
                self.assertEqual(self.wait()["status"], "damaged")

    def test_an_invalid_answer_does_not_stand_in_for_missing_state(self):
        (self.shift / "driver-command.json").write_text("{")
        self.driver.poll()
        self.assertEqual(self.wait(2)["status"], "ready")
        (self.shift / "driver-state.json").unlink()  # a turn may have been running: unknown now
        result = self.wait(2)
        self.assertEqual((result["status"], result["response"]["status"]), ("damaged", "invalid"))

    def test_no_reading_starts_without_time_left_for_it(self):
        seen = []
        real = receipts.session
        def session(shift, seconds, **kw):
            seen.append((self.clock.now(), seconds))
            self.clock.sleep(0.01)  # a reading takes time too
            return real(shift, seconds, **kw)
        self.brief()
        with unittest.mock.patch("sortie_kit.shift_wait.receipts.session", side_effect=session):
            self.assertEqual(self.wait(1)["status"], "waiting")
            self.assertLessEqual(self.clock.now(), 1 + 1e-9)
            for at, seconds in seen[1:]:
                self.assertLessEqual(at + seconds, 1 + 1e-9)
            seen.clear()
            self.clock.sleep(998.5 - self.clock.now())
            result = self.wait(30)
        self.assertEqual((result["status"], result["seconds_left"]), ("expired", 0))
        self.assertLessEqual(self.clock.now(), 1000 + 1e-9)
        for at, seconds in seen[1:]:
            self.assertLessEqual(at + seconds, 1000 + 1e-9)

    def test_cli_prints_one_json_line_and_refuses_without_a_grant(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["wait", "--shift", str(self.shift), "--for", "1s"])
        line, = out.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(line)["status"], "ready")
        (self.shift / "grant.json").unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["wait", "--shift", str(self.shift)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out.getvalue())["status"], "refused")

    def test_a_wait_whose_first_reading_crosses_into_wind_down_returns_then(self):
        """Whether a wait began before wind-down is settled when it is called."""
        self.brief()
        self.driver.poll()
        self.clock.sleep(666.5)  # wind-down begins at 666.67
        real = shift_wait.observe

        def slow(shift, seconds=5):
            self.clock.sleep(0.4)
            return real(shift, seconds)
        with unittest.mock.patch.object(shift_wait, "observe", slow):
            result = self.wait()
        self.assertEqual((result["status"], result["wind_down"], result["seconds_before_wind_down"]),
                         ("waiting", True, 0))
        self.assertEqual(self.sleeps, [])

    def test_seconds_before_wind_down_is_zero_only_in_wind_down(self):
        self.clock.sleep(666.2)
        result = self.wait()
        self.assertEqual((result["wind_down"], result["seconds_before_wind_down"]), (False, 1))


for _name in tuple(vars(LifecycleFixture)):
    if _name.startswith("test_"):
        setattr(WaitTests, _name, None)
