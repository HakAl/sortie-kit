"""The hold against idle sleep: every system call here is a fake the test hands in."""
import ctypes
import os
import subprocess
import threading
import unittest

from sortie_kit import wake


class Wall:
    def __init__(self):
        self.value = 1_900_000_000.0

    def __call__(self):
        return self.value


class Mac:
    """Stands in for IOKit: hands out assertion ids and notes every call in order."""
    def __init__(self):
        self.calls, self.next, self.refuse, self.release_code, self.fail, self.same = [], 100, None, 0, None, False
        # `alive` answers `held`, or raises it: True in force, False there and not in force, None nothing given.
        self.held, self.asked, self.block, self.stuck = True, [], None, None

    def create(self, timeout):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("create", timeout))
        if self.block is not None:
            self.block.wait(10)  # a power request the system does not answer
        if self.refuse is not None:
            return self.refuse, 0
        self.next += 0 if self.same else 1
        return 0, self.next

    def release(self, identity):
        self.calls.append(("release", identity))
        return self.release_code

    def alive(self, identity):
        self.asked.append(identity)
        if self.stuck is not None:
            self.stuck.wait(10)
        if isinstance(self.held, Exception):
            raise self.held
        return self.held


class Child:
    """Stands in for the systemd-inhibit process. `ready()` is the byte its command prints
    once the lock is held; the pipe it comes through is a real one of this test's own."""
    def __init__(self):
        self.code, self.signals, self.stdin = None, [], Pipe()
        reading, self.writing = os.pipe()
        self.stdout, self.open = open(reading, "rb", buffering=0), True

    def ready(self):
        os.write(self.writing, b"1")

    def gone(self):
        if self.open:
            self.open = False
            os.close(self.writing)

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.stdin.closed and self.code is None:
            self.code = 0
        if self.code is None:
            raise subprocess.TimeoutExpired("systemd-inhibit", timeout)
        return self.code

    def terminate(self):
        self.signals.append("terminate")
        self.code = -15

    def kill(self):
        self.signals.append("kill")
        self.code = -9


class Pipe:
    closed = False

    def close(self):
        self.closed = True


class Library:
    """Stands in for the two system libraries that `mac_system` loads."""
    def __init__(self, fail=False):
        def create(*arguments):
            if fail:
                raise ctypes.ArgumentError("argument 6: wrong type")
            arguments[-1]._obj.value = 7  # the id, written where IOKit would write it
            return 0

        def release(identity):
            if fail:
                raise ctypes.ArgumentError("argument 1: wrong type")
            return 0

        def properties(identity):
            if fail:
                raise ctypes.ArgumentError("argument 1: wrong type")
            return "properties" if identity.value == 7 else None  # given only for an assertion the system has
        # What the system says of assertion 7: its level, and the two marks of one that is not in force.
        self.level, self.suspended, self.timed_out = 255, None, None

        def value(properties, key):
            return {"AssertLevel": None if self.level is None else "level", "AssertionIsStateSuspended": self.suspended,
                    "AssertTimedOutWhen": self.timed_out}[key]

        def number(given, kind, where):
            if given != "level" or not isinstance(self.level, int):
                return 0  # not a number CoreFoundation can give
            where._obj.value = self.level
            return 1
        self.freed = []
        self.CFStringCreateWithCString = lambda allocator, value, encoding: value.decode()
        self.CFRelease = lambda given: self.freed.append(given)
        self.CFDictionaryGetValue, self.CFNumberGetValue = value, number
        self.CFBooleanGetValue = lambda given: 1 if given == "true" else 0
        self.CFNumberGetTypeID, self.CFBooleanGetTypeID = (lambda: 22), (lambda: 21)
        self.CFGetTypeID = lambda given: 22 if given == "level" else 21 if given in ("true", "false") else 7
        self.IOPMAssertionCreateWithDescription, self.IOPMAssertionRelease = create, release
        self.IOPMAssertionCopyProperties = properties


class MacTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.mac = Wall(), Mac()
        self.wake = wake.Wake(platform="darwin", system=self.mac, wall=self.wall, clock=self.wall)

    def test_the_timeout_is_at_least_twice_the_renewal_interval(self):
        self.assertGreaterEqual(wake.TIMEOUT, 2 * wake.RENEW)

    def test_acquire_holds_one_assertion_with_a_timeout_and_the_record_says_whole(self):
        self.wake.acquire()
        self.assertEqual(self.mac.calls, [("create", wake.TIMEOUT)])
        seen = self.wake.record()
        self.assertEqual((seen["asked"], seen["held"], seen["whole"]), (True, True, True))
        self.assertIn("PreventUserIdleSystemSleep", seen["means"])
        self.assertEqual((seen["refusals"], seen["gaps"]), ([], []))
        self.assertEqual(seen["held_from"], "2030-03-17T17:46:40+00:00")
        self.assertEqual(seen["seen"], seen["held_from"])

    def test_renewal_waits_for_the_interval_and_creates_the_new_one_before_it_releases_the_old(self):
        self.wake.acquire()
        self.wall.value += wake.RENEW - 1
        self.wake.renew()
        self.assertEqual(len(self.mac.calls), 1)
        self.wall.value += 1
        self.wake.renew()
        self.assertEqual(self.mac.calls[1:], [("create", wake.TIMEOUT), ("release", 101)])
        self.assertTrue(self.wake.record()["whole"])

    def test_a_refused_acquire_is_recorded_with_its_return_code_and_tried_again(self):
        self.mac.refuse = -0x1FFFFD3F
        self.wake.acquire()
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["held_from"]), (False, False, None))
        self.assertEqual(seen["refusals"], [dict(at="2030-03-17T17:46:40+00:00", what="create", code=-0x1FFFFD3F)])
        self.assertEqual(seen["refusals_count"], 1)
        # Tried again at the next renewal; held from then on, and still not for the whole shift.
        self.mac.refuse = None
        self.wall.value += wake.RENEW
        self.wake.renew()
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"]), (True, False))
        self.assertEqual(seen["held_from"], "2030-03-17T17:47:10+00:00")

    def test_a_refused_renewal_keeps_the_old_assertion_and_is_recorded(self):
        self.wake.acquire()
        self.mac.refuse = -5
        self.wall.value += wake.RENEW
        self.wake.renew()
        self.assertNotIn(("release", 101), self.mac.calls)  # nothing is given up for a renewal that failed
        seen = self.wake.record()
        self.assertEqual((seen["whole"], seen["refusals"][0]["code"]), (False, -5))

    def test_a_renewal_that_comes_after_the_timeout_is_a_gap(self):
        self.wake.acquire()
        self.wall.value += wake.TIMEOUT + 600  # the host was held up, or the machine slept anyway
        self.wake.renew()
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["gaps_count"]), (True, False, 1))
        self.assertEqual(seen["gaps"], [{"from": "2030-03-17T17:46:40+00:00", "to": "2030-03-17T17:58:40+00:00"}])

    def test_the_record_itself_sees_a_lapse_that_no_renewal_has_met_yet(self):
        self.wake.acquire()
        self.wall.value += wake.TIMEOUT + 1
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"]), (False, False))

    def test_release_is_done_once_and_an_assertion_dropped_at_its_timeout_is_no_failure(self):
        self.wake.acquire()
        self.wall.value += wake.TIMEOUT + 1  # its timeout came first: the system was right to drop it
        self.mac.release_code = wake.ALREADY_RELEASED
        self.wake.release()
        self.wake.release()
        self.assertEqual(self.mac.calls.count(("release", 101)), 1)
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["refusals"]), (False, []))
        self.wake.renew()  # nothing is taken up again after the release
        self.assertEqual(len(self.mac.calls), 2)

    def test_an_assertion_the_system_no_longer_held_before_its_timeout_is_recorded(self):
        self.wake.acquire()
        self.mac.release_code = wake.ALREADY_RELEASED
        self.wake.release()
        seen = self.wake.record()
        self.assertEqual(seen["refusals_count"], 1)
        self.assertEqual(seen["refusals"][0]["code"], wake.ALREADY_RELEASED)
        self.assertIn("the system no longer held it", seen["refusals"][0]["what"])

    def test_the_same_met_at_a_renewal_means_the_shift_was_not_held_whole(self):
        self.wake.acquire()
        self.wall.value += wake.RENEW
        self.mac.release_code = wake.ALREADY_RELEASED
        self.wake.renew()
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (True, False, 1))
        self.assertIn("the system no longer held it", seen["refusals"][0]["what"])

    # What the record says of the hold is what the system says at that moment.

    def test_a_hold_the_system_gives_nothing_for_is_recorded_as_not_known_and_is_still_let_go(self):
        # Nothing given is what the system answers for an assertion it let go early, and also
        # what comes back when it could not be reached: the two cannot be told apart.
        self.wake.acquire()
        self.mac.held = None
        seen = self.wake.record()
        self.assertEqual(self.mac.asked, [101])
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (None, False, 1))
        self.assertIn("the system gave nothing for it", seen["refusals"][0]["what"])
        again = self.wake.record()
        self.assertEqual((again["held"], again["refusals_count"]), (None, 1))  # said once
        self.wake.release()
        self.assertIn(("release", 101), self.mac.calls)  # it may still be held: the id is kept and let go

    def test_a_hold_the_system_has_but_not_in_force_is_recorded_as_not_held_and_is_still_let_go(self):
        self.wake.acquire()
        self.mac.held = False  # suspended, switched off or timed out: there, and stopping no sleep
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (False, False, 1))
        self.assertIn("the system has it, but not in force", seen["refusals"][0]["what"])
        again = self.wake.record()
        self.assertEqual((again["held"], again["refusals_count"]), (False, 1))  # said once
        self.mac.held = True  # in force again: held now, and the shift stays not whole
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (True, False, 1))
        self.wake.release()
        self.assertIn(("release", 101), self.mac.calls)

    def test_a_hold_that_was_dropped_is_taken_again_at_the_next_renewal_and_the_shift_is_not_whole(self):
        self.wake.acquire()
        self.mac.held = None
        self.wake.record()
        self.mac.held = True
        self.wall.value += wake.RENEW
        self.wake.renew()
        # The old id is let go after the new one is made: whether it was still held was not known.
        self.assertEqual(self.mac.calls, [("create", wake.TIMEOUT), ("create", wake.TIMEOUT), ("release", 101)])
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"]), (True, False))

    def test_when_the_system_cannot_be_asked_the_record_says_not_known(self):
        self.wake.acquire()
        self.mac.held = OSError("no answer")
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (None, False, 1))
        self.assertIn("whether it is held could not be asked: OSError: no answer", seen["refusals"][0]["what"])
        self.mac.held = True  # asked again later it answers; the shift stays not whole
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"]), (True, False))

    def test_an_assertion_past_its_timeout_is_not_asked_about(self):
        self.wake.acquire()
        self.wall.value += wake.TIMEOUT + 1
        self.assertFalse(self.wake.record()["held"])
        self.assertEqual(self.mac.asked, [])

    # No call into the system is waited on for long.

    def patient(self, patience=0.05):
        return wake.Wake(platform="darwin", system=self.mac, wall=self.wall, clock=self.wall, patience=patience)

    def test_a_request_the_system_does_not_answer_is_given_up_and_no_other_is_made_until_it_has(self):
        self.mac.block = threading.Event()
        self.addCleanup(self.mac.block.set)
        held = self.patient()
        held.acquire()  # comes back
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (False, False, 1))
        self.assertIn("did not return within 0.05s", seen["refusals"][0]["what"])
        held.patience = 30  # were another call made now, this test would wait on it
        self.wall.value += wake.RENEW
        held.renew()
        self.assertEqual(self.mac.calls, [("create", wake.TIMEOUT)])  # no second request beside the first
        self.assertIn("an earlier call into the system has not returned", held.record()["refusals"][1]["what"])
        # The request comes back after all: what it made is let go at once, and not kept.
        stuck = held.pending
        self.mac.block.set()
        stuck.join(5)
        self.assertFalse(stuck.is_alive())
        self.assertEqual(self.mac.calls[1:], [("release", 101)])
        self.assertIsNone(held.identity)
        self.wall.value += wake.RENEW
        held.renew()
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (True, False, 2))
        self.assertEqual(self.mac.calls[2:], [("create", wake.TIMEOUT)])
        self.assertEqual(self.mac.asked, [102])

    def test_a_question_the_system_does_not_answer_leaves_the_record_not_known_and_the_release_does_not_wait(self):
        held = self.patient()
        held.acquire()
        self.mac.stuck = threading.Event()
        self.addCleanup(self.mac.stuck.set)
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"]), (None, False))
        self.assertIn("did not return within 0.05s", seen["refusals"][0]["what"])
        held.patience = 30
        held.release()  # at once: nothing is asked of a system that still owes an answer
        self.assertNotIn(("release", 101), self.mac.calls)  # left to its timeout
        seen = held.record()
        self.assertEqual((seen["held"], seen["refusals_count"]), (False, 2))
        self.assertIn("release: OSError: an earlier call", seen["refusals"][1]["what"])

    def test_the_question_for_the_record_is_waited_on_for_less_than_any_other_call(self):
        # It is asked while the closeout is written, out of the seconds kept for the report.
        import time
        held = self.patient(30)
        held.acquire()
        self.mac.stuck = threading.Event()
        self.addCleanup(self.mac.stuck.set)
        began = time.monotonic()
        seen = held.record()
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(seen["held"], None)
        self.assertIn("did not return within %gs" % wake.ASK, seen["refusals"][0]["what"])

    def test_a_renewal_judges_the_age_of_the_old_hold_at_one_moment(self):
        # Between two readings of the clock the timeout may pass: then neither a gap nor an early loss is seen.
        self.wake.acquire()
        ticks = iter([wake.TIMEOUT, wake.TIMEOUT + 1])
        start = self.wall.value
        self.wake.clock = lambda: start + next(ticks, wake.TIMEOUT + 1)
        self.mac.release_code = wake.ALREADY_RELEASED  # the system no longer holds the old one
        self.wake.renew()
        self.wake.clock = lambda: start + wake.TIMEOUT + 1
        seen = self.wake.record()
        self.assertEqual((seen["whole"], seen["gaps_count"] + seen["refusals_count"]), (False, 1))

    def test_a_call_that_returns_in_time_gives_its_value_or_its_error_and_leaves_nothing_pending(self):
        held = self.patient(5)
        self.assertEqual(held.call(lambda a, b: a + b, 1, 2), 3)
        with self.assertRaises(KeyError):
            held.call({}.__getitem__, "none")
        self.assertIsNone(held.pending)

    # Ages are on a clock that goes on in sleep; the wall only gives the stamps.

    def apart(self):
        self.clock = Wall()
        self.clock.value = 5000.0
        self.wake = wake.Wake(platform="darwin", system=self.mac, wall=self.wall, clock=self.clock)
        self.wake.acquire()

    def test_a_wall_clock_that_is_set_is_no_gap_and_hides_none(self):
        for jump in (7200, -7200):
            with self.subTest(jump=jump):
                self.mac = Mac()
                self.apart()
                self.wall.value += jump
                self.clock.value += wake.RENEW
                self.wake.renew()
                self.assertEqual(len(self.mac.calls), 3)  # renewed when due by the clock that is not set
                seen = self.wake.record()
                self.assertEqual((seen["held"], seen["whole"], seen["gaps_count"], seen["refusals_count"]), (True, True, 0, 0))
        # And a wall that stands still does not make an expired hold look current.
        self.mac = Mac()
        self.apart()
        self.clock.value += wake.TIMEOUT + 1
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], self.mac.asked), (False, False, []))

    def test_a_gap_is_measured_on_the_continuing_clock_and_stamped_from_the_wall(self):
        self.apart()
        self.clock.value += wake.TIMEOUT + 600
        self.wall.value += 60  # the wall says a minute; the clock that went on in sleep says twelve
        self.wake.renew()
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["gaps_count"]), (True, False, 1))
        self.assertEqual(seen["gaps"], [{"from": "2030-03-17T17:46:40+00:00", "to": "2030-03-17T17:47:40+00:00"}])

    def test_the_continuing_clock_is_one_that_goes_on_in_sleep_where_the_platform_has_one(self):
        import sys
        import time
        before = wake.continuing()
        self.assertIsInstance(before, float)
        self.assertGreaterEqual(wake.continuing(), before)
        if sys.platform == "darwin":
            self.assertLess(abs(before - time.clock_gettime(time.CLOCK_MONOTONIC)), 5)
        elif sys.platform.startswith("linux"):
            self.assertLess(abs(before - time.clock_gettime(time.CLOCK_BOOTTIME)), 5)

    def test_the_real_calls_pass_the_code_and_the_id_on(self):
        library = Library()
        system = wake.mac_system(load=lambda: (library, library))
        self.assertEqual(system.create(wake.TIMEOUT), (0, 7))
        self.assertEqual((system.alive(7), library.freed), (True, ["properties"]))  # what the system gave is given back
        self.assertEqual((system.alive(8), library.freed), (None, ["properties"]))  # nothing given: gone, or not reached
        self.assertEqual(system.release(7), 0)

    def test_properties_alone_do_not_make_an_assertion_one_that_is_in_force(self):
        # The system keeps the properties of an assertion it has suspended, switched off or
        # timed out, and gives them like any other.
        library = Library()
        system = wake.mac_system(load=lambda: (library, library))
        for change, wanted in ((dict(suspended="true"), False), (dict(timed_out="a date"), False), (dict(level=0), False),
                               (dict(suspended="false"), True), (dict(), True),
                               (dict(level=None), None), (dict(level="no number"), None),  # no level: not known
                               (dict(suspended="a string"), None)):  # a mark that is no yes or no
            with self.subTest(change=change):
                library.level, library.suspended, library.timed_out = 255, None, None
                vars(library).update(change)
                library.freed.clear()
                self.assertIs(system.alive(7), wanted)
                self.assertEqual(library.freed, ["properties"])

    def test_a_renewal_with_too_little_time_for_its_calls_is_not_begun(self):
        # The host says how much working time is left. Making a new hold and letting the old one
        # go are two calls, each waited on for up to `patience`: with less than that left, the
        # renewal is left out, and that is no refusal, since the hold made last still runs.
        self.wake.acquire()
        self.wall.value += wake.RENEW
        self.wake.renew(2 * wake.PATIENCE - 0.01)
        self.assertEqual(len(self.mac.calls), 1)
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (True, True, 0))
        self.wake.renew(2 * wake.PATIENCE)
        self.assertEqual(self.mac.calls[1:], [("create", wake.TIMEOUT), ("release", 101)])
        self.assertGreaterEqual(wake.TIMEOUT - wake.RENEW, 60)  # what was renewed last outlasts any stop

    def test_arguments_that_ctypes_refuses_are_a_recorded_refusal_and_not_raised(self):
        library = Library(fail=True)
        system = wake.mac_system(load=lambda: (library, library))
        with self.assertRaises(OSError):
            system.create(wake.TIMEOUT)
        with self.assertRaises(OSError):
            system.release(7)
        with self.assertRaises(OSError):
            system.alive(7)
        held = wake.Wake(platform="darwin", system=system, wall=self.wall, clock=self.wall)
        held.acquire()
        held.release()
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals"][0]["code"]), (False, False, None))
        self.assertIn("OSError", seen["refusals"][0]["what"])

    def test_a_failed_release_is_recorded(self):
        self.wake.acquire()
        self.mac.release_code = -7
        self.wake.release()
        self.assertEqual(self.wake.record()["refusals"][0], dict(at="2030-03-17T17:46:40+00:00", what="release", code=-7))

    def test_a_system_call_that_raises_is_recorded_and_never_raised(self):
        for failure in (OSError("no IOKit"), AttributeError("no such symbol"), ValueError("bad")):
            mac = Mac()
            mac.fail = failure
            held = wake.Wake(platform="darwin", system=mac, wall=self.wall, clock=self.wall)
            held.acquire()
            held.renew()
            held.release()
            seen = held.record()
            self.assertEqual((seen["held"], seen["whole"]), (False, False))
            self.assertEqual(seen["refusals"][0]["code"], None)
            self.assertIn(type(failure).__name__, seen["refusals"][0]["what"])

    def test_a_clock_set_back_does_not_hide_an_assertion_the_system_no_longer_held(self):
        self.wake.acquire()
        self.wall.value -= 3600
        self.mac.release_code = wake.ALREADY_RELEASED
        self.wake.renew()
        seen = self.wake.record()
        self.assertEqual((seen["whole"], seen["refusals_count"]), (False, 1))

    def test_an_id_the_system_hands_out_again_is_not_released_as_the_old_one(self):
        self.wake.acquire()
        self.mac.same = True  # the old assertion was gone, and its id is the new one's
        self.wall.value += wake.RENEW
        self.wake.renew()
        self.assertNotIn(("release", 101), self.mac.calls)  # that would let go of the one just made
        seen = self.wake.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (True, False, 1))
        self.assertIn("the system no longer held it", seen["refusals"][0]["what"])
        self.wake.release()
        self.assertEqual(self.mac.calls.count(("release", 101)), 1)

    def test_one_lapse_is_one_gap_however_long_the_renewals_keep_failing(self):
        self.wake.acquire()
        self.mac.refuse = -5
        self.wall.value += wake.TIMEOUT + 1
        for _ in range(4):
            self.wake.renew()
            self.wall.value += wake.RENEW
        seen = self.wake.record()
        self.assertEqual((seen["gaps_count"], len(seen["gaps"]), seen["refusals_count"]), (1, 1, 4))

    def test_an_import_that_fails_under_a_system_call_is_a_recorded_refusal(self):
        mac = Mac()
        mac.fail = ImportError("No module named '_ctypes'")
        held = wake.Wake(platform="darwin", system=mac, wall=self.wall, clock=self.wall)
        held.acquire()
        self.assertIn("ImportError", held.record()["refusals"][0]["what"])

    def test_only_a_few_refusals_are_kept_and_all_are_counted(self):
        self.mac.refuse = -1
        self.wake.acquire()
        for _ in range(20):
            self.wall.value += wake.RENEW
            self.wake.renew()
        seen = self.wake.record()
        self.assertEqual((len(seen["refusals"]), seen["refusals_count"]), (wake.KEPT, 21))

    def test_turned_off_it_touches_nothing_and_says_so(self):
        off = wake.Wake(False, platform="darwin", system=self.mac, wall=self.wall, clock=self.wall)
        off.acquire(), off.renew(), off.release()
        self.assertEqual(self.mac.calls, [])
        seen = off.record()
        self.assertEqual((seen["asked"], seen["held"], seen["whole"]), (False, False, False))
        self.assertIn("--no-wake", seen["means"])
        self.assertIn("--no-wake", off.plan())

    def test_the_plan_is_one_ascii_line_that_names_the_means(self):
        line = self.wake.plan()
        self.assertNotIn("\n", line)
        line.encode("ascii")
        self.assertIn("idle sleep", line)
        self.assertIn("macOS power assertion", line)
        self.assertEqual(self.mac.calls, [])  # saying what will be done does none of it


class LinuxTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.started = Wall(), []

    def popen(self, argv, **options):
        self.started.append((argv, options))
        return self.child

    def made(self, found="/usr/bin/systemd-inhibit", ready=True):
        self.child = Child()
        self.addCleanup(self.child.stdout.close)
        self.addCleanup(self.child.gone)
        if ready:
            self.child.ready()
        return wake.Wake(platform="linux", wall=self.wall, clock=self.wall, which=lambda name: found, popen=self.popen)

    def test_systemd_inhibit_is_held_through_a_pipe_of_the_hosts_and_stopped_at_the_end(self):
        held = self.made()
        held.acquire()
        argv, options = self.started[0]
        self.assertEqual(argv[:2], ["/usr/bin/systemd-inhibit", "--what=idle"])
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "printf 1; exec cat"])  # says so once the lock is held
        self.assertIn("--mode=block", argv)
        self.assertEqual(options["stdin"], subprocess.PIPE)
        self.assertEqual((options["stdout"], options["stderr"]), (subprocess.PIPE, subprocess.DEVNULL))
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"]), (True, True))
        self.assertIn("systemd-inhibit", seen["means"])
        self.wall.value += 10 * wake.TIMEOUT  # nothing to renew: no gap however long a pass takes
        held.renew()
        self.assertTrue(held.record()["whole"])
        held.release()
        self.assertTrue(self.child.stdin.closed)
        self.assertEqual(self.child.signals, [])
        self.assertFalse(held.record()["held"])

    def test_an_inhibitor_that_ended_by_itself_is_a_refusal_with_its_exit_status(self):
        held = self.made()
        held.acquire()
        self.child.code = 1
        held.renew()
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"]), (False, False))
        self.assertEqual((seen["refusals"][0]["what"], seen["refusals"][0]["code"]), ("systemd-inhibit ended", 1))
        self.assertTrue(self.child.stdin.closed)  # the host's end of the pipe is not left open
        held.release()
        self.assertEqual(held.record()["refusals_count"], 1)

    def test_the_record_itself_sees_an_inhibitor_that_ended_at_once(self):
        held = self.made()
        held.acquire()
        self.child.code = 1  # no pass of the host's loop has looked yet
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"]), (False, False))
        self.assertEqual((seen["refusals"][0]["what"], seen["refusals"][0]["code"]), ("systemd-inhibit ended", 1))
        self.assertEqual(held.record()["refusals_count"], 1)  # said once

    def test_with_no_starter_handed_in_the_child_is_started_through_the_modules_own(self):
        self.child, before = Child(), wake.start_inhibitor
        wake.start_inhibitor = self.popen
        try:
            self.addCleanup(self.child.stdout.close)
            self.addCleanup(self.child.gone)
            self.child.ready()
            held = wake.Wake(platform="linux", wall=self.wall, clock=self.wall, which=lambda name: "/usr/bin/systemd-inhibit")
            held.acquire()
        finally:
            wake.start_inhibitor = before
        self.assertEqual(self.started[0][0][0], "/usr/bin/systemd-inhibit")
        self.assertTrue(held.record()["held"])

    def test_it_is_not_held_until_the_inhibitor_says_it_has_the_lock(self):
        # The program starts before it has asked the system for anything: a start is not a hold.
        held = self.made(ready=False)
        held.acquire()
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"], seen["held_from"], seen["refusals"]), (False, False, None, []))
        self.wall.value += 2
        self.child.ready()
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"]), (True, True))
        self.assertEqual(seen["held_from"], "2030-03-17T17:46:42+00:00")

    def test_an_inhibitor_that_never_says_so_is_a_refusal_said_once(self):
        held = self.made(ready=False)
        held.acquire()
        self.wall.value += wake.CONFIRM + 1
        held.renew()
        held.renew()
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (False, False, 1))
        self.assertIn("did not say it holds the lock", seen["refusals"][0]["what"])
        self.child.ready()  # late, and then it is held, though not for the whole shift
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"]), (True, False))

    def test_a_word_first_seen_after_the_deadline_is_a_hold_from_then_and_not_for_the_whole_shift(self):
        # The byte lies in the pipe already; when it came is not known, since nobody looked in time.
        held = self.made()
        held.acquire()
        self.wall.value += wake.CONFIRM + 1
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals_count"]), (True, False, 1))
        self.assertIn("was seen to hold the lock only after 10s", seen["refusals"][0]["what"])
        self.assertEqual(seen["held_from"], "2030-03-17T17:46:51+00:00")
        self.assertEqual(held.record()["refusals_count"], 1)

    def test_a_word_seen_just_inside_the_deadline_counts_for_the_whole_shift(self):
        held = self.made()
        held.acquire()
        self.wall.value += wake.CONFIRM - 0.5
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals"]), (True, True, []))

    def test_a_closed_pipe_with_no_word_is_not_a_hold(self):
        held = self.made(ready=False)
        held.acquire()
        self.child.gone()
        self.assertFalse(held.record()["held"])

    def test_a_child_whose_output_cannot_be_made_unblocking_is_stopped_and_not_kept(self):
        held = self.made()
        self.child.stdout.close()  # so that its descriptor cannot be set up
        held.acquire()
        seen = held.record()  # and no look ever waits on that pipe
        self.assertEqual((seen["held"], seen["whole"]), (False, False))
        self.assertIn("systemd-inhibit not started", seen["refusals"][0]["what"])
        self.assertTrue(self.child.stdin.closed)
        self.assertIsNone(held.child)

    def test_a_pipe_that_will_not_close_does_not_keep_the_inhibitor_from_being_stopped(self):
        held = self.made()
        held.acquire()
        self.child.stdin.close = lambda: (_ for _ in ()).throw(OSError("bad descriptor"))
        held.release()
        self.assertEqual(self.child.signals, ["terminate"])
        self.assertIn("OSError", held.record()["refusals"][0]["what"])

    def test_an_inhibitor_that_does_not_end_is_signaled(self):
        held = self.made()
        held.acquire()
        self.child.stdin.close = lambda: None  # the pipe closes and the child stays
        held.release()
        self.assertEqual(self.child.signals, ["terminate"])

    def test_a_start_that_fails_is_recorded_and_not_raised(self):
        held = self.made()
        held.popen = lambda argv, **options: (_ for _ in ()).throw(OSError("cannot start"))
        held.acquire()
        seen = held.record()
        self.assertEqual((seen["held"], seen["whole"], seen["refusals"][0]["code"]), (False, False, None))

    def test_without_systemd_inhibit_there_is_no_lock_and_that_is_said(self):
        none = self.made(found=None)
        none.acquire(), none.renew(), none.release()
        self.assertEqual(self.started, [])
        seen = none.record()
        self.assertEqual((seen["asked"], seen["held"], seen["whole"]), (True, False, False))
        self.assertIn("systemd-inhibit is not installed", seen["means"])
        self.assertIn("systemd-inhibit is not installed", none.plan())

    def test_another_platform_has_no_means_and_that_is_said(self):
        none = wake.Wake(platform="win32", wall=self.wall, clock=self.wall)
        none.acquire()
        seen = none.record()
        self.assertEqual((seen["held"], seen["whole"]), (False, False))
        self.assertIn("win32", seen["means"])


class NoRealCallTests(unittest.TestCase):
    def test_the_test_support_module_forbids_the_real_system_call(self):
        import support  # noqa: F401  (replaces the factory when it is imported)
        with self.assertRaises(AssertionError):
            wake.Wake(platform="darwin").acquire()
        with self.assertRaises(AssertionError):
            wake.Wake(platform="linux", which=lambda name: "/usr/bin/systemd-inhibit").acquire()


if __name__ == "__main__":
    unittest.main()
