"""Hold the machine awake against idle sleep while a shift runs.

A running shift is the whole reason: there is no predicate. The hold stops idle
system sleep only. Closing a laptop lid or choosing Sleep by hand still sleeps
the machine, and the signed window keeps running on the wall clock.

macOS: one power assertion in the host's own process (IOKit through ctypes),
created with a timeout and renewed from the host's loop, so that the system
drops it by itself when the host dies or hangs. Linux: a `systemd-inhibit`
child when that program is installed, held through a pipe the host keeps open,
so that it ends when the host does. Anywhere else there is no hold, and that is
said. Nothing here ever stops a shift: a refusal is recorded with its return
code and shown in the report.

How old a hold is, is measured on a clock that goes on while the machine sleeps
(`continuing`): Python's monotonic clock pauses in sleep on macOS, which is the
very thing to be seen, and the wall clock can be set. The wall clock gives only
the times written into the record.

No call into the system is waited on for more than PATIENCE seconds, and before
the record says the machine is held, the system is asked whether it is; that
question is waited on for ASK seconds, since the host keeps that much beside
the time for its report. The answer is yes only for an assertion the system
says is in force: it keeps the properties of one it has suspended, switched off
or timed out. When it gives nothing, the assertion is gone or the system was
not reached, which cannot be told apart: that is not known, and the id is kept
so that it is still let go. The host renews only on a pass of its loop that goes
on, and tells the renewal how much working time is left: with less than its
calls can take, the renewal is not begun. So a stop waits on nothing here until
the report is written.
"""
import datetime as dt
import os
import shutil
import subprocess
import sys
import threading
import time

RENEW = 30.0  # seconds between renewals of the macOS assertion
TIMEOUT = 120.0  # after which the system drops an assertion that was not renewed; at least twice RENEW
PATIENCE = 2.0  # seconds one call into the system is waited on while the shift runs, and for the last release
ASK = 0.5  # seconds the question for the record is waited on: it is asked while the closeout is written
CONFIRM = 10.0  # seconds the Linux child has to say it holds the lock before that is a refusal
KEPT = 8  # refusals and gaps kept in the record; all are counted
REASON = "sortie-kit: a shift is running"
LEVEL_ON = 255  # kIOPMAssertionLevelOn
ALREADY_RELEASED = -0x1FFFFD3E  # kIOReturnBadArgument: the id is no longer held, its timeout came first
FAILED = (OSError, ValueError, AttributeError, TypeError, ImportError, subprocess.SubprocessError)
MEANS = {"mac": "a macOS power assertion against idle sleep (PreventUserIdleSystemSleep), held by the host process",
         "inhibit": "systemd-inhibit --what=idle (the login manager's idle handling), run by the host for as long as the shift"}


def continuing():
    """Seconds on a clock that goes on while the machine sleeps, where there is one.
    macOS: CLOCK_MONOTONIC does (`man clock_gettime`), unlike time.monotonic().
    Linux: CLOCK_BOOTTIME. Elsewhere nothing is held and the wall clock will do."""
    for name in ("CLOCK_BOOTTIME",) if sys.platform.startswith("linux") else ("CLOCK_MONOTONIC",) if sys.platform == "darwin" else ():
        try:
            return time.clock_gettime(getattr(time, name))
        except (AttributeError, OSError):
            pass
    return time.time()


def mac_libraries():
    """CoreFoundation and IOKit, loaded when first wanted and never at import."""
    import ctypes
    import ctypes.util
    found = [ctypes.util.find_library(name) for name in ("CoreFoundation", "IOKit")]
    if not all(found):
        raise OSError("CoreFoundation or IOKit was not found")  # CDLL(None) would open this process itself
    return ctypes.CDLL(found[0]), ctypes.CDLL(found[1])


def start_inhibitor(argv, **options):
    """The Linux child. Every start that was handed no other starter goes through here."""
    return subprocess.Popen(argv, **options)


def mac_system(load=None):
    """The two IOKit calls. `load` gives the two libraries; left out, the real ones."""
    import ctypes
    cf, iokit = (mac_libraries if load is None else load)()
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    iokit.IOPMAssertionCreateWithDescription.restype = ctypes.c_int32
    iokit.IOPMAssertionCreateWithDescription.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_double, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    iokit.IOPMAssertionRelease.restype = ctypes.c_int32
    iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
    iokit.IOPMAssertionCopyProperties.restype = ctypes.c_void_p
    iokit.IOPMAssertionCopyProperties.argtypes = [ctypes.c_uint32]
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFDictionaryGetValue.restype = ctypes.c_void_p  # not retained: it lives as long as the dictionary
    cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFNumberGetValue.restype = ctypes.c_ubyte
    cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
    cf.CFBooleanGetValue.restype = ctypes.c_ubyte
    cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    cf.CFGetTypeID.restype = cf.CFNumberGetTypeID.restype = cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
    cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    cf.CFNumberGetTypeID.argtypes = cf.CFBooleanGetTypeID.argtypes = []
    text = lambda value: cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)  # UTF-8
    # Made once for the life of the process and never freed.
    kind, name, action = text("PreventUserIdleSystemSleep"), text(REASON), text("TimeoutActionRelease")
    # The properties that say whether an assertion is in force: its level, and the marks
    # the system puts on one it has suspended or timed out.
    level, suspended, timed_out = text("AssertLevel"), text("AssertionIsStateSuspended"), text("AssertTimedOutWhen")
    if not (kind and name and action and level and suspended and timed_out):
        raise OSError("CoreFoundation gave no string")

    class System:
        @staticmethod
        def create(timeout):
            """(return code, assertion id). Zero is success."""
            identity = ctypes.c_uint32(0)
            try:
                code = iokit.IOPMAssertionCreateWithDescription(
                    kind, name, None, None, None, ctypes.c_double(timeout), action, ctypes.byref(identity))
            except ctypes.ArgumentError as exc:  # not an OSError by itself; a refusal like any other
                raise OSError("ctypes refused the arguments: " + str(exc)) from exc
            return code, identity.value

        @staticmethod
        def release(identity):
            try:
                return iokit.IOPMAssertionRelease(ctypes.c_uint32(identity))
            except ctypes.ArgumentError as exc:
                raise OSError("ctypes refused the arguments: " + str(exc)) from exc

        @staticmethod
        def alive(identity):
            """Whether this assertion is in force now. True: the system has it, at level on,
            neither suspended nor timed out. False: the system has it and it is not in
            force. None: not known. The system gave nothing, which it does for an id it
            no longer has and also when it could not be reached, or gave no level."""
            try:
                properties = iokit.IOPMAssertionCopyProperties(ctypes.c_uint32(identity))
                if not properties:
                    return None
                try:
                    on, number = ctypes.c_int32(-1), cf.CFDictionaryGetValue(properties, level)
                    if (not number or cf.CFGetTypeID(number) != cf.CFNumberGetTypeID()
                            or not cf.CFNumberGetValue(number, 3, ctypes.byref(on))):  # 3: a 32 bit integer
                        return None
                    mark = cf.CFDictionaryGetValue(properties, suspended)
                    if mark and cf.CFGetTypeID(mark) != cf.CFBooleanGetTypeID():
                        return None  # a mark that is no yes or no: not known
                    return bool(on.value == LEVEL_ON and not cf.CFDictionaryGetValue(properties, timed_out)
                                and not (mark and cf.CFBooleanGetValue(mark)))
                finally:
                    cf.CFRelease(properties)
            except ctypes.ArgumentError as exc:
                raise OSError("ctypes refused the arguments: " + str(exc)) from exc

    return System


def stamp(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()


class Wake:
    """One hold for one shift: `acquire` once, `renew` from the host's loop, `release`
    once the closeout is written. `record` is what the closeout keeps. No method
    raises for anything the system does or refuses."""

    def __init__(self, enabled=True, *, platform=None, system=None, wall=time.time, clock=continuing,
                 which=shutil.which, popen=None, patience=PATIENCE):
        self.platform = sys.platform if platform is None else platform
        self.system, self.wall, self.clock, self.popen, self.patience = system, wall, clock, popen, patience
        self.pending = None  # a call into the system that did not return in time and may still run
        self.program = None
        if not enabled:
            self.kind, self.means = "off", "off: --no-wake was given"
        elif self.platform == "darwin":
            self.kind = "mac"
        elif self.platform.startswith("linux"):
            self.program = which("systemd-inhibit")
            self.kind, self.means = ("inhibit", None) if self.program else ("none", "none: systemd-inhibit is not installed")
        else:
            self.kind, self.means = "none", "none: sortie-kit has no means on this platform (" + self.platform[:40] + ")"
        if self.kind in MEANS:
            self.means = MEANS[self.kind]
        self.identity, self.child = None, None  # the macOS assertion held now; the Linux child
        self.confirmed, self.started, self.overdue = False, None, False  # the child said it holds the lock; its start
        self.gapped = None  # the renewal time whose lapse is already counted
        self.told = None  # the assertion and the answer (not in force, not known) already recorded for it
        # `first` and `renewed` are wall times, for the record. `last` and `tried` are on
        # the continuing clock: the last renewal and the last attempt.
        self.first, self.renewed, self.last, self.tried = None, None, None, None
        self.began = self.done = False
        self.clean = True  # nothing was refused and nothing lapsed since the first try
        self.refusals, self.gaps, self.refused, self.lapsed = [], [], 0, 0

    def plan(self):
        """One line of ASCII for the signer: whether this run will hold the machine awake, and how."""
        if self.kind == "off":
            return "Keep awake: off (--no-wake). The machine may idle-sleep while the signed window runs on."
        if self.kind == "none":
            return ("Keep awake: " + self.means + ". The machine may idle-sleep while the signed window runs on.")
        return ("Keep awake: the host will stop idle sleep while the shift runs, through " + self.means
                + ". A closed lid or Sleep chosen by hand still sleeps the machine. --no-wake turns this off.")

    def refuse(self, what, code):
        self.clean = False
        self.refused += 1
        if len(self.refusals) < KEPT:
            self.refusals.append(dict(at=stamp(self.wall()), what=str(what)[:200], code=code if type(code) is int else None))

    def call(self, function, *arguments, late=None, patience=None):
        """One call into the system, waited on for `patience` seconds and no longer. A call
        that has not returned by then is left to itself, and until it has, no other is
        made: each raises at once. `late` is given the value of a call that came back
        after it was given up."""
        if self.pending is not None and self.pending.is_alive():
            raise OSError("an earlier call into the system has not returned")
        box, given_up, one = [], [], threading.Lock()  # under `one`: either the value is taken or it is given up

        def run():
            try:
                value = (True, function(*arguments))
            except BaseException as exc:  # handed to the caller, which decides
                value = (False, exc)
            with one:
                box.append(value)
                left = bool(given_up)
            if left and value[0] and late is not None:
                try:
                    late(value[1])
                except FAILED:
                    pass
        thread = threading.Thread(target=run, name="sortie-kit wake", daemon=True)
        try:
            thread.start()
        except RuntimeError as exc:  # no thread to be had: a refusal like any other
            raise OSError("no thread for the call into the system: " + str(exc)) from exc
        patience = self.patience if patience is None else min(patience, self.patience)
        try:
            thread.join(patience)
        finally:  # also when the wait itself is broken off: the call may still run
            with one:
                if not box:
                    given_up.append(True)
                    self.pending = thread
        if given_up:
            raise OSError("the call into the system did not return within %gs" % patience)
        self.pending = None
        if box[0][0]:
            return box[0][1]
        raise box[0][1]

    def loaded(self):
        if self.system is None:
            self.system = mac_system()
        return self.system

    def orphan(self, made):
        """An assertion whose making came back too late to be kept: let it go at once."""
        if made[0] == 0:
            self.system.release(made[1])

    def take(self, now=None):
        """Create a new hold. True when one was made; the one held before is untouched.
        `now` is the moment the caller judged the age of the old one at: one reading."""
        now = self.tried = self.clock() if now is None else now
        try:
            if self.kind == "mac":
                if self.system is None:
                    self.call(self.loaded)
                code, identity = self.call(self.system.create, TIMEOUT, late=self.orphan)
                if code != 0:
                    self.refuse("create", code)
                    return False
                old, self.identity = self.identity, identity
                if old is not None and old != identity:
                    self.drop(old, now)  # only now: the new one is held first
                elif old is not None and now - self.last <= TIMEOUT:
                    # The id just made is the old one's: the system had let that one go before
                    # its timeout. Releasing "the old one" now would let go of the new.
                    self.refuse("release: the system no longer held it, before its timeout", None)
            else:
                # The command says `1` once it runs, which is once systemd-inhibit holds the
                # lock: a program that started has not yet asked the system for anything.
                self.confirmed, self.started, self.overdue = False, now, False
                child = (start_inhibitor if self.popen is None else self.popen)(
                    [self.program, "--what=idle", "--who=sortie-kit", "--why=" + REASON, "--mode=block",
                     "/bin/sh", "-c", "printf 1; exec cat"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                    start_new_session=True)  # a signal to the terminal's group is not what ends it
                try:
                    os.set_blocking(child.stdout.fileno(), False)  # `look` must never wait on it
                except FAILED:
                    self.end(child)
                    raise
                self.child = child
                return True  # started; `look` sets the times when it is confirmed
        except FAILED as exc:
            self.refuse(("create: " if self.kind == "mac" else "systemd-inhibit not started: ")
                        + type(exc).__name__ + ": " + str(exc), None)
            return False
        self.mark(now)
        return True

    def mark(self, now):
        """A hold was made or confirmed at `now` on the continuing clock."""
        self.last, self.renewed = now, self.wall()
        if self.first is None:
            self.first = self.renewed

    def drop(self, identity, now):
        """Release one assertion. `self.last` is still the time it was made or last renewed."""
        try:
            code = self.call(self.system.release, identity)
            if code == ALREADY_RELEASED and now - self.last <= TIMEOUT:  # a clock set back counts as early
                # Its timeout had not come, so nothing of sortie-kit's let it go: it was not
                # held for some time before this, and how long is not known.
                self.refuse("release: the system no longer held it, before its timeout", code)
            elif code not in (0, ALREADY_RELEASED):
                self.refuse("release", code)
        except FAILED as exc:
            self.refuse("release: " + type(exc).__name__ + ": " + str(exc), None)

    def end(self, child):
        """Stop the Linux child and close the host's ends of its pipes. May raise what FAILED names."""
        try:
            child.stdin.close()  # `cat` ends at end of input, and the inhibitor with it
        except FAILED as exc:
            self.refuse("systemd-inhibit input not closed: " + type(exc).__name__ + ": " + str(exc), None)
        try:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=1)
        finally:
            try:
                child.stdout.close()
            except FAILED:
                pass

    def acquire(self):
        if self.kind in ("mac", "inhibit") and not self.began:
            self.began = True
            self.take()

    def renew(self, limit=None):
        """Called on every pass of the host's loop; does something only when it is time.
        `limit` is the working time the host has left, in seconds. A renewal is two calls
        into the system, the new hold and the release of the old, each waited on for up
        to `patience`: with less than that left it is not begun, so that it cannot run
        past the moment the seats are stopped. That is no refusal: what was renewed
        last is held for TIMEOUT from then, far longer than the stop and the report take."""
        if self.kind not in ("mac", "inhibit") or not self.began or self.done:
            return
        now = self.clock()
        if self.kind == "inhibit":
            self.look()  # never waits
            return
        if self.tried is not None and 0 <= now - self.tried < RENEW:
            return
        if limit is not None and not limit >= 2 * self.patience:
            return
        if self.identity is not None and now - self.last > TIMEOUT and self.gapped != self.last:
            # The system dropped the assertion before this renewal: the host was held up,
            # or the machine slept anyway (a closed lid, Sleep chosen by hand). Counted
            # once, however many renewals fail after it.
            self.gapped = self.last
            self.clean = False
            self.lapsed += 1
            if len(self.gaps) < KEPT:
                self.gaps.append({"from": stamp(self.renewed), "to": stamp(self.wall())})
        self.take(now)

    def look(self):
        """See whether the Linux child still runs; one that ended by itself is a refusal, said once."""
        child = self.child
        if child is None:
            return
        try:
            code = child.poll()
        except FAILED:
            code = None
        if code is not None:
            self.child = None
            self.refuse("systemd-inhibit ended", code)
            for pipe in (child.stdin, child.stdout):
                try:
                    pipe.close()  # the host's ends of the two pipes; nothing uses them now
                except FAILED:
                    pass
            return
        if not self.confirmed:
            try:
                said = os.read(child.stdout.fileno(), 1)
            except BlockingIOError:
                said = None  # nothing yet
            except FAILED:
                said = b""
            now = self.clock()
            late = not 0 <= now - self.started < CONFIRM
            if said == b"1":
                self.confirmed = True
                self.mark(now)
                if late and not self.overdue:
                    # Held from now on, but the start of the shift went without: the word may
                    # have lain in the pipe for a while, and when it came is not known.
                    self.refuse("systemd-inhibit was seen to hold the lock only after %ds" % CONFIRM, None)
            elif late and not self.overdue:
                self.overdue = True  # it may still come; until then nothing is held
                self.refuse("systemd-inhibit did not say it holds the lock within %ds" % CONFIRM, None)

    def holding(self):
        if self.done:
            return False
        if self.kind == "mac":
            if self.identity is None or not 0 <= self.clock() - self.last <= TIMEOUT:
                return False
            # Its age says it should be held; whether it is, only the system knows. It may
            # have let it go early, which is otherwise seen only at the next renewal or at
            # the release, after the closeout was written.
            # Whatever the answer, the id is kept: an assertion that may still be there
            # is let go at the release, and a new one is made at the next renewal.
            try:
                answer = self.call(self.system.alive, self.identity, patience=ASK)
            except FAILED as exc:
                self.refuse("whether it is held could not be asked: " + type(exc).__name__ + ": " + str(exc), None)
                return None  # not known
            if answer is True:
                return True
            answer = False if answer is False else None
            if self.told != (self.identity, answer):  # said once for each
                self.told = (self.identity, answer)
                self.refuse("the system has it, but not in force (suspended, switched off or timed out)" if answer is False
                            else "the system gave nothing for it: it let it go before its timeout, or could not be "
                            "reached", None)
            return answer
        if self.kind == "inhibit":
            self.look()
        return self.kind == "inhibit" and self.child is not None and self.confirmed

    def release(self):
        if self.done:
            return
        self.done = True
        if self.kind == "mac" and self.identity is not None:
            identity, self.identity = self.identity, None
            self.drop(identity, self.clock())
        elif self.kind == "inhibit" and self.child is not None:
            child, self.child = self.child, None
            try:
                self.end(child)
            except FAILED as exc:
                self.refuse("systemd-inhibit not stopped: " + type(exc).__name__ + ": " + str(exc), None)

    def record(self):
        """Plain data for the closeout: the state at this moment. `held` is None when the
        system could not be asked whether it holds the assertion."""
        held = self.holding()
        return dict(
            asked=self.kind != "off", means=self.means, held=held,
            held_from=stamp(self.first) if self.first is not None else None, seen=stamp(self.wall()),
            # For the whole shift: held from the first try on, held now, nothing refused, no lapse.
            whole=bool(held is True and self.clean and self.began),
            refusals=list(self.refusals), refusals_count=self.refused, gaps=list(self.gaps), gaps_count=self.lapsed)
