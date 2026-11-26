"""Signed JSON authority: verified bytes, one admission, bounded guarded actions.

Grant admission and withdrawal were adapted from earlier code; the verifier
uses OpenSSH.
"""
from __future__ import annotations

import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from .bounds import Budget, Sources
from .layout import Refused, safe_child

NAMESPACE = "sortie-kit-grant-v1"
ACTIONS = {"local-work", "spawn-driver", "spawn-builder", "spawn-reviewer"}
FORMAT = 4  # the one format that is written and admitted; 1 to 3 are still read to check a signature
# What earlier formats signed, kept so that the grant of an old shift still verifies under its own rules.
ACTIONS_3 = {"local-work", "spawn-driver", "spawn-builder"}
LEGACY_ACTIONS = {"inventory", "read", "prompt", "local-work"}  # formats 1 and 2, signature checks only
ROLES = ("driver", "builder", "reviewer")
ROLES_3 = ("driver", "builder")
REVIEW_LIMIT, REVIEW_LIMITS = 600, (30, 3600)  # seconds one run of the reviewer may take: the default and the bounds
CHOICES = ("picked-other-family", "picked-same-family", "given")  # how the reviewer seat was chosen
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}\Z")
FAMILY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
POLICY = {"builder_model", "builder_family", "review_model", "review_family", "same_family_review",
          "review_choice"}
POLICY_3 = {"builder_model", "builder_family", "review_tool", "review_model", "review_family",
            "same_family_review"}  # formats 2 and 3: the review went through the agy-review script
KEY_TYPES = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
             "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def decoded(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Refused("duplicate JSON field")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Refused("nonfinite JSON")))
    except (ValueError, UnicodeError) as exc:
        raise Refused("invalid grant/trust JSON") from exc


def read_file(path, limit=1024 * 1024):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Refused("symlink in authority path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise Refused("authority must be a bounded regular file")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise Refused("authority file too large")
    return raw, (info.st_dev, info.st_ino, hashlib.sha256(raw).hexdigest())


def external_file(path, repo):
    path = Path(path).absolute()
    raw, pin = read_file(path)
    info = path.stat()
    if path.resolve().is_relative_to(Path(repo).resolve()):
        raise Refused("key and trust must be outside the target repo")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused("key and trust require owner-only permissions")
    return path, raw, pin


def publish(path, raw):
    """Install new complete bytes without overwriting existing evidence."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def moment(value):
    if not isinstance(value, str):
        raise Refused("grant timestamp must be a UTC string")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise Refused("invalid grant timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise Refused("grant timestamps must be UTC")
    return parsed


def seats_checked(seats, policy, version=FORMAT):
    """Each seat is a pinned agent start; the builder is the signed builder model and,
    from format 4 on, the reviewer seat is the signed review model. Format 3 has
    no reviewer seat."""
    from .agents import seat_checked
    roles = ROLES if version >= 4 else ROLES_3
    if not isinstance(seats, dict) or set(seats) != set(roles):
        raise Refused("grant must name exactly a driver, a builder and a reviewer seat" if version >= 4
                      else "grant must name exactly a driver and a builder seat")
    for role in roles:
        seat_checked(role, seats[role])
    builder = seats["builder"]
    if (builder["model"], builder["family"]) != (policy["builder_model"], policy["builder_family"]):
        raise Refused("builder seat model differs from the signed review policy")
    if version >= 4:
        reviewer = seats["reviewer"]
        if (reviewer["model"], reviewer["family"]) != (policy["review_model"], policy["review_family"]):
            raise Refused("reviewer seat model differs from the signed review policy")
    return seats


def legacy_seats_checked(seats):
    if not isinstance(seats, dict) or not seats:
        raise Refused("empty seat mapping")
    panes = []
    for role, seat in seats.items():
        if (not isinstance(role, str) or not TOKEN.fullmatch(role)
                or not isinstance(seat, dict) or set(seat) != {"pane", "runtime"}
                or not isinstance(seat["pane"], str) or not TOKEN.fullmatch(seat["pane"])
                or seat["runtime"] not in ("codex", "claude")):
            raise Refused("invalid seat scope")
        panes.append(seat["pane"])
    if len(set(panes)) != len(panes):
        raise Refused("seat panes must be distinct")
    return seats


# A maker's name and its model line are one family. Without this a builder placed in
# "gpt" and a reviewer stated as "openai" would pass as independent.
FAMILY_ALIASES = {"openai": "gpt", "anthropic": "claude", "google": "gemini"}


def family(value):
    """One canonical name per family, so neither "Claude" and "claude" nor "openai"
    and "gpt" can pass as different families."""
    value = value.strip().casefold() if isinstance(value, str) else ""
    if not FAMILY.fullmatch(value):
        raise Refused("model family must be a short token such as gpt, claude or gemini")
    return FAMILY_ALIASES.get(value, value)


def review_policy(builder_model, builder_family, review_model, review_family, choice):
    """The signed review policy. `choice` is how the reviewer seat was chosen (see
    `launcher.pick_reviewer`, which decides whether a same-family reviewer is
    allowed). Whether the review is same-family is not a choice here: it is what
    the two families are, said plainly."""
    builder_family, review_family = family(builder_family), family(review_family)
    return policy_checked({
        "builder_model": builder_model, "builder_family": builder_family,
        "review_model": review_model, "review_family": review_family,
        "same_family_review": builder_family == review_family, "review_choice": choice})


def policy_checked(policy, version=FORMAT):
    """Signed model identity for the builder and its reviewer; nothing is defaulted."""
    if not isinstance(policy, dict) or set(policy) != (POLICY if version >= 4 else POLICY_3):
        raise Refused("grant must name the builder model, review model and their families")
    if (any(not isinstance(policy[k], str) or not MODEL.fullmatch(policy[k])
            for k in ("builder_model", "review_model"))
            or any(not isinstance(policy[k], str) or not FAMILY.fullmatch(policy[k])
                   for k in ("builder_family", "review_family"))
            or type(policy["same_family_review"]) is not bool
            or (version >= 4 and policy["review_choice"] not in CHOICES)
            or (version < 4 and policy["review_tool"] != "agy-review")):
        raise Refused("invalid builder or review model identity")
    same = policy["builder_family"] == policy["review_family"]
    if version >= 4:
        # One name per family, here as where the policy is made: a maker's name beside its
        # model line would otherwise be signed as two families.
        if any(policy[k] != family(policy[k]) for k in ("builder_family", "review_family")):
            raise Refused("a model family in the grant is not in its canonical spelling")
        # Signed as a fact, not as a permission: the grant says what the review is.
        if policy["same_family_review"] != same:
            raise Refused("the grant's same-family statement differs from the builder and reviewer families")
        if {"picked-other-family": same, "picked-same-family": not same}.get(policy["review_choice"], False):
            raise Refused("the grant's account of how the reviewer was picked differs from the two families")
        return policy
    if same and not policy["same_family_review"]:
        raise Refused("builder and reviewer share a model family; choose another reviewer "
                      "or explicitly allow same-family review")
    if policy["same_family_review"] and not same:
        raise Refused("same-family review was allowed, but the builder and reviewer families differ")
    return policy


def review_limit(seconds):
    """The signed limit of one run of the reviewer, in whole seconds."""
    if type(seconds) is float and seconds == int(seconds):
        seconds = int(seconds)
    if type(seconds) is not int or not REVIEW_LIMITS[0] <= seconds <= REVIEW_LIMITS[1]:
        raise Refused("the review limit must be a whole number of seconds from " + str(REVIEW_LIMITS[0]) + " to "
                      + str(REVIEW_LIMITS[1]))
    return seconds


def shift_root(shift):
    """The repository of a shift directory. The path may be relative and may hold `.`
    and `..`; it may not lead through a symlink anywhere."""
    written, given = Path(shift), Path(shift).absolute()
    # `.` and `..` are dropped as written, with no link followed to do it. Whether
    # that is also where the system would arrive is checked below.
    shift = Path(os.path.normpath(given))
    # Callers take the shift's id from the end of the path as it was given.
    if written.name != shift.name:  # `.` from inside the shift has no name at all
        raise Refused("the path must end with the shift directory itself, not with `..`")
    if shift.parent.name != "shifts" or shift.parent.parent.name != ".sortie":
        raise Refused("shift must be under repo/.sortie/shifts")
    repo = shift.parent.parent.parent
    safe_child(repo, str(shift.relative_to(repo)))
    if not shift.is_dir():
        raise Refused("shift or canonical repo unavailable")
    # A link in the repository's own path, or one before a `..` (where the path as
    # written and the path the system follows part ways), is refused: every later
    # read of this shift must be of the one directory checked here.
    try:
        real = Path(os.path.realpath(given, strict=True))  # every part as written must be there to walk
        # And the system, walking the path as written, must arrive at that same directory.
        same = os.path.samestat(os.stat(given), os.stat(shift))
        # A link whose `..` leads back to where the written path goes (one that points
        # beside itself) passes both of those, so each part as written is looked at too.
        linked = any(part.is_symlink() for part in (given, *given.parents))
    except (OSError, ValueError) as exc:
        raise Refused("shift or canonical repo unavailable: a directory in the path as written is not there") from exc
    if repo != repo.resolve() or real != shift or not same or linked:
        raise Refused("the shift is reached through a symlink; give it by its real path "
                      "(shift or canonical repo unavailable)")
    return repo


class Trust:
    """Explicit developer-selected configuration; never nominated by a grant."""
    def __init__(self, path, repo):
        self.repo = Path(repo).resolve()
        self.path, self.raw, self.pin = external_file(path, self.repo)
        value = decoded(self.raw)
        if not isinstance(value, dict) or set(value) != {"version", "identity", "public_key", "ssh_keygen"}:
            raise Refused("unsupported trust configuration")
        if type(value["version"]) is not int or value["version"] != 1:
            raise Refused("unsupported trust version")
        self.identity = value["identity"]
        if not isinstance(self.identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", self.identity):
            raise Refused("trust requires one literal identity")
        key = value["public_key"]
        parts = key.split() if isinstance(key, str) else []
        if len(parts) != 2 or parts[0] not in KEY_TYPES:
            raise Refused("unsupported public key; hardware presence is not implemented")
        try:
            self.key_blob = base64.b64decode(parts[1], validate=True)
        except ValueError as exc:
            raise Refused("invalid public key") from exc
        self.public_key = key
        self.fingerprint = hashlib.sha256(self.key_blob).hexdigest()
        tool = value["ssh_keygen"]
        if not isinstance(tool, str) or not Path(tool).is_absolute():
            raise Refused("trust requires an absolute ssh-keygen executable")
        self.tool = Path(tool).resolve(strict=True)
        if self.tool.is_relative_to(self.repo) or not os.access(self.tool, os.X_OK):
            raise Refused("ssh-keygen must be executable outside the target repo")
        _, self.tool_pin = read_file(self.tool, 32 * 1024 * 1024)

    def guard(self):
        try:
            if external_file(self.path, self.repo)[2] != self.pin:
                return "trust replaced"
            if read_file(self.tool, 32 * 1024 * 1024)[1] != self.tool_pin:
                return "signing tool changed"
        except (OSError, ValueError):
            return "trust unavailable or changed"
        return ""

    def environment(self, scratch):
        # No inherited agent socket, askpass helper, provider, preload or secret.
        return {"PATH": os.defpath, "HOME": str(scratch), "LC_ALL": "C",
                "SSH_ASKPASS_REQUIRE": "never", "TMPDIR": str(scratch),
                "TMP": str(scratch), "TEMP": str(scratch)}


def make_grant(shift, trust, seats, seconds, *, policy, review_seconds=REVIEW_LIMIT, now=None):
    repo = shift_root(shift)
    now = now or dt.datetime.now(dt.timezone.utc)
    Budget(seconds)
    return encoded({
        "version": FORMAT, "namespace": NAMESPACE, "identity": trust.identity,
        "key_sha256": trust.fingerprint, "presence": "terminal-passphrase",
        "shift_id": Path(shift).name, "nonce": uuid.uuid4().hex, "repo": str(repo),
        "direction_sha256": read_file(Path(shift) / "direction.md")[1][2],
        "seats": seats_checked(seats, policy_checked(policy)), "review_policy": policy,
        "review_limit_seconds": review_limit(review_seconds), "start": now.isoformat(),
        "until": (now + dt.timedelta(seconds=seconds)).isoformat(),
        "admit_before": (now + dt.timedelta(seconds=min(120, seconds))).isoformat(),
        "allowed_actions": sorted(ACTIONS), "finalization_seconds": 10,
    })


def scope(raw, shift, trust, *, now=None, expected_seats=None, legacy=False, current=True):
    """Format 4 is the only admissible grant. legacy=True lets a signature check
    (never an admission) still read format 1 (no models), format 2 (pane seats)
    and format 3 (two seats, the review through the agy-review script), each
    under the rules it was signed with. current=False, for a signature check
    only, leaves out whether the window is open now and whether the shift was
    withdrawn; `window` says those apart."""
    value = decoded(raw)
    fields = {"version", "namespace", "identity", "key_sha256", "presence", "shift_id",
              "nonce", "repo", "direction_sha256", "seats", "start", "until",
              "admit_before", "allowed_actions", "finalization_seconds"}
    version = value.get("version") if isinstance(value, dict) else None
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise Refused("unsupported grant format")
    if version != FORMAT and not legacy:
        raise Refused("format-3 grant has no reviewer seat: this version of sortie-kit runs the review itself, "
                      "with a reviewer you sign; sign the shift again" if version == 3 else
                      "format-" + str(version) + " grant does not pin agent seats; sign a fresh shift")
    if set(value) != fields | {1: set(), 2: {"review_policy"}, 3: {"review_policy"},
                               4: {"review_policy", "review_limit_seconds"}}[version]:
        raise Refused("unsupported grant format")
    if version != 1:
        policy_checked(value["review_policy"], version)
    if version >= 4:
        review_limit(value["review_limit_seconds"])
    if (value["namespace"] != NAMESPACE or value["identity"] != trust.identity
            or value["key_sha256"] != trust.fingerprint or value["presence"] != "terminal-passphrase"):
        raise Refused("grant identity or namespace mismatch")
    repo = shift_root(shift)
    if repo != trust.repo or value["repo"] != str(repo) or value["shift_id"] != Path(shift).name:
        raise Refused("grant repo or shift mismatch")
    if not isinstance(value["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"]):
        raise Refused("invalid grant nonce")
    if value["direction_sha256"] != read_file(Path(shift) / "direction.md")[1][2]:
        raise Refused("direction digest mismatch")
    if version >= 3:
        seats_checked(value["seats"], value["review_policy"], version)
    else:
        legacy_seats_checked(value["seats"])
    if expected_seats is not None and value["seats"] != expected_seats:
        raise Refused("signed seats differ from the requested agents")
    actions, known = value["allowed_actions"], {4: ACTIONS, 3: ACTIONS_3}.get(version, LEGACY_ACTIONS)
    if (not isinstance(actions, list) or any(not isinstance(a, str) or a not in known for a in actions)
            or len(set(actions)) != len(actions)):
        raise Refused("unsupported grant actions")
    if type(value["finalization_seconds"]) is not int or not 0 <= value["finalization_seconds"] <= 10:
        raise Refused("invalid report-only finalization bound")
    start, until, admit = (moment(value[key]) for key in ("start", "until", "admit_before"))
    now = now or dt.datetime.now(dt.timezone.utc)
    if not start < admit <= min(until, start + dt.timedelta(seconds=120)):
        raise Refused("grant not current or invalid admission window")
    if not current:
        return value
    if not start <= now < until:
        raise Refused("grant not current or invalid admission window")
    if os.path.lexists(Path(shift) / "STOP"):
        raise Refused("shift withdrawn")
    return value


def ended(shift, raw):
    """When the shift of the grant bytes `raw` ended, by the host's own closeout in the
    shift folder, or None when that does not say that it has. The file is not signed.

    The host writes `closeout.json` while a shift runs as well, and once more when the
    driver closes the shift, before anything is stopped: a closeout alone is no end,
    and neither is a state that says `closed`. The host's last closeout is the one
    that says what became of its cleanup, `done` or `incomplete`; every earlier one
    says `not yet performed` (`launcher.report_read` tells a final report the same
    way). The closeout of an admitted start that failed is an end too and names no
    cleanup. A closeout of another grant, or one that cannot be read, says nothing."""
    try:
        closed = json.loads(read_file(Path(shift) / "closeout.json")[0])
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(closed, dict) or closed.get("grant_sha256") != hashlib.sha256(raw).hexdigest():
        return None
    if closed.get("source") != "admitted start failed":
        processes = closed.get("processes")
        cleanup = processes.get("cleanup") if isinstance(processes, dict) else None
        if not isinstance(cleanup, str) or not cleanup.startswith(("done", "incomplete")):
            return None
    try:
        return moment(closed.get("end"))
    except ValueError:
        return None


def window(value, shift, now=None, closed=False):
    """Whether the signed window of a verified grant is open now: (True, words) when
    it is and the shift was not withdrawn, else (False, words). Words for a person.
    `closed` is for a shift that has ended: an open window is then the grant's, and
    is not called current."""
    start, until = moment(value["start"]), moment(value["until"])
    now = now or dt.datetime.now(dt.timezone.utc)
    said = lambda at: at.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if now >= until:
        return False, "ended " + said(until)
    if now < start:
        return False, "not begun; it starts " + said(start)
    if os.path.lexists(Path(shift) / "STOP"):
        return False, "open until " + said(until) + ", but the shift was withdrawn (sortie stop, or the host at its end)"
    return True, ("the grant's window runs until " if closed else "current until ") + said(until)


def verify(shift, trust, *, expected_seats=None, now=None, timeout=10, legacy=False, current=True):
    """Hold exact input bytes; verify snapshots, then detect replacement.
    current=False is for `sortie verify` alone (see `scope`); an admission never passes it."""
    shift = Path(shift)
    raw, grant_pin = read_file(shift / "grant.json")
    signature, signature_pin = read_file(shift / "grant.json.sig")
    value = scope(raw, shift, trust, now=now, expected_seats=expected_seats, legacy=legacy, current=current)
    if why := trust.guard():
        raise Refused(why)
    scratch = safe_child(shift, "scratch")
    with tempfile.TemporaryDirectory(prefix="verify-", dir=scratch) as work:
        work = Path(work)
        (work / "signature").write_bytes(signature)
        (work / "signers").write_text(f'{trust.identity} namespaces="{NAMESPACE}" {trust.public_key}\n')
        result = subprocess.run([str(trust.tool), "-Y", "verify", "-f", str(work / "signers"),
                                 "-I", trust.identity, "-n", NAMESPACE, "-s", str(work / "signature")],
                                input=raw, capture_output=True, timeout=timeout,
                                env=trust.environment(scratch))
    if result.returncode:
        raise Refused("grant signature verification failed")
    if (read_file(shift / "grant.json")[1] != grant_pin
            or read_file(shift / "grant.json.sig")[1] != signature_pin or trust.guard()):
        raise Refused("authority changed during verification")
    scope(raw, shift, trust, now=now, expected_seats=expected_seats, legacy=legacy, current=current)
    return raw, grant_pin, signature_pin


class Admission:
    """One foreground owner, immutable binding, durable replay refusal.

    The consumed nonce survives clean exit and crashes. No recovery or stale-lock
    deletion grants a second admission; the developer signs a fresh shift.
    """
    def __init__(self, shift, trust, *, seconds, expected_seats, sources=None,
                 now=lambda: dt.datetime.now(dt.timezone.utc), clock=None):
        self.shift, self.trust = Path(shift), trust
        self.fd = None
        self.closed = False
        self.refusal = ""
        self.now = now
        self.sources = sources or Sources()
        self.budget = Budget(seconds, now=now, **({"clock": clock} if clock else {}))
        self.raw, self.grant_pin, self.signature_pin = verify(
            self.shift, trust, expected_seats=expected_seats, now=now(), timeout=min(10, self.budget.remaining()))
        value = decoded(self.raw)
        if now() >= moment(value["admit_before"]):
            raise Refused("stale admission; sign a fresh shift")
        self.budget = Budget(min(seconds, self.budget.remaining()), until=moment(value["until"]),
                             now=now, **({"clock": clock} if clock else {}))
        repo = shift_root(self.shift)
        lock = safe_child(repo, ".sortie/owner.lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Refused("invalid owner lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Refused("another shift owns this repo") from exc
            self.lock, self.lock_identity = lock, (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            # Signed pins must match what is on disk now; later starts compare
            # against these observations instead of hashing a large binary again.
            from . import agents
            self.executables = {}
            for role in ROLES:
                seat = value["seats"][role]
                self.executables[role] = agents.check_executable(seat["executable"], repo)
            # Hashing is slow; a program replaced while a later pin was read
            # must not be admitted on its earlier observation.
            for role in ROLES:
                if agents.observe(value["seats"][role]["executable"]["path"]) != self.executables[role]:
                    raise Refused(role + " agent executable changed during admission")
            # The pin reads took time. Whatever expired, was withdrawn or changed
            # meanwhile refuses here, while the nonce is still unused. Later
            # failures keep the consumed nonce; there is no rollback.
            if why := self.standing():
                raise Refused(why)
            if now() >= moment(value["admit_before"]):
                raise Refused("stale admission; sign a fresh shift")
            ledger = safe_child(repo, ".sortie/admissions")
            ledger.mkdir(mode=0o700, exist_ok=True)
            self.marker = ledger / value["nonce"]
            self.binding = encoded({"grant_sha256": self.grant_pin[2], "signature_sha256": self.signature_pin[2],
                                    "trust_sha256": trust.pin[2], "shift_id": value["shift_id"]})
            try:
                publish(self.marker, self.binding)
            except FileExistsError as exc:
                raise Refused("grant already admitted; replay refused") from exc
            self.marker_pin = read_file(self.marker)[1]
            self.fd = fd
            if why := self.guard():
                raise Refused(why)
            # Slow publication and validation must finish before admission closes.
            # Running guards use the separate expiry; keep the consumed nonce.
            if now() >= moment(value["admit_before"]):
                raise Refused("stale admission; sign a fresh shift")
        except BaseException:
            self.fd = None
            os.close(fd)
            raise

    @property
    def value(self):
        return decoded(self.raw)  # Callers cannot mutate effective authority.

    def guard(self):
        if self.closed or self.fd is None:
            return "admission closed"
        if self.refusal:
            return self.refusal
        try:
            self.refusal = self.standing(marker=True)
        except (OSError, ValueError):
            self.refusal = "admitted scope unavailable, changed, or withdrawn"
        return self.refusal

    def standing(self, marker=False):
        """Why the authority no longer holds, or "". One list of checks for the
        last look before the nonce is consumed and for every guard after it; only
        the consumed marker itself (marker=True) cannot be checked beforehand."""
        why = self.budget.guard() or self.sources.guard() or self.trust.guard()
        if not why:
            scope(self.raw, self.shift, self.trust, now=self.now())
            checks = [(self.shift / "grant.json", self.grant_pin),
                      (self.shift / "grant.json.sig", self.signature_pin)]
            if marker:
                checks.append((self.marker, self.marker_pin))
            if any(read_file(path)[1] != pin for path, pin in checks):
                why = "admitted authority replaced or missing"
            info = self.lock.stat(follow_symlinks=False)
            if (info.st_dev, info.st_ino) != self.lock_identity:
                why = "owner lock replaced"
        # Recheck time after potentially slow reads/hash checks.
        return why or self.budget.guard()

    def seat_start(self, role, prompt, cwd=None):
        """Argv for one signed seat, only while the admission and every pin still hold.

        Called immediately before each start. Nothing is taken from the caller
        except the host-authored prompt and, for the reviewer alone, the
        directory the host made for that review: a directory directly under
        the shift's `reviews`, which is where the reviewer is started instead
        of the repository. The executable is compared with its
        admission-time observation first and again last. That is a check, not an
        atomic start: the caller must start the returned argv at once, and a
        writer able to replace the program between this return and the start
        is outside what a path check can exclude.
        """
        from . import agents
        if why := self.guard():
            raise Refused(why)
        value = self.value
        if "spawn-" + role not in value["allowed_actions"]:
            raise Refused("signed grant does not authorize starting the " + role)
        seat = value["seats"][role]
        if role != "reviewer":
            if cwd is not None:
                raise Refused("only the reviewer is started outside the repository")
            cwd = self.trust.repo
        else:
            # Named by the host, and still checked: a real directory, reached through no
            # link, directly under this shift's reviews folder.
            where, folder = Path(cwd) if cwd is not None else None, self.shift / "reviews"
            if (where is None or not where.is_absolute() or where.parent != folder or folder.is_symlink()
                    or where.is_symlink() or not where.is_dir()):
                raise Refused("the reviewer is started only in a directory the host made under the shift's reviews")
            cwd = where

        def unchanged():
            # A look at the file, not at its bytes: device, inode, size and two times. A file
            # written again with the same bytes is refused as any other replacement, and the
            # words say what was compared, since for a launcher script the rewrite is all the
            # host can see of an update of the program behind it.
            try:
                if agents.observe(seat["executable"]["path"]) == self.executables[role]:
                    return
            except OSError:
                raise Refused(role + " agent executable cannot be looked at after admission") from None
            raise Refused(role + " agent executable replaced or rewritten after admission: the file at the pinned "
                          "path is another file or has other size or times; its bytes were not read again")

        unchanged()
        argv = agents.render_argv(seat, prompt=prompt, cwd=cwd)
        if why := self.guard():
            raise Refused(why)
        unchanged()  # last, after the guard's slower reads
        return argv

    def close(self):
        self.closed = True
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def stop(shift):
    shift = Path(shift)
    shift_root(shift)
    try:
        publish(shift / "STOP", encoded({"version": 1, "reason": "developer stop"}))
    except FileExistsError:
        pass  # Any existing marker, including damaged evidence, already withholds.
