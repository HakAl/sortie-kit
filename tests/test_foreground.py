"""The interactive driver's terminal handoff, in a disposable PTY; never a developer pane."""
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import sys
import time

from support import LocalTest

HOST = r'''
import json, os, signal, sys, termios, time
from pathlib import Path
from sortie_kit.tick_supervise import ForegroundSeat
out, mode, child_code = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def settings():
    value = termios.tcgetattr(0)
    value[3] &= ~getattr(termios, "PENDIN", 0)  # transient on macOS, not a setting
    return value
before = settings()
handlers = [signal.getsignal(s) for s in (signal.SIGTTOU, signal.SIGTTIN)]
seat = ForegroundSeat([sys.executable, "-B", "-c", child_code, str(out) + ".child"], cwd=str(out.parent))
during = dict(host_foreground=os.tcgetpgrp(0) == os.getpgrp(), child_group=os.tcgetpgrp(0) == seat.child.pid)
end = time.monotonic() + (0.6 if mode == "stop" else 8)
while not seat.ended() and time.monotonic() < end:
    time.sleep(0.02)
ended = seat.ended()
outcome = seat.close()
os.write(1, b"host writes after reclaim\n")   # would stop a background group
out.write_text(json.dumps(dict(during, ended=ended, outcome=outcome, again=seat.close(),
    reclaimed=os.tcgetpgrp(0) == os.getpgrp(), attributes=settings() == before,
    handlers=[signal.getsignal(s) for s in (signal.SIGTTOU, signal.SIGTTIN)] == handlers,
    code=seat.child.returncode)))
'''
CHILD_HEAD = r'''
import json, os, signal, sys, termios, time
from pathlib import Path
seen = dict(foreground=os.tcgetpgrp(0) == os.getpgrp(), leader=os.getpgrp() == os.getpid(),
            ttou=signal.getsignal(signal.SIGTTOU) == signal.SIG_DFL,
            ttin=signal.getsignal(signal.SIGTTIN) == signal.SIG_DFL, tty=all(os.isatty(n) for n in (0, 1, 2)))
# Agent CLIs watch their terminal with the platform's event queue. macOS refuses the
# /dev/tty alias there (EINVAL) and accepts only the terminal's own device.
import select
seen["device"] = [os.ttyname(n) == "/dev/tty" for n in (0, 1, 2)]
seen["watchable"] = []
for n in (0, 1, 2):
    try:
        if hasattr(select, "kqueue"):
            select.kqueue().control([select.kevent(n, select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0, 0)
        else:
            select.epoll().register(n, select.EPOLLIN)
        seen["watchable"].append("ok")
    except OSError as exc:
        seen["watchable"].append(repr(exc))
raw = termios.tcgetattr(0); raw[3] &= ~(termios.ECHO | termios.ICANON); termios.tcsetattr(0, termios.TCSANOW, raw)
def keep(**more): Path(sys.argv[1]).write_text(json.dumps(dict(seen, **more)))
keep()
'''
# What a full-screen program turns on and one stopped by a signal leaves on: an entry
# on the keyboard stack of the main screen, the alternate screen, an entry on its stack.
MODES_ON = r'os.write(1, b"\x1b[>1u\x1b[?1049h\x1b[>3u\x1b[?1000h")' + "\n"
CHILDREN = {
    "exit": CHILD_HEAD + 'print("driver ready", flush=True)\nkeep(read=os.read(0, 5).decode())\nsys.exit(7)\n',
    "stop": CHILD_HEAD + 'print("driver ready", flush=True)\ntime.sleep(30)\n',
    "modes": CHILD_HEAD + MODES_ON + 'print("driver ready", flush=True)\ntime.sleep(30)\n',
    "suspend": CHILD_HEAD + 'print("driver ready", flush=True)\nos.kill(os.getpid(), signal.SIGSTOP)\ntime.sleep(30)\n',
}


class Terminal:
    """The other end of the PTY: what a terminal keeps of the state the host asks about,
    and its answers. The caller of the host has one entry of its own on the keyboard
    stack of the main screen and a saved cursor; each screen has its own stack."""
    SEEN = re.compile(rb"\x1b\[\?u|\x1b\[\?1049\$p|\x1b\[c|\x1b\[>(\d+)u|\x1b\[<u|\x1b\[\?1049([hl])|\x1b7")

    def __init__(self, answers="all"):
        self.answers, self.done = answers, 0
        self.alternate, self.stacks, self.cursor = False, {False: [5], True: []}, "the caller's"

    def feed(self, output, master):
        for found in self.SEEN.finditer(output, self.done):
            self.done, text, stack = found.end(), found.group(0), self.stacks[self.alternate]
            if text == b"\x1b[c":
                if self.answers:
                    os.write(master, b"\x1b[?62;22c")
            elif text == b"\x1b[?u":
                if self.answers == "all":
                    os.write(master, b"\x1b[?%du" % (stack[-1] if stack else 0))
            elif text == b"\x1b[?1049$p":
                if self.answers == "all":
                    os.write(master, b"\x1b[?1049;%d$y" % (1 if self.alternate else 2))
            elif text == b"\x1b[<u":
                del stack[-1:]
            elif text == b"\x1b7":
                self.cursor = "overwritten"
            elif found.group(1):
                stack.append(int(found.group(1)))
            else:
                self.alternate = found.group(2) == b"h"


class ForegroundStatusTests(LocalTest):
    """What close() reports, without a terminal: the seat is assembled by hand."""
    def seat(self, **state):
        from sortie_kit.tick_supervise import ForegroundSeat
        from support import FakeChild
        seat = object.__new__(ForegroundSeat)
        seat.child, seat.suspended, seat.closed, seat.outcome, seat.exit = FakeChild(), False, False, "running", None
        seat.child.pid, seat.child.returncode, seat.child.send_signal = 2 ** 22 + 7, None, lambda sig: None
        seat.previous, seat.saved = {}, None
        seat.tty = os.open(os.devnull, os.O_RDWR)  # not a terminal: it cannot be taken back or restored
        vars(seat).update(state)
        return seat

    def test_unknown_exit_status_is_reported_as_unknown_not_as_a_signal(self):
        seat = self.seat()  # no such child: waitpid raises ChildProcessError
        seat.child.code = -1
        self.assertTrue(seat.ended())
        self.assertEqual(seat.close(), "exit status unknown (reaped elsewhere); terminal not restored")
        self.assertEqual(seat.close(), "exit status unknown (reaped elsewhere); terminal not restored")

    def test_surviving_group_is_reported_beside_the_leaders_own_exit(self):
        seat = self.seat(exit="exited on its own with status 0")
        seat.child.code = 0
        seat.child.group_alive = lambda: True
        self.assertEqual(seat.close(), "exited on its own with status 0; its process group was not observed gone"
                                       "; terminal not restored")

    def test_terminal_restoration_failure_is_reported_after_a_confirmed_stop(self):
        seat = self.seat(exit="exited on its own with status 0")
        seat.child.code = 0
        self.assertEqual(seat.close(), "exited on its own with status 0; terminal not restored")


    def test_nothing_is_written_to_what_is_not_a_terminal(self):
        """The modes a full-screen program turns on are turned off by bytes written to the
        terminal. A descriptor that is no terminal gets none of them."""
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        seat = self.seat(exit="exited on its own with status 0")
        os.close(seat.tty)
        seat.tty = writer
        seat.child.code = 0
        self.assertEqual(seat.close(), "exited on its own with status 0; terminal not restored")
        os.set_blocking(reader, False)
        self.assertEqual(os.read(reader, 100), b"")  # the writing end is closed and nothing was written

    def test_taking_back_is_bounded_and_says_when_the_terminal_did_not_come_back(self):
        """Each step is followed by asking again. A terminal that stops answering, one
        whose flags never come back and a difference no step undoes all end with the
        terminal said to be not as it was, after a bounded number of pops."""
        from unittest.mock import patch
        from sortie_kit import tick_supervise
        for answers, pops, back in (
                ([(False, 1), (False, 0)], 1, True),
                ([(True, 3), (False, 1), (False, 0)], 1, True),
                ([(False, 1), None], 1, False),            # silent after the first pop
                ([(False, 1)] * 40, tick_supervise.KEYBOARD_ENTRIES, False),
                ([(False, 1)] * tick_supervise.KEYBOARD_ENTRIES + [(False, 0)], tick_supervise.KEYBOARD_ENTRIES, True),
                ([(True, 0)] * 40, 0, False),               # a screen that is not left is asked to once
                ([(False, 0)], 0, True),
                ([(None, 4)], 0, False),                   # the screen answer is gone: not compared
                ([(False, None)], 0, False)):
            with self.subTest(answers=answers):
                written = []
                with patch.object(tick_supervise, "ask", side_effect=list(answers)), \
                        patch.object(tick_supervise, "put", side_effect=lambda fd, data: written.append(data)):
                    self.assertEqual(tick_supervise.take_back(-1, None, (False, 0)), back)
                self.assertEqual(written.count(tick_supervise.KEYBOARD_POP), pops)
                self.assertEqual(len(written) - pops, sum(1 for item in answers[:1] if item and item[0]))

    def test_an_answer_is_read_for_what_it_says_and_a_mode_the_terminal_does_not_know_is_not_known(self):
        from sortie_kit import tick_supervise
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        import termios
        def settings():
            value = termios.tcgetattr(slave)
            value[3] &= ~getattr(termios, "PENDIN", 0)  # transient on macOS, not a setting
            return value
        saved = settings()
        for reply, expected in ((b"\x1b[?1049;1$y\x1b[?3u\x1b[?62c", (True, 3)), (b"\x1b[?1049;2$y\x1b[?62c", (False, None)),
                                (b"\x1b[?1049;0$y\x1b[?0u\x1b[?1;2c", (None, 0)), (b"x\x1b[?4;1$y", None)):
            with self.subTest(reply=reply):
                os.write(master, reply)
                self.assertEqual(tick_supervise.ask(slave, saved, 0.2), expected)
                self.assertEqual(settings(), saved)
                # The reply was written before the question, so the terminal echoed it first.
                self.assertTrue(os.read(master, 100).endswith(tick_supervise.TERMINAL_ASKED))

    def test_a_terminal_that_hung_up_ends_the_question_at_once(self):
        from sortie_kit import tick_supervise
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, slave)
        saved = termios.tcgetattr(slave)
        os.close(master)
        began = time.monotonic()
        try:
            answer = tick_supervise.ask(slave, saved, 5)
        except (OSError, termios.error):
            answer = None  # a terminal that is gone may also refuse the question itself
        self.assertIsNone(answer)
        self.assertLess(time.monotonic() - began, 2)

    def exiting(self):
        """A driver that took the stop and is still inside its own exit: macOS answers
        the look at its group with a permission error and it cannot be reaped yet."""
        seat = self.seat()
        seat.child.stubborn, seat.ended = True, lambda: False
        def refused():
            raise PermissionError("the leader is exiting")
        seat.child.group_alive = refused
        return seat

    def test_stop_group_notes_a_leader_that_was_exiting_at_its_last_look(self):
        from sortie_kit.tick_supervise import stop_group
        seat, seen = self.exiting(), {}
        self.assertFalse(stop_group(seat.child, 0.05, seen=seen))
        self.assertEqual(seen, {"exiting": True})
        self.assertEqual(seat.child.signals, [])  # nothing can be sent to it, and nothing was
        seat.child.group_alive, seen = (lambda: True), {}
        self.assertFalse(stop_group(seat.child, 0.05, seen=seen))
        self.assertEqual(seen, {"exiting": False})

    def test_a_driver_still_exiting_at_the_bound_is_said_so_and_a_later_look_confirms_it(self):
        seat = self.exiting()
        self.assertEqual(seat.close(), "stop signaled; it was exiting and had not finished when the host stopped "
                                       "waiting; its process group was not observed gone; terminal not restored")
        self.assertEqual((seat.confirmed, seat.group), (False, 2 ** 22 + 7))
        self.assertEqual(seat.recheck(), seat.outcome)  # still exiting: nothing changes
        self.assertFalse(seat.confirmed)
        seat.child.code, seat.child.group_alive = -15, lambda: False
        self.assertEqual(seat.recheck(), "stop signaled; seen ended after the host stopped waiting"
                                         "; terminal not restored")
        self.assertTrue(seat.confirmed)
        self.assertEqual(seat.close(), seat.recheck())

    def test_a_later_look_changes_nothing_about_a_confirmed_stop_or_an_open_seat(self):
        seat = self.seat(exit="exited on its own with status 0")
        seat.child.code = 0
        self.assertEqual(seat.recheck(), "running")  # not closed yet: close() is the one that stops
        said = seat.close()
        self.assertEqual((seat.confirmed, seat.recheck()), (True, said))


    def test_a_refused_first_signal_stays_inside_the_bounded_stop_and_the_later_look(self):
        for refused in ("terminate", "send_signal"):
            with self.subTest(refused=refused):
                seat = self.exiting()
                def refuse(*args):
                    raise PermissionError("the leader is exiting")
                setattr(seat.child, refused, refuse)
                self.assertEqual(seat.close(), "stop signaled; it was exiting and had not finished when the host "
                                 "stopped waiting; its process group was not observed gone; terminal not restored")
                seat.child.code, seat.child.group_alive = -15, lambda: False
                self.assertEqual(seat.recheck(), "stop signaled; seen ended after the host stopped waiting"
                                                 "; terminal not restored")

    def test_a_close_that_failed_still_gets_the_later_look_and_keeps_the_terminal_finding(self):
        from unittest.mock import patch
        seat = self.exiting()
        with patch("sortie_kit.tick_supervise.stop_group", side_effect=OSError("no such look")), \
                self.assertRaises(OSError):
            seat.close()
        self.assertEqual((seat.confirmed, seat.unrestored), (None, True))
        self.assertEqual(seat.recheck(), "stop attempted; exit not observed; terminal not restored")
        seat.child.code, seat.child.group_alive = -15, lambda: False
        self.assertEqual(seat.recheck(), "stop signaled; seen ended after the host stopped waiting"
                                         "; terminal not restored")


class OwnTerminalTests(LocalTest):
    def test_an_interrupted_check_closes_the_descriptor_it_opened(self):
        from unittest.mock import patch
        from sortie_kit import tick_supervise
        opened, real = [], os.open
        def opening(*args, **kwargs):
            opened.append(real(os.devnull, os.O_RDWR))
            return opened[-1]
        with patch.object(tick_supervise.os, "ttyname", return_value="/dev/ttys-fixture"), \
                patch.object(tick_supervise.os, "open", side_effect=opening), \
                patch.object(tick_supervise.os, "tcgetpgrp", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            tick_supervise.own_terminal()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_streams_known_only_by_the_alias_are_refused_by_name(self):
        from unittest.mock import patch
        from sortie_kit import tick_supervise
        from sortie_kit.layout import Refused
        with patch.object(tick_supervise.os, "ttyname", return_value="/dev/tty"), \
                patch.object(tick_supervise.os, "open", side_effect=AssertionError("the alias is never opened")), \
                self.assertRaisesRegex(Refused, "own device name"):
            tick_supervise.own_terminal()


class ForegroundTests(LocalTest):
    def run_host(self, mode, typed=b"", answers="all"):
        self.terminal = Terminal(answers)
        out = self.root / "host.json"
        script = self.root / "host.py"
        script.write_text(HOST)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        pid, master = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-B", str(script), str(out), mode, CHILDREN[mode]], env)
        output, status, began = b"", None, time.monotonic()
        try:
            while time.monotonic() - began < 15:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        pass
                    self.terminal.feed(output, master)
                if typed and b"driver ready" in output:
                    os.write(master, typed)
                    typed = b""
                reaped, observed = os.waitpid(pid, os.WNOHANG)
                if reaped:
                    status = observed
                    break
            self.assertIsNotNone(status, "host fixture did not finish: " + output.decode(errors="replace"))
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors="replace"))
        finally:
            if status is None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(master)
        self.assertIn(b"host writes after reclaim", output)
        self.output = output
        return json.loads(out.read_text()), json.loads((self.root / "host.json.child").read_text())

    def check_handoff(self, host, child):
        self.assertEqual(child, dict(child, foreground=True, leader=True, ttou=True, ttin=True, tty=True,
                                     device=[False] * 3, watchable=["ok"] * 3))
        self.assertEqual((host["host_foreground"], host["child_group"]), (False, True))
        self.assertEqual((host["ended"] or host["outcome"].startswith("stopped"), host["reclaimed"],
                          host["attributes"], host["handlers"]), (True, True, True, True))
        self.assertEqual(host["again"], host["outcome"])
        # After the driver, and before anything the host writes: the modes a full-screen
        # program turns on and a program stopped by a signal leaves on. Once: the second
        # close() writes nothing.
        from sortie_kit.tick_supervise import TERMINAL_MODES_OFF
        for mode in (b"\x1b[?1000l", b"\x1b[?1002l", b"\x1b[?1003l", b"\x1b[?1006l", b"\x1b[?1004l", b"\x1b[?2004l",
                     b"\x1b[?25h"):
            self.assertIn(mode, TERMINAL_MODES_OFF)
        # Never blind: what the caller may hold itself. A pop takes an entry of the
        # keyboard stack whoever pushed it, and saving the cursor overwrites the one saved.
        for mode in (b"\x1b[<u", b"\x1b[?1049l", b"\x1b7"):
            self.assertNotIn(mode, TERMINAL_MODES_OFF)
        self.assertEqual((self.terminal.cursor, self.terminal.alternate, self.terminal.stacks[False]),
                         ("the caller's", False, [5]))
        self.assertEqual(self.output.count(TERMINAL_MODES_OFF), 1)
        self.assertLess(self.output.index(b"driver ready"), self.output.index(TERMINAL_MODES_OFF))
        self.assertLess(self.output.index(TERMINAL_MODES_OFF), self.output.index(b"host writes after reclaim"))

    def test_driver_owns_the_terminal_reads_from_it_and_its_exit_is_observed(self):
        host, child = self.run_host("exit", typed=b"hello")
        self.check_handoff(host, child)
        self.assertEqual(child["read"], "hello")
        self.assertEqual((host["ended"], host["code"], host["outcome"]), (True, 7, "exited on its own with status 7"))

    def test_host_stop_ends_a_running_driver_and_restores_the_terminal(self):
        host, child = self.run_host("stop")
        self.check_handoff(host, child)
        self.assertEqual((host["ended"], host["code"], host["outcome"]), (False, -15, "stopped by the host"))

    def test_a_driver_that_left_the_terminal_as_it_was_costs_the_caller_nothing(self):
        """The caller's own entry on the keyboard stack and its saved cursor are the
        caller's. After a driver that changed neither, on its own exit and on the
        host's stop, the host writes no pop, does not leave an alternate screen and
        saves no cursor."""
        for mode in ("exit", "stop"):
            with self.subTest(mode=mode):
                host, child = self.run_host(mode, typed=b"hello" if mode == "exit" else b"")
                self.check_handoff(host, child)
                for never in (b"\x1b[<u", b"\x1b[?1049l", b"\x1b7"):
                    self.assertNotIn(never, self.output)

    def test_what_a_stopped_driver_left_on_is_taken_back_and_no_more(self):
        """The host asks the terminal before the driver and after it and puts back what
        differs: it leaves the alternate screen the driver entered and pops the one
        entry the driver pushed on the main screen's keyboard stack. The caller's entry
        under it stays."""
        host, child = self.run_host("modes")
        self.check_handoff(host, child)
        self.assertEqual(host["outcome"], "stopped by the host")
        self.assertEqual((self.output.count(b"\x1b[?1049l"), self.output.count(b"\x1b[<u")), (1, 1))
        self.assertEqual(self.terminal.stacks, {False: [5], True: [3]})
        self.assertNotIn(b"\x1b7", self.output)

    def test_a_terminal_that_does_not_say_what_it_holds_gets_only_the_plain_modes_turned_off(self):
        """Without an answer the host cannot tell the driver's entry from the caller's,
        so it pops nothing and leaves no screen. A terminal that answers only what it
        is, and one that answers nothing."""
        from sortie_kit import tick_supervise
        from sortie_kit.tick_supervise import TERMINAL_MODES_OFF
        for answers in ("", "identity"):
            with self.subTest(answers=answers):
                host, child = self.run_host("modes", answers=answers)
                self.assertEqual((host["outcome"], host["reclaimed"], host["attributes"]),
                                 ("stopped by the host", True, True))
                self.assertEqual(self.output.count(TERMINAL_MODES_OFF), 1)
                for never in (b"\x1b[<u", b"\x1b[?1049l", b"\x1b7"):
                    self.assertNotIn(never, self.output)
                # Asked before the driver, and with nothing to compare not again after it.
                self.assertEqual(self.output.count(tick_supervise.TERMINAL_ASKED), 1)

    def test_suspended_driver_counts_as_ended_and_is_stopped(self):
        host, child = self.run_host("suspend")
        self.check_handoff(host, child)
        self.assertTrue(host["ended"])
        self.assertEqual(host["outcome"], "suspended from the terminal; stopped by the host")
        self.assertIn(host["code"], (-15, -9))
