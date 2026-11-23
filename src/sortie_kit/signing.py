"""Developer-terminal presence path. Python never reads the passphrase."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time

from .bounds import Budget, Sources
from .grants import NAMESPACE, external_file, read_file, publish, scope, verify
from .layout import Refused, safe_child


def encrypted_key(raw, trusted_blob):
    """Inspect only OpenSSH's unencrypted envelope, never decrypt key material."""
    lines = raw.strip().splitlines()
    if len(lines) < 3 or lines[0] != b"-----BEGIN OPENSSH PRIVATE KEY-----" or lines[-1] != b"-----END OPENSSH PRIVATE KEY-----":
        raise Refused("require a passphrase-encrypted OpenSSH private key")
    try:
        data = base64.b64decode(b"".join(lines[1:-1]), validate=True)
        magic = b"openssh-key-v1\x00"
        if not data.startswith(magic):
            raise ValueError("format")
        offset = len(magic)
        def number():
            nonlocal offset
            value, = struct.unpack_from(">I", data, offset)
            offset += 4
            return value
        def string():
            nonlocal offset
            size = number()
            value = data[offset:offset + size]
            offset += size
            if len(value) != size:
                raise ValueError("truncated")
            return value
        cipher, kdf, options = string(), string(), string()
        if cipher == b"none" or kdf != b"bcrypt" or not options:
            raise Refused("unencrypted keys cannot prove password presence")
        if number() != 1 or string() != trusted_blob:
            raise Refused("private key does not match trusted public key")
        if not string() or offset != len(data):
            raise ValueError("private section")
    except Refused:
        raise
    except (ValueError, struct.error) as exc:
        raise Refused("invalid or unsupported encrypted private key") from exc


def terminal():
    """Require inherited foreground terminal streams; never accept piped secrets.

    This cannot identify a human-owned PTY versus an agent-owned PTY. Harness
    rules must deny agents this signing path/key and access to this terminal.
    """
    if threading.current_thread() is not threading.main_thread():
        raise Refused("signing requires the foreground main thread")
    if not all(os.isatty(fd) for fd in (0, 1, 2)):
        raise Refused("sign only in the developer's own terminal; redirected streams refused")
    fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    try:
        if os.tcgetpgrp(fd) != os.getpgrp():
            raise Refused("signing requires the foreground terminal")
        # /dev/tty is an alias device; compare stream devices to one another.
        if len({os.fstat(n).st_rdev for n in (0, 1, 2)}) != 1:
            raise Refused("signing streams must share the developer's terminal")
        return fd
    except BaseException:
        os.close(fd)
        raise


def shown(text):
    """One line of plain ASCII for the terminal: control and non-ASCII characters appear as escapes."""
    return json.dumps(str(text), ensure_ascii=True)[1:-1]


def span(seconds):
    """A duration in the largest two whole units, for example 1h 30m or 45s."""
    seconds = int(seconds)
    hours, minutes, rest = seconds // 3600, seconds % 3600 // 60, seconds % 60
    parts = [str(n) + unit for n, unit in ((hours, "h"), (minutes, "m"), (rest, "s")) if n]
    return " ".join(parts[:2]) or "0s"


# How the reviewer seat came to be the one it is, as the signer reads it.
CHOSEN = {"picked-other-family": "picked: the first installed agent of another family than the builder",
          "picked-same-family": "picked: no agent of another family is installed",
          "given": "given with --agent reviewer="}
SAME = {"picked-same-family": "because no agent of another family is installed",
        "given": "as you named it with --agent reviewer=",
        "picked-other-family": ""}


def summary(value):
    """What the developer is about to sign, in plain words. Built only from the
    decoded grant; the exact bytes are shown after it and are what counts."""
    from . import agents
    from .bounds import closing, wind_down
    from .grants import moment
    start, until = moment(value["start"]), moment(value["until"])
    window = (until - start).total_seconds()
    reserve = wind_down(window)
    policy = value["review_policy"]
    lines = ["Shift to sign",
             "  Repository: " + shown(value["repo"]),
             "  Direction:  direction.md in the shift, SHA256 " + shown(value["direction_sha256"]),
             "  Window:     " + span(window) + ", until " + until.strftime("%Y-%m-%d %H:%M:%S %Z") + ".",
             "              New work can start for at most " + span(window - reserve)
             + ", less what signing and startup take.",
             "              In the last " + span(reserve) + " nothing new starts; a builder turn already running may finish"
             + (", and a review starts only when its whole limit still fits." if "review_limit_seconds" in value else "."),
             "              " + span(closing(reserve)) + " before the deadline the host begins to stop whatever still runs."]
    # Where the shift is, so it can be ended from another terminal while the driver has this one.
    place = value["repo"].rstrip("/") + "/.sortie/shifts/" + value["shift_id"]
    lines += ["  Shift:      " + shown(place), "              To end it early, from another terminal:"]
    lines.append("              sortie stop --shift " + shlex.quote(place) if shown(place) == place
                 else "              sortie stop with this shift's directory (shown above with escapes) as --shift")
    for role, how in (("driver", "in this terminal"), ("builder", "headless, one turn at a time")):
        seat = value["seats"][role]
        lines += ["  " + (role.capitalize() + ":").ljust(12) + shown(seat["agent"]) + ", model " + shown(seat["model"])
                  + " (family " + shown(seat["family"]) + "), " + how,
                  "              " + shown(shlex.join(agents.shown_command(seat, value["repo"])))]
    # The reviewer is shown, never asked for: who it is, how it came to be that one, the
    # command the host starts for each review, and what that command can reach.
    seat, limit = value["seats"]["reviewer"], value["review_limit_seconds"]
    lines += ["  Reviewer:   " + shown(seat["agent"]) + ", model " + shown(seat["model"]) + " (family "
              + shown(seat["family"]) + "), headless, started by the host for each review",
              "              " + CHOSEN[policy["review_choice"]]]
    if policy["same_family_review"]:
        lines.append("              same-family review: the reviewer is of the builder's own family ("
                     + shown(policy["builder_family"]) + "), " + SAME[policy["review_choice"]])
    lines += ["              " + shown(shlex.join(agents.shown_command(seat, place + "/reviews/REVIEW"))),
              "              REVIEW stands for the directory the host makes for each run of a review.",
              "              One run may take at most " + span(limit) + "; a run that gives nothing usable is",
              "              made once more, and a second failure blocks that task."]
    lines.append("  What the builder may do:")
    lines += ["    - " + shown(line) for line in agents.reach("builder", value["seats"]["builder"])]
    lines.append("  What the reviewer can reach:")
    lines += ["    - " + shown(line) for line in agents.reach("reviewer", seat)]
    lines += ["  Authority:  the grant's own actions are local work and starting these three seats. It has no",
              "              push or publish action. What the agents can reach is what their command lines",
              "              and your configuration allow; sortie-kit inspects neither."]
    return "\n".join(lines) + "\n"


def sign(shift, trust, key, raw, *, seconds, sources=None, note=None):
    """Sign exact public scope through ssh-keygen's direct terminal prompt.

    `note` is one line about this run that the grant does not hold (whether the
    host will keep the machine awake). It is shown after the summary, under words
    that say it is not signed.

    No password parameter, input buffer, capture pipe, env secret, askpass or
    agent socket exists. Only ssh-keygen reads the secret from the terminal.
    """
    budget, sources = Budget(seconds), sources or Sources()
    shift = Path(shift)
    value = scope(raw, shift, trust)
    key, key_raw, key_pin = external_file(key, trust.repo)
    encrypted_key(key_raw, trust.key_blob)
    tty = terminal()  # Refuse before writing signed-scope artifacts without TTY.
    child = None
    previous = {}
    interrupted = False
    saved_terminal = None
    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True
    try:
        saved_terminal = termios.tcgetattr(tty)
        hidden = list(saved_terminal)
        hidden[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(tty, termios.TCSANOW, hidden)
        if termios.tcgetattr(tty)[3] & (termios.ECHO | termios.ECHONL):
            raise Refused("cannot disable terminal echo; refusing password prompt")
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupt)
        for name in ("grant.json", "grant.json.sig"):
            if os.path.lexists(shift / name):
                raise Refused("grant already exists; prepare a fresh shift")
        if why := trust.guard() or sources.guard() or budget.guard():
            raise Refused(why)
        # JSON escapes controls in arbitrary direction/seat values, in the summary
        # and in the bytes; no terminal control sequences or password content
        # are interpolated here.
        aside = "" if note is None else "\nNot part of what is signed:\n  " + shown(note) + "\n"
        os.write(tty, (summary(value) + aside).encode("ascii") + b"\nExact bytes being signed:\n" + raw)
        os.write(tty, b"Enter the key passphrase only at ssh-keygen's prompt in your own terminal.\n")
        publish(shift / "grant.json", raw)
        scratch = safe_child(shift, "scratch")
        child = subprocess.Popen([str(trust.tool), "-Y", "sign", "-f", str(key),
                                  "-n", NAMESPACE, str(shift / "grant.json")],
                                 stdin=tty, stdout=tty, stderr=tty, cwd=shift,
                                 env=trust.environment(scratch))
        # Same foreground group is essential for ssh-keygen's /dev/tty read.
        # No askpass or provider descendants are permitted by this default mode.
        while child.poll() is None:
            why = ("signing interrupted" if interrupted else "") or budget.guard() or sources.guard() or trust.guard()
            if why or external_file(key, trust.repo)[2] != key_pin or read_file(shift / "grant.json")[0] != raw:
                raise Refused(why or "signing input changed")
            time.sleep(min(0.05, budget.remaining()))
        if child.returncode or interrupted:
            raise Refused("terminal signature failed or interrupted")
        if budget.guard() or sources.guard() or trust.guard() or external_file(key, trust.repo)[2] != key_pin:
            raise Refused("signing deadline or source/key/trust changed")
        verified, _, _ = verify(shift, trust, timeout=min(10, budget.remaining()))
        if verified != raw:
            raise Refused("signed scope changed")
        if why := budget.guard() or sources.guard():
            raise Refused(why)
        return shift
    except termios.error as exc:
        raise Refused("terminal echo control unavailable; refusing password prompt") from exc
    finally:
        try:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=0.5)
        finally:
            try:
                if saved_terminal is not None:
                    termios.tcsetattr(tty, termios.TCSANOW, saved_terminal)
            except (OSError, termios.error):
                pass  # A hung-up terminal may already be gone.
            os.close(tty)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
