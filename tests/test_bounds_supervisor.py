import datetime as dt
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from sortie_kit.bounds import Budget, Sources, duration
from sortie_kit.tick_supervise import Supervisor, spawn
from support import Clock, LocalTest, NOW


class BoundsTests(LocalTest):
    def test_duration_and_monotonic_cap_survive_wall_clock_rollback(self):
        self.assertEqual(duration("1.5m"), 90)
        self.assertEqual(duration("4h"), 14400)
        self.assertEqual(duration("1e-5"), 0.00001)
        clock = Clock()
        wall = [NOW]
        budget = Budget(10, clock=clock.now, now=lambda: wall[0], until=NOW + dt.timedelta(seconds=5))
        wall[0] -= dt.timedelta(hours=1)
        clock.sleep(5)
        self.assertEqual(budget.remaining(), 0)
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Budget(value)

    def test_source_change_add_remove_and_unreadable_fail_closed(self):
        for change in ("modify", "add", "remove", "symlink"):
            with self.subTest(change=change):
                root = self.root / change
                root.mkdir()
                code = root / "code.py"
                code.write_text("original")
                sources = Sources(root)
                self.assertEqual(sources.guard(), "")
                if change == "modify":
                    code.write_text("changed")
                elif change == "add":
                    (root / "helper.py").write_text("new")
                else:
                    code.unlink()
                    if change == "symlink":
                        code.symlink_to(root / "missing")
                self.assertIn("source", sources.guard())


class SupervisorTests(LocalTest):
    def sources(self):
        root = self.root / "code"
        root.mkdir()
        (root / "worker.py").write_text("original")
        return Sources(root)

    def test_deadline_stops_and_reaps_real_owned_child(self):
        supervisor = Supervisor(Budget(0.15), self.sources(), authorize=lambda: "")
        code, why = supervisor.run([sys.executable, "-c", "import time; time.sleep(5)"])
        self.assertEqual((code, why), (0, "duration elapsed"))
        self.assertIsNotNone(supervisor.child.poll())
        self.assertEqual(supervisor.launches, 1)
        self.assertEqual(supervisor.cleanup, "owned group stop signaled; child reaped")

    def refusing_child(self, refusals, at=("look", "terminate", "kill"), stubborn=False, code=None, answered=0):
        """A child whose group answers the first `refusals` of the calls named in `at`
        with the refusal macOS gives while a group leader is inside its own exit.
        A stubborn one ends only when killed. The first `answered` looks are answered,
        so that the refusals meet a later look."""
        class Exiting:
            returncode, sent, left, looks = code, [], refusals, 0
            def refuse(self, what):
                if what == "look":
                    self.looks += 1
                    if self.looks <= answered:
                        return
                if what in at and self.left > 0:
                    self.left -= 1
                    raise PermissionError("the leader is exiting")
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode
            def group_alive(self):
                self.refuse("look")
                return self.returncode is None
            def terminate(self):
                self.refuse("terminate")
                self.sent.append("terminate")
                if not stubborn:
                    self.returncode = -15
            def kill(self):
                self.refuse("kill")
                self.sent.append("kill")
                self.returncode = -9
        return Exiting()

    def owner(self, clock, name):
        root = self.root / name
        root.mkdir()
        (root / "worker.py").write_text("original")
        return Supervisor(Budget(5, clock=clock.now), Sources(root), authorize=lambda: "", sleep=clock.sleep)

    def test_a_refused_look_at_an_exiting_leader_is_repeated_within_the_bound(self):
        clock = Clock()
        for refusals in (1, 2, 7):
            with self.subTest(refusals=refusals):
                supervisor = self.owner(clock, "look-%d" % refusals)
                supervisor.child = child = self.refusing_child(refusals)
                began = clock.now()
                self.assertTrue(supervisor.stop_child())
                self.assertEqual(child.sent, ["terminate"])
                self.assertEqual(supervisor.cleanup, "owned group stop signaled; child reaped")
                self.assertLessEqual(clock.now() - began, 1.0)

    def test_a_look_refused_after_the_stop_request_or_before_the_kill_is_repeated(self):
        clock = Clock()
        # The usual case: the leader takes the stop request and is inside its exit at the next look.
        supervisor = self.owner(clock, "after-terminate")
        supervisor.child = child = self.refusing_child(3, at=("look",), answered=1)
        self.assertTrue(supervisor.stop_child())
        self.assertEqual((child.sent, child.looks), (["terminate"], 7))  # the last is the look after the reap
        # No time is left for the grace, so the third look is the one before the kill.
        supervisor = self.owner(clock, "before-kill")
        clock.sleep(5)
        self.assertEqual(supervisor.budget.remaining(), 0)
        supervisor.child = child = self.refusing_child(3, at=("look",), answered=2, stubborn=True)
        self.assertTrue(supervisor.stop_child())
        self.assertEqual((child.sent, child.looks), (["terminate", "kill"], 7))  # with the look after the reap

    def test_a_refused_signal_is_sent_again_and_never_counted_as_sent(self):
        clock = Clock()
        # The stop request is refused three times, then sent.
        supervisor = self.owner(clock, "terminate")
        supervisor.child = child = self.refusing_child(3, at=("terminate",))
        self.assertTrue(supervisor.stop_child())
        self.assertEqual(child.sent, ["terminate"])
        # A child that ignores the stop request is killed, and a refused kill is sent again.
        supervisor = self.owner(clock, "kill")
        supervisor.child = child = self.refusing_child(3, at=("kill",), stubborn=True)
        self.assertTrue(supervisor.stop_child())
        self.assertEqual(child.sent, ["terminate", "kill"])
        # A signal refused to the end of the bound is an error and nothing is recorded as cleaned.
        for at, sent in ((("terminate",), []), (("kill",), ["terminate"])):
            with self.subTest(at=at):
                supervisor = self.owner(clock, "never-" + at[0])
                supervisor.child = child = self.refusing_child(10 ** 9, at=at, stubborn=True)
                began = clock.now()
                with self.assertRaises(PermissionError):
                    supervisor.stop_child()
                self.assertEqual(child.sent, sent)
                self.assertAlmostEqual(clock.now() - began, 1.0, delta=0.01)
                self.assertIsNot(supervisor._cleaned_child, child)
                self.assertEqual(supervisor.cleanup, "no child")

    def test_a_refusal_that_outlasts_the_bound_is_still_an_error(self):
        clock = Clock()
        supervisor = self.owner(clock, "never")
        supervisor.child = child = self.refusing_child(10 ** 9)
        with self.assertRaises(PermissionError):
            supervisor.stop_child()
        self.assertEqual(child.sent, [])
        self.assertAlmostEqual(clock.now(), 1.0, delta=0.01)
        # Never recorded as cleaned: a later call looks again.
        self.assertIsNot(supervisor._cleaned_child, child)

    def test_nothing_is_tried_again_once_the_second_is_over(self):
        # A refusal met just before the end of the second: the wait after it is only what is
        # left, and no look or signal follows it, even one that would now be answered.
        clock = Clock()
        supervisor = self.owner(clock, "late")
        began = clock.now()

        class Late:
            returncode, sent, looks = None, [], 0
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode
            def group_alive(self):
                self.looks += 1
                if self.looks == 1:
                    clock.sleep(0.9995)  # the look itself took that long, and was refused
                    raise PermissionError("the leader is exiting")
                return True
            def terminate(self):
                self.sent.append("terminate")
            def kill(self):
                self.sent.append("kill")
        supervisor.child = child = Late()
        with self.assertRaises(PermissionError):
            supervisor.stop_child()
        self.assertEqual((child.looks, child.sent), (1, []))
        self.assertLessEqual(clock.now() - began, 1.0 + 1e-9)
        self.assertIsNot(supervisor._cleaned_child, child)

    def test_a_refusal_ends_the_stop_at_the_end_of_the_second_whichever_call_would_come_next(self):
        # One look is refused early and answered when repeated. Much later the look before the
        # kill is answered, and takes until the second is over: the kill is not sent after it.
        clock = Clock()
        supervisor = self.owner(clock, "late-kill")
        began = clock.now()

        class Slow:
            returncode, sent, looks, refused, slow = None, [], 0, False, False
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode
            def group_alive(self):
                self.looks += 1
                if self.looks == 2 and not self.refused:
                    self.refused = True
                    raise PermissionError("the leader is exiting")
                if clock.now() - began >= 0.5 and not self.slow:  # the look before the kill
                    self.slow = True
                    clock.sleep(0.6)
                return True
            def terminate(self):
                self.sent.append("terminate")
            def kill(self):
                self.sent.append("kill")
                self.returncode = -9
        supervisor.child = child = Slow()
        with self.assertRaises(PermissionError):
            supervisor.stop_child()
        self.assertTrue(child.refused and child.slow)
        self.assertEqual(child.sent, ["terminate"])
        self.assertIsNot(supervisor._cleaned_child, child)

    def jumping(self, clock, supervisor):
        """The supervisor's clock, made to pass the end of the second between two readings:
        once `arm()` is called, the next reading is as before and the one after it is two
        seconds later. Code that reads what is left twice, to check it and then to wait it,
        asks for a wait of less than nothing, which the fixture's sleep refuses."""
        real, state = clock.now, {"readings": None}

        def read():
            if state["readings"] is not None:
                state["readings"] -= 1
                if state["readings"] < 0:
                    state["readings"] = None
                    clock.sleep(2.0)
            return real()
        supervisor.budget.clock = read
        return lambda: state.update(readings=1)

    def test_a_wait_after_a_refusal_is_what_one_reading_of_the_clock_left(self):
        clock = Clock()
        supervisor = self.owner(clock, "boundary")
        arm = self.jumping(clock, supervisor)

        class Refusing:
            returncode, sent, looks = None, [], 0
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode
            def group_alive(self):
                self.looks += 1
                if self.looks == 1:
                    arm()
                raise PermissionError("the leader is exiting")
            def terminate(self):
                self.sent.append("terminate")
            def kill(self):
                self.sent.append("kill")
        supervisor.child = child = Refusing()
        with self.assertRaises(PermissionError):  # and not the refusal of a wait of less than nothing
            supervisor.stop_child()
        self.assertEqual((child.looks, child.sent), (1, []))

    def test_a_wait_for_the_group_after_the_reap_is_what_one_reading_of_the_clock_left(self):
        clock = Clock()
        supervisor = self.owner(clock, "boundary-reap")
        arm = self.jumping(clock, supervisor)

        class Lingering:
            returncode, sent = None, []
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode
            def group_alive(self):
                return True
            def terminate(self):
                self.sent.append("terminate")
            def kill(self):
                self.sent.append("kill")
                self.returncode = -9
                arm()
        supervisor.child = child = Lingering()
        self.assertFalse(supervisor.stop_child())
        self.assertEqual(supervisor.cleanup, "owned group still present after its stop")

    def test_a_group_still_there_after_the_kill_and_the_reap_is_not_a_confirmed_cleanup(self):
        # The leader was killed and reaped; something of its group still runs when the second is over.
        clock = Clock()
        supervisor = self.owner(clock, "lingering")

        class Lingering:
            returncode, sent = None, []
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode
            def group_alive(self):
                return True
            def terminate(self):
                self.sent.append("terminate")
            def kill(self):
                self.sent.append("kill")
                self.returncode = -9
        supervisor.child = child = Lingering()
        began = clock.now()
        self.assertFalse(supervisor.stop_child())
        self.assertEqual(child.sent, ["terminate", "kill"])
        self.assertEqual(supervisor.cleanup, "owned group still present after its stop")
        self.assertLessEqual(clock.now() - began, 1.0 + 1e-9)
        self.assertFalse(supervisor.stop_child())  # the same answer, without going through it again
        self.assertEqual(child.sent, ["terminate", "kill"])

    def test_a_group_that_goes_after_the_kill_within_the_second_is_a_confirmed_cleanup(self):
        clock = Clock()
        supervisor = self.owner(clock, "going")

        class Going:
            returncode, sent, after = None, [], 0
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                return self.returncode
            def group_alive(self):
                if self.returncode is not None:
                    self.after += 1
                return self.after < 3  # a descendant that takes two more looks to be gone
            def terminate(self):
                self.sent.append("terminate")
            def kill(self):
                self.sent.append("kill")
                self.returncode = -9
        supervisor.child = child = Going()
        self.assertTrue(supervisor.stop_child())
        self.assertEqual(supervisor.cleanup, "owned group stop signaled; child reaped")

    def test_a_run_whose_stop_was_refused_to_the_end_does_not_go_through_the_bound_twice(self):
        clock = Clock()
        supervisor = self.owner(clock, "run")
        child = self.refusing_child(10 ** 9, code=1)  # ended on its own; its group cannot be looked at
        supervisor.spawn = lambda argv, **kwargs: child
        with self.assertRaises(PermissionError):
            supervisor.run(["fixture"])
        self.assertAlmostEqual(clock.now(), 1.0, delta=0.01)

    def test_source_change_after_spawn_stops_without_restart(self):
        sources = self.sources()
        def changed(argv, **kwargs):
            child = spawn(argv, **kwargs)
            (sources.root / "worker.py").write_text("changed")
            return child
        supervisor = Supervisor(Budget(2), sources, authorize=lambda: "", spawn=changed)
        self.assertEqual(supervisor.run([sys.executable, "-c", "import time; time.sleep(5)"]),
                         (3, "source changed"))
        self.assertIsNotNone(supervisor.child.poll())
        self.assertEqual(supervisor.launches, 1)

    def test_interrupt_cleans_owned_child(self):
        sources = self.sources()
        supervisor = Supervisor(Budget(2), sources, authorize=lambda: "")
        def interrupted(argv, **kwargs):
            child = spawn(argv, **kwargs)
            supervisor.interrupted = True
            return child
        supervisor.spawn = interrupted
        self.assertEqual(supervisor.run([sys.executable, "-c", "import time; time.sleep(5)"]),
                         (3, "interrupted"))
        self.assertIsNotNone(supervisor.child.poll())

    def test_refusal_before_spawn_and_spawn_failure(self):
        supervisor = Supervisor(Budget(1), self.sources(), authorize=lambda: "withdrawn")
        with patch.object(supervisor, "spawn", side_effect=AssertionError("no launch")):
            self.assertEqual(supervisor.run(["fixture"]), (3, "withdrawn"))
        supervisor.authorize = lambda: ""
        with patch.object(supervisor, "spawn", side_effect=OSError("cannot spawn")):
            with self.assertRaises(OSError):
                supervisor.run(["fixture"])
        self.assertEqual(supervisor.cleanup, "no child")

    def test_restarts_back_off_and_receive_decreasing_lifetimes(self):
        clock = Clock()
        class Failed:
            returncode = 1
            def poll(self):
                return 1
        commands = []
        def failed(argv, **kwargs):
            commands.append(argv)
            return Failed()
        supervisor = Supervisor(Budget(3.5, clock=clock.now), self.sources(),
                                authorize=lambda: "", spawn=failed, sleep=clock.sleep)
        self.assertEqual(supervisor.run(["fixture"]), (0, "duration elapsed"))
        lifetimes = [float(a[-1]) for a in commands]
        self.assertEqual(len(lifetimes), 3)
        self.assertGreater(lifetimes[0], lifetimes[1])
        self.assertGreater(lifetimes[1], lifetimes[2])
        self.assertAlmostEqual(clock.now(), 3.5)
