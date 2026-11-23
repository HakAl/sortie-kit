"""Foreground-only supervisor; owned-child pattern copied from upstream run mode."""
from __future__ import annotations
import subprocess
import math
import os
import signal
import termios
import threading
import time

from .bounds import duration
from .layout import Refused


class OwnedChild(subprocess.Popen):
    """A toolkit child and its inherited process group, never a pane agent.

    Only spawn() constructs this handle, with a fresh group whose ID is the
    child's PID. Existing developer processes cannot belong to that new group.
    Descendants must retain the group; this is not containment for programs
    that deliberately escape it by creating another group/session.
    """
    def _signal_group(self, sig):
        try:
            os.killpg(self.pid, sig)
        except PermissionError:
            # macOS can report EPERM for a group consisting of an unreaped
            # exited leader. Reap it and retry once. A persistent denial is
            # still an error, never evidence that descendants have stopped.
            self.poll()
            os.killpg(self.pid, sig)

    def group_alive(self):
        self.poll()
        try:
            self._signal_group(0)
            return True
        except ProcessLookupError:
            return False

    def send_signal(self, sig):
        # Unlike Popen.send_signal, do not skip because the leader has exited:
        # its transport descendants can still be executing in the owned group.
        try:
            self._signal_group(sig)
        except ProcessLookupError:
            pass


def spawn(argv: list, env=None, pass_fds=(), cwd=None) -> subprocess.Popen:
    """Keep a foreground owner and wait handle for an isolated process group.

    process_group=0 sets the group before exec without starting a new session
    or daemon. Transport subprocesses inherit it. No saved PID or pane ID is
    used to decide which group to signal. Toolkit children have no terminal
    input; the developer's agent CLIs remain outside this group.
    """
    return OwnedChild(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                      stderr=subprocess.STDOUT, env=env, pass_fds=pass_fds, cwd=cwd,
                      process_group=0)


def stop_group(child, seconds=1.0, *, clock=time.monotonic, sleep=time.sleep, seen=None):
    """Stop one owned group within a fixed bound and reap its leader.

    Signals the group even when the leader already exited, because descendants
    may still run in it: a stop request first, a kill after half the bound.
    Returns False when the group was still observed, or its leader not reaped,
    at the end of the bound; that is never evidence of cessation.

    `seen`, a dict, receives `exiting`: whether the last look at the group was
    refused. macOS answers that way while the leader is inside its own exit: it
    has stopped running, cannot be signaled and cannot be reaped yet. A pass that
    is refused sends nothing, and there is nothing it could send.
    """
    alive = getattr(child, "group_alive", None) or (lambda: child.poll() is None)
    end = clock() + seconds
    grace = end - seconds / 2
    asked = killed = gone = exiting = False
    while True:
        try:
            exiting = False
            if not alive():
                gone = True
                break
            if not asked:
                child.terminate()
                asked = True
            elif not killed and clock() >= grace:
                child.kill()
                killed = True
        except PermissionError:
            exiting = True  # macOS answers this for a group whose leader is exiting; look again
        if clock() >= end:
            break
        sleep(min(0.02, max(0.001, end - clock())))
    while child.poll() is None and clock() < end:
        sleep(min(0.02, max(0.001, end - clock())))
    if seen is not None:
        seen["exiting"] = exiting
    return gone and child.poll() is not None


def own_terminal():
    """Open this process's controlling terminal by its own device name.

    The driver gets this descriptor as its input and output. `/dev/tty` names the
    same terminal but is an alias device: macOS refuses it in kqueue (EINVAL),
    and agent CLIs watch their terminal that way, so a driver handed the alias
    dies at startup. The name comes from the command's own standard streams,
    which `sortie start` already requires to be the terminal.
    """
    for stream in (0, 1, 2):
        try:
            name = os.ttyname(stream)
        except OSError:
            continue
        if name == "/dev/tty":
            continue
        try:
            fd = os.open(name, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError:
            continue
        keep = False
        try:
            os.set_blocking(fd, True)
            os.tcgetpgrp(fd)  # only this process's controlling terminal answers
            keep = os.ttyname(fd) != "/dev/tty"
            if keep:
                return fd
        except OSError:
            pass
        finally:
            # Also on an interruption: the descriptor is the caller's only once returned.
            if not keep:
                os.close(fd)
    raise Refused("starting the driver needs the terminal's own device name, and this command's input and "
                  "output do not give one (redirected, or opened through /dev/tty); run it directly in a terminal")


class ForegroundSeat:
    """The interactive driver: an owned process group in the foreground of the
    developer's terminal.

    The driver gets its own group, so a terminal Ctrl-C reaches it and not the
    host, and the host's stop reaches it and nothing else. The host hands the
    terminal over, writes nothing to it while the driver runs, and in close()
    stops the group, takes the terminal back and restores its settings. A driver
    suspended from the terminal (Ctrl-Z) counts as ended: the host cannot resume
    an interactive program on the developer's behalf.
    """
    def __init__(self, argv, *, cwd=None, env=None):
        if threading.current_thread() is not threading.main_thread():
            raise Refused("starting the driver requires the foreground main thread")
        self.child, self.suspended, self.closed, self.outcome = None, False, False, "not started"
        self.exit = None  # how the driver process ended by itself, once observed
        self.previous, self.saved = {}, None
        self.tty = own_terminal()
        try:
            if os.tcgetpgrp(self.tty) != os.getpgrp():
                raise Refused("starting the driver requires the foreground terminal")
            self.saved = termios.tcgetattr(self.tty)
            # A background group that touches the terminal is stopped by these
            # signals; the host is in the background while the driver runs.
            for sig in (signal.SIGTTOU, signal.SIGTTIN):
                self.previous[sig] = signal.signal(sig, signal.SIG_IGN)

            def foreground():
                # In the child, before exec: same steps as the parent takes below,
                # so neither order of scheduling leaves the driver in the background.
                os.setpgid(0, 0)
                os.tcsetpgrp(0, os.getpgrp())
                signal.signal(signal.SIGTTOU, signal.SIG_DFL)
                signal.signal(signal.SIGTTIN, signal.SIG_DFL)

            self.child = OwnedChild(argv, stdin=self.tty, stdout=self.tty, stderr=self.tty,
                                    cwd=cwd, env=env, preexec_fn=foreground)
            self.outcome = "running"
            for step in (lambda: os.setpgid(self.child.pid, self.child.pid),
                         lambda: os.tcsetpgrp(self.tty, self.child.pid)):
                try:
                    step()
                except OSError:
                    pass  # the child already did it, or already exited
        except BaseException:
            self.close()
            raise

    def ended(self):
        child = self.child
        if child is None or self.exit is not None or self.suspended:
            return True
        try:
            pid, status = os.waitpid(child.pid, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            child.returncode = -1  # only so Popen stops waiting; the real status is unknown
            self.exit = "exit status unknown (reaped elsewhere)"
            return True
        if pid == 0:
            return False
        if os.WIFSTOPPED(status):
            self.suspended = True
            return True
        code = child.returncode = os.waitstatus_to_exitcode(status)
        self.exit = ("exited on its own with status " + str(code) if code >= 0
                     else "ended on its own by signal " + str(-code))
        return True

    def close(self):
        """Stop the driver group, take the terminal back, restore it. Returns what
        was observed: how the driver process itself ended, and separately whether
        its process group was seen gone. Process exit is never seat cessation."""
        if self.closed:
            return self.outcome
        self.closed = True
        try:
            child = self.child
            if child is not None:
                self.outcome = "stop attempted; exit not observed"  # stands if anything below raises
                self.ended()
                own = self.exit
                if own is None:
                    # A suspended group cannot act on the stop, hence the second signal.
                    for send in (child.terminate, lambda: child.send_signal(signal.SIGCONT)):
                        try:
                            send()
                        except PermissionError:
                            pass  # refused, as for a leader that is exiting: the bounded stop below looks again
                seen = {}
                gone = stop_group(child, 2.0, seen=seen)
                leader = own or (("suspended from the terminal; " if self.suspended else "")
                                 + ("stopped by the host" if child.poll() is not None
                                    else "stop signaled; it was exiting and had not finished when the host "
                                         "stopped waiting" if seen.get("exiting")
                                    else "stop signaled; exit not observed"))
                self.outcome = leader if gone else leader + "; its process group was not observed gone"
                self.confirmed = bool(gone)
        finally:
            try:
                os.tcsetpgrp(self.tty, os.getpgrp())
                if self.saved is not None:
                    termios.tcsetattr(self.tty, termios.TCSANOW, self.saved)
            except (OSError, termios.error):
                # A hung-up terminal is gone and cannot be restored; say that it was not.
                self.unrestored = True
                self.outcome += "; terminal not restored"
            finally:
                for sig, handler in self.previous.items():
                    signal.signal(sig, handler)
                os.close(self.tty)
        return self.outcome

    def recheck(self):
        """One more look, with no wait and no signal, at a stop that close() could not
        confirm. A process that was exiting when the bound ran out is usually gone a
        moment later; the host looks again before it writes its report. Returns what
        is known now."""
        child = self.child
        if not self.closed or child is None or self.confirmed:
            return self.outcome  # also looked at again when close() failed before it could tell
        try:
            if child.poll() is None or child.group_alive():
                return self.outcome
        except OSError:
            return self.outcome  # still exiting, or the look failed: nothing new is known
        self.confirmed = True
        self.outcome = (self.exit or "stop signaled") + ("; seen ended after the host stopped waiting"
                                                         + ("; terminal not restored" if self.unrestored else ""))
        return self.outcome

    unrestored = False  # whether close() failed to give the terminal back as it was
    confirmed = None  # after close(): whether the process and its group were seen gone

    @property
    def group(self):
        """The driver's process group id, for whoever has to look for what is left of it."""
        return None if self.child is None else self.child.pid


class Supervisor:
    """Cut of foreground run ownership, bounded restart, and owned-child stop.

    No service installation, recovery seat, or detached process. The child also
    receives a finite lifetime, so parent death cannot leave an unbounded tick.
    """
    def __init__(self, budget, sources, *, authorize, spawn=spawn,
                 sleep=time.sleep, poll=0.1, floor=1.0, cap=30.0):
        if any(not math.isfinite(n) or n <= 0 for n in (poll, floor, cap)) or cap < floor:
            raise ValueError("invalid supervisor intervals")
        self.budget, self.sources, self.authorize = budget, sources, authorize
        self.spawn, self.sleep, self.poll = spawn, sleep, poll
        self.floor, self.cap = floor, cap
        self.child = None
        self._cleaned_child = None
        self._cleanup_confirmed = True
        self.interrupted = False
        self.launches = 0
        self.cleanup = "no child"

    def guard(self):
        return ("interrupted" if self.interrupted else "") or self.budget.guard() or self.sources.guard() or self.authorize()

    def pause(self, seconds):
        end = self.budget.clock() + min(seconds, self.budget.remaining())
        while self.budget.clock() < end and not self.guard():
            self.sleep(min(self.poll, end - self.budget.clock(), self.budget.remaining()))

    def stop_child(self):
        """Stop the owned group even if its leader already exited; reap leader."""
        child = self.child
        if child is None:
            return True
        if child is self._cleaned_child:
            return self._cleanup_confirmed
        group_alive = getattr(child, "group_alive", None)
        alive = group_alive or (lambda: child.poll() is None)
        # Local process cleanup only: at most one second, no transport or restart.
        end = self.budget.clock() + 1.0
        grace_end = min(end, self.budget.clock() + min(0.5, self.budget.remaining()))

        refusal = []  # the last refusal met in this stop

        def left():
            return end - self.budget.clock()

        def within(action):
            # macOS refuses a look at, or a signal to, a group whose leader is inside
            # its own exit: it can be neither signaled nor reaped until that is over,
            # a matter of milliseconds. Repeat within the same bound. Once a refusal
            # was met, nothing is tried at or after the end of the bound, by this call
            # or a later one: the refusal is raised, never taken as evidence of
            # cessation, and the wait between two tries is never more than is left.
            while True:
                if refusal and left() <= 0:
                    raise refusal[-1]
                try:
                    return action()
                except PermissionError as exc:
                    refusal[:] = [exc]
                remaining = left()  # read once: the clock goes on between two readings
                if remaining <= 0:
                    raise refusal[-1]
                self.sleep(min(0.005, remaining))

        if within(alive):
            within(child.terminate)
        while within(alive) and self.budget.clock() < grace_end:
            child.poll()  # Reap the leader, but do not mistake it for its group.
            self.sleep(max(0, min(0.05, grace_end - self.budget.clock())))
        if within(alive):
            within(child.kill)
        try:
            if child.poll() is None:
                remaining = left()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("owned child", 0)
                child.wait(timeout=remaining)
            # The leader is reaped. Its group is looked at once more, for what is left of
            # the second: a descendant that was signaled can still be inside its exit.
            present = bool(group_alive) and within(alive)
            while present and (remaining := left()) > 0:
                self.sleep(min(0.005, remaining))
                if left() > 0:
                    present = within(alive)
            if present:
                self.cleanup = "owned group still present after its stop"
                self._cleanup_confirmed = False
            else:
                self.cleanup = ("owned group stop signaled; child reaped" if group_alive
                                else "child stopped and reaped")
                self._cleanup_confirmed = True
        except subprocess.TimeoutExpired:
            self.cleanup = "child reap unconfirmed"
            self._cleanup_confirmed = False
        self._cleaned_child = child
        return self._cleanup_confirmed

    def run(self, argv, *, env=None, cwd=None):
        if not argv or "--for" in argv or any(a.startswith("--for=") for a in argv):
            raise ValueError("supervisor owns the child's --for argument")
        previous = {}
        if threading.current_thread() is threading.main_thread():
            # A terminal hangup reaches the owner, not its separate child group.
            # Keep ownership until the same bounded finally cleanup has run.
            for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, self._interrupt)
        delay = self.floor
        refused = False  # the bounded stop was refused to its end; it is not gone through twice
        try:
            while True:
                if why := self.guard():
                    return (0 if why == "duration elapsed" else 3), why
                # Include the precise remaining lifetime at EVERY restart.
                command = list(argv) + ["--for", str(self.budget.remaining())]
                self.child = self.spawn(command, env=env, cwd=cwd)
                self.launches += 1
                while self.child.poll() is None:
                    if why := self.guard():
                        return (0 if why == "duration elapsed" else 3), why
                    self.pause(self.poll)
                code = self.child.returncode
                # Drain descendants on ALL exits, including before a restart.
                try:
                    stopped = self.stop_child()
                except PermissionError:
                    refused = True
                    raise
                if not stopped:
                    return 3, "owned child cleanup unconfirmed"
                if code in (0, 3):
                    return code, "child completed" if code == 0 else "child refused"
                self.pause(delay)
                delay = min(self.cap, delay * 2)
        except KeyboardInterrupt:
            return 130, "interrupted"
        finally:
            try:
                if not refused:
                    self.stop_child()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)

    def _interrupt(self, signum, frame):
        self.interrupted = True


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--for", dest="seconds", type=duration, required=True)
    parser.parse_args(argv)
    parser.exit(3, "live supervisor admission is unavailable in this slice\n")


if __name__ == "__main__":
    main()
