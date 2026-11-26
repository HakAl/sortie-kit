"""Bounded task evidence validation. This module never executes receipt commands."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from .bounds import Budget
from .grants import shift_root
from .layout import Elapsed, Refused
from .receipt_schema import asset, decode, shape, validate_shape

DOCUMENT_LIMIT = 1024 * 1024
ARTIFACT_LIMIT = 16 * 1024 * 1024
TOTAL_LIMIT = 64 * 1024 * 1024
PHASES = ("plan", "build", "review", "acknowledgment", "stop")
COMMON = ("version", "shift_id", "task_id", "request_id", "role", "phase", "round", "brief")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


# The host's own copies of the files consumed receipts name, each named by its SHA256.
# A seat chooses the names of its files and may use one again, as a later builder turn
# did with a test log: what a receipt points to must not depend on that. The name starts
# with a dot, like the ledger's, so the look at receipts/ around a turn leaves it out.
KEPT = "receipts/.kept"


class TooLarge(Refused):
    """A regular file whose size alone is over the limit for its kind. It was not read,
    and it is known not to be a file that was read within that limit."""


class Changed(Refused):
    """A file that was written or replaced between the first look at it and the last.
    Nothing is known of its content from this read. It is not known to be damaged."""


class Unsettled(Elapsed, Changed):
    """The time ran out while a file that changed under the read was read again. Both
    hold: a caller that gives an operation that ran out of time another try, or answers
    it as out of time, still does, and one that takes a changed file as no reading does."""


# How often one name is read while it keeps changing under the read, and the pause
# between two reads: a fifth of a second in all.
SETTLE_TRIES, SETTLE_PAUSE = 20, 0.01


def kept_path(sha256):
    return KEPT + "/" + sha256


def references(node):
    """Every {path, sha256} a receipt names, in the order it names them."""
    if isinstance(node, dict):
        if set(node) == {"path", "sha256"}:
            yield node
        else:
            for value in node.values():
                yield from references(value)
    elif isinstance(node, list):
        for value in node:
            yield from references(value)


def keep(view, value):
    """Copy what this receipt names into KEPT, from the bytes this session checked
    against the receipt's sums. Written once; a name already there must hold its sum."""
    with view.parent(KEPT) as (parent, name):
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    for sha in dict.fromkeys(ref["sha256"] for ref in references(value)):
        if sha not in view.held:
            raise Refused("a file the receipt names was not checked: no copy can be kept")
        # Looked for first: the same bytes are named by many receipts, the brief by every
        # one of a task. A copy already there is the host's own and is checked, not written
        # again, and is not counted against what may be read of a seat's evidence.
        try:
            copy = view.read(kept_path(sha), ARTIFACT_LIMIT, charge=False)
        except FileNotFoundError:
            try:
                view.atomic(kept_path(sha), view.held[sha], limit=ARTIFACT_LIMIT)
                continue
            except FileExistsError:  # made between the look and the write
                copy = view.read(kept_path(sha), ARTIFACT_LIMIT, charge=False)
        with view.parent(kept_path(sha)) as (parent, name):
            links = os.stat(name, dir_fd=parent, follow_symlinks=False).st_nlink
        # One name only: a copy that is also a file under another name can be written there.
        if digest(copy) != sha or links != 1:
            raise Refused("the kept copy " + kept_path(sha) + (" has another name" if digest(copy) == sha
                                                               else " does not hold the bytes it is named by"))


def encoded(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def relative(path):
    if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
            or any(ord(c) < 32 for c in path)
            or any(p in ("", ".", "..") for p in path.split("/"))):
        raise Refused("unsafe relative evidence path")
    return path.split("/")


class Evidence:
    def __init__(self, shift, seconds):
        if isinstance(seconds, (int, float)) and seconds <= 0:
            raise Elapsed("no time was left for this")
        self.budget = Budget(seconds)
        self.guard()
        self.shift = Path(shift).absolute()
        self.repo = shift_root(self.shift)
        self.fd = os.open(self.shift, DIRECTORY)
        self.total = 0
        self.commits = set()
        self.held = None  # a dict, while a receipt is being consumed: the bytes of each artifact checked, by sum

    def guard(self):
        if why := self.budget.guard():
            raise Elapsed(why)

    def renew(self, seconds):
        """Give what follows a time of its own, apart from what was spent so far."""
        if isinstance(seconds, (int, float)) and seconds <= 0:
            raise Elapsed("no time was left for this")
        self.budget = Budget(seconds)
        self.guard()  # also what a caller put around the guard, such as the end of finalization

    @contextmanager
    def parent(self, path):
        parts = relative(path)
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                self.guard()
                next_fd = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def read(self, path, limit=DOCUMENT_LIMIT, *, charge=True):
        self.guard()
        with self.parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise Refused("evidence must be a bounded regular file: " + path)
            if before.st_size > limit:
                raise TooLarge("evidence must be a bounded regular file: " + path)
            chunks, size = [], 0
            while True:
                self.guard()
                chunk = os.read(fd, min(65536, limit - size + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if charge:
                    self.total += len(chunk)
                if size > limit or self.total > TOTAL_LIMIT:
                    raise Refused("evidence read limit exceeded")
            after = os.fstat(fd)
            pin = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if pin(before) != pin(after):
                raise Changed("evidence changed while reading")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def settled(self, path, limit=DOCUMENT_LIMIT):
        """Read a file that its owner replaces while others read it: the host's state,
        answers, expected context and ledger, a seat's command or request. A read the
        replacement cut across is made again, SETTLE_TRIES times in all and within this
        session's time; a name that changed under every one of them is refused as `read`
        refuses it, and one whose reading again ran out of time is Unsettled. Each read is pinned
        as any other. Never for a receipt or a file a
        receipt names: those are written once, and one that changes is refused."""
        changed = None
        for left in range(SETTLE_TRIES - 1, -1, -1):
            total = self.total
            try:
                return self.read(path, limit)
            except Changed as exc:
                if not left:
                    raise
                changed = exc
                self.total = total  # what is read again is counted once
                time.sleep(max(0, min(SETTLE_PAUSE, self.budget.remaining())))
            except Elapsed as exc:
                if changed is None:
                    raise
                raise Unsettled("evidence changed while reading, and the time ran out on reading it again") from exc

    def artifact(self, ref):
        raw = self.read(ref["path"], ARTIFACT_LIMIT)
        if digest(raw) != ref["sha256"]:
            raise Refused("artifact hash mismatch: " + ref["path"])
        if self.held is not None:
            self.held[ref["sha256"]] = raw
        return raw

    def git(self, *args):
        self.guard()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
        result = subprocess.run(["git", "--no-optional-locks", "--no-replace-objects",
                                 "-C", str(self.repo), "-c", "core.fsmonitor=false", *args],
                                env=env, capture_output=True,
                                timeout=min(5, self.budget.remaining()))
        self.guard()
        if result.returncode:
            raise Refused("receipt Git check failed")
        return result.stdout.strip()

    def commit(self, value):
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise Refused("receipt requires a full commit object ID")
        if value not in self.commits:
            if self.git("cat-file", "-t", value) != b"commit":
                raise Refused("receipt target is not a commit")
            self.commits.add(value)

    def scratch_path(self, path):
        parts = relative(path)
        if parts[0] != "scratch" or len(parts) < 2:
            raise Refused("reproduction must name shift-local scratch")
        # A historical worktree may have been removed, but existing components
        # must not redirect the recorded location through a symlink.
        fd = os.dup(self.fd)
        try:
            for part in parts:
                self.guard()
                try:
                    next_fd = os.open(part, DIRECTORY, dir_fd=fd)
                except FileNotFoundError:
                    break
                os.close(fd)
                fd = next_fd
        finally:
            os.close(fd)

    def atomic(self, path, raw, *, replace=False, limit=DOCUMENT_LIMIT):
        if len(raw) > limit:
            raise Refused("receipt document exceeds limit")
        with self.parent(path) as (parent, name):
            temp = ".receipt-" + uuid.uuid4().hex
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            try:
                offset = 0
                while offset < len(raw):
                    self.guard()
                    count = os.write(fd, raw[offset:])
                    if count <= 0:
                        raise Refused("short receipt write")
                    offset += count
                os.fsync(fd)
                self.guard()
                if replace:
                    os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
            finally:
                os.close(fd)
                try:
                    os.unlink(temp, dir_fd=parent)
                except FileNotFoundError:
                    pass

    @contextmanager
    def lock(self):
        with self.parent("receipts/.ledger.lock") as (parent, name):
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            # Concurrent nonexclusive O_CREAT can return ENOENT on macOS.
            # Create once, then open the winner's permanent lock inode.
            try:
                fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            except FileExistsError:
                fd = os.open(name, flags, dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise Refused("receipt lock is not a regular file")
                while True:
                    self.guard()
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(min(0.02, self.budget.remaining()))
                yield
            finally:
                os.close(fd)


@contextmanager
def session(shift, seconds, *, local_only=False):
    view = None
    try:
        view = Evidence(shift, seconds)
        if not local_only and Path(os.fsdecode(view.git("rev-parse", "--show-toplevel"))) != view.repo:
            raise Refused("shift must belong to the Git worktree root")
        yield view
    except Refused:
        raise
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired) and view is not None and view.budget.remaining() <= 0:
            raise Elapsed("duration elapsed") from exc
        raise Refused("receipt check refused: " + str(exc)) from exc
    finally:
        if view is not None:
            os.close(view.fd)


def target(view, value):
    if value["kind"] == "commit":
        view.commit(value["commit"])
    else:
        view.artifact(value["artifact"])


def review_policy(expected):
    """Whether builder and reviewer are of one family, refused where the signed choice contradicts it."""
    signed = expected["review"]
    same = signed["family"] == expected["implementer_family"]
    if same != signed["same_family"]:
        raise Refused("the signed reviewer's same-family statement contradicts the expected families")
    return same


def answers(view, data, expected):
    """What the builder says it did about each finding it was handed: every one of
    them once, and no other. A claim, kept for the next review to check."""
    given = [item["id"] for item in data["answers"]]
    if len(set(given)) != len(given):
        raise Refused("a finding is answered twice")
    if sorted(given) != sorted(expected["required_findings"]):
        raise Refused("the receipt must answer each finding it was handed, once, and no other: "
                      + (", ".join(sorted(expected["required_findings"])) or "there are none"))
    for item in data["answers"]:
        if item["evidence"] is not None:
            view.artifact(item["evidence"])


def reviewed(view, data, expected, reasons):
    """A review receipt is the host's record of what it ran and what it took from
    the output. Each attempt's kept output is read again here and must give what
    the receipt says it gave, so the verdict rests on the raw output and on
    nothing a seat wrote."""
    from . import review
    if data["target"] != expected["target"]:
        raise Refused("review target differs from expected candidate or plan")
    if data["implementer_family"] != expected["implementer_family"]:
        raise Refused("review implementer family differs from expected context")
    if data["reviewer"] != expected["review"]:
        raise Refused("the reviewer differs from the signed reviewer")
    review_policy(expected)
    target(view, data["target"])
    if data["base"] is not None:
        view.commit(data["base"])
    prior = data["prior"]
    if (len({item["id"] for item in prior}) != len(prior)
            or sorted(item["id"] for item in prior) != sorted(expected["required_findings"])):
        raise Refused("the earlier findings of the review differ from the expected context")
    attempts = data["attempts"]
    for index, attempt in enumerate(attempts):
        if attempt["attempt"] != index + 1:
            raise Refused("review attempts are numbered in order")
        kept = {name: view.artifact(attempt[name]) for name in ("stdout", "stderr") if attempt[name] is not None}
        if attempt["status"] == "complete":
            if index != len(attempts) - 1:
                raise Refused("only the last attempt of a review can be complete")
            if "stdout" not in kept or attempt["marker"] is None or attempt["repository"] != "unchanged":
                raise Refused("a complete review attempt keeps its output and marker and left the repository unchanged")
            if (attempt["ended_by"] != "exit" or attempt["exit_status"] != 0 or attempt["output_dropped_bytes"]
                    or attempt["reasons"] or not attempt["argv"]):
                raise Refused("a complete review attempt ended on its own with status 0, with all its output kept, "
                              "its command recorded and no reason for failure")
            # No schema is named: the kept object's own decides. A commit review kept by an earlier build has
            # no requirement list and is still what the host took then; one with the list keeps its contract.
            again = review.check(kept["stdout"].decode("utf-8", "replace"), attempt["marker"], data["target"], prior,
                                 schema=None)
            if (again["status"], again["verdict"], again["record"]) != ("complete", attempt["verdict"], attempt["record"]):
                raise Refused("the kept review output does not give the verdict and record the receipt states")
        elif attempt["verdict"] is not None or attempt["record"] is not None or not attempt["reasons"]:
            raise Refused("a failed review attempt has a reason and neither verdict nor record")
    last = attempts[-1]
    if data["outcome"] != (last["verdict"] if last["status"] == "complete" else "FAILED"):
        raise Refused("the review outcome is not what its last attempt gave")
    if data["outcome"] == "FAILED":
        reasons.append("review failed")
    elif data["outcome"] == "CHANGES":
        reasons.append("review requests changes")


def payload(view, value, expected):
    validate_shape(expected, "expected")
    validate_shape(value, "receipt")
    if expected["shift_id"] != view.shift.name:
        raise Refused("expected shift does not match directory")
    if any(value[key] != expected[key] for key in COMMON):
        raise Refused("stale or mismatched receipt context")
    phase, data = value["phase"], value["data"]
    context(view, expected)
    view.artifact(value["brief"])
    reasons = []
    if phase == "plan":
        if data["plan"] != expected["target"]["artifact"]:
            raise Refused("plan artifact differs from expected digest")
        view.artifact(data["plan"])
        answers(view, data, expected)
    elif phase == "build":
        if data["commit"] != expected["target"]["commit"]:
            raise Refused("build candidate mismatch")
        view.commit(data["commit"])
        for path in data["touched_files"]:
            if relative(path)[0] == ".git":
                raise Refused("touched path must name repository content")
        for check in data["checks"]:
            view.artifact(check["output"])
            if check["exit_status"] != check["expected_exit"]:
                reasons.append("build check did not produce expected exit status")
        if not data["checks"] and not data["no_checks_reason"]:
            raise Refused("build needs checks or an explicit applicability reason")
        answers(view, data, expected)
    elif phase == "review":
        reviewed(view, data, expected, reasons)
    else:
        for evidence in data["evidence"]:
            view.artifact(evidence)
        if phase == "stop" and data["scope"] != expected["stop_scope"]:
            raise Refused("stop scope differs from expected cessation claim")
        if data["status"] not in ("acknowledged", "stopped"):
            reasons.append("acknowledgment or cessation remains unknown")
    view.guard()
    return {"status": "blocked" if reasons else "diagnostic", "satisfies_phase": False,
            "recorded": False, "receipt_id": value["receipt_id"], "reasons": reasons}


def context(view, expected):
    """What the expected context must be for its own phase, whatever the receipt says."""
    phase = expected["phase"]
    if expected["shift_id"] != view.shift.name:
        raise Refused("expected shift does not match directory")
    if (phase == "review") != (expected["role"] == "reviewer"):
        raise Refused("a review, and only a review, is the reviewer's")
    if phase != "review" and (expected["implementer_family"] is not None or expected["review"] is not None):
        raise Refused("unexpected review policy on another phase")
    if phase not in ("plan", "build", "review") and expected["required_findings"]:
        raise Refused("unexpected findings to answer on this phase")
    if phase == "review" and (expected["implementer_family"] is None or expected["review"] is None):
        raise Refused("expected review context must bind implementer family and signed reviewer")
    if phase != "stop" and expected["stop_scope"] is not None:
        raise Refused("unexpected stop scope")
    wanted_kind = "plan" if phase == "plan" else "commit" if phase == "build" else None
    if phase in ("plan", "build", "review"):
        if expected["target"] is None or (wanted_kind and expected["target"]["kind"] != wanted_kind):
            raise Refused("expected phase target missing or wrong kind")
    elif expected["target"] is not None:
        raise Refused("unexpected phase target")


def ledger(view):
    try:
        value = decode(view.settled("receipts/.consumed.json"))
    except FileNotFoundError:
        return {"version": 1, "entries": []}
    if (not isinstance(value, dict) or set(value) != {"version", "entries"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["entries"], list) or len(value["entries"]) > 4096):
        raise Refused("invalid receipt ledger")
    ids, slots = set(), set()
    for entry in value["entries"]:
        if (not isinstance(entry, dict) or set(entry) != {"receipt_id", "slot", "sha256", "satisfies_phase"}
                or not isinstance(entry["receipt_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", entry["receipt_id"])
                or any(not isinstance(entry[k], str) or not re.fullmatch("[0-9a-f]{64}", entry[k])
                       for k in ("slot", "sha256"))
                or type(entry["satisfies_phase"]) is not bool
                or entry["receipt_id"] in ids or entry["slot"] in slots):
            raise Refused("invalid receipt ledger entry")
        ids.add(entry["receipt_id"])
        slots.add(entry["slot"])
    return value


def phase_slot(value):
    return digest(encoded([value[k] for k in ("shift_id", "task_id", "request_id", "role", "phase", "round")]))


def consumed(state, value):
    """Whether the ledger already holds this receipt's ID or its phase slot."""
    slot = phase_slot(value)
    return any(e["slot"] == slot or e["receipt_id"] == value["receipt_id"] for e in state["entries"])


def validate_receipt(shift, receipt_path, expected_path, *, record=False, seconds=10, expected_sha256=None):
    """Check attributed evidence; only record=True reserves its phase slot.

    A caller that built the expected context itself passes its digest, so a file
    replaced after publication cannot stand in for the caller's own policy.
    """
    with session(shift, seconds) as view:
        def check():
            pinned = view.settled(expected_path)
            if expected_sha256 is not None and digest(pinned) != expected_sha256:
                raise Refused("expected context changed after its owner published it")
            expected = decode(pinned)
            raw = view.read(receipt_path)
            value = decode(raw)
            view.held = {} if record else None
            result = payload(view, value, expected)
            if receipt_path != "receipts/" + value["receipt_id"] + ".json":
                raise Refused("receipt must use its complete receipt-ID filename")
            state, slot = ledger(view), phase_slot(value)
            result["sha256"] = digest(raw)
            if consumed(state, value):
                result.update(status="duplicate", satisfies_phase=False, reasons=["receipt ID or phase slot already consumed"])
                return result
            if record:
                if len(state["entries"]) == 4096:
                    raise Refused("receipt ledger is full")
                # Before the ledger entry: no receipt is consumed without its copies. A review
                # receipt of the host's has them too: the host names its output files itself,
                # but under logs/, where a builder writes.
                keep(view, value)
                satisfies = result["status"] == "diagnostic"
                state["entries"].append({"receipt_id": value["receipt_id"], "slot": slot,
                                         "sha256": digest(raw), "satisfies_phase": satisfies})
                view.guard()
                view.atomic("receipts/.consumed.json", encoded(state), replace=True)
                result["recorded"] = True
                if satisfies:
                    result.update(status="satisfied", satisfies_phase=True)
            return result
        if record:
            with view.lock():
                return check()
        return check()


def write_receipt_atomic(shift, value, expected_path, *, seconds=10):
    """Write checked evidence once; this does not reserve or advance a phase."""
    with session(shift, seconds) as view:
        return written(view, value, expected_path)["path"]


def written(view, value, expected_path, *, host=False):
    raw = encoded(value)
    if len(raw) > DOCUMENT_LIMIT:
        raise Refused("receipt document exceeds limit")
    if not host and isinstance(value, dict) and (value.get("phase") == "review" or value.get("role") == "reviewer"):
        raise Refused("a review receipt is written by the host from the reviewer's own output; no seat writes one")
    result = payload(view, decode(raw), decode(view.settled(expected_path)))
    # The ledger is read as the receipt check reads it, so that what is written here
    # is something that check would still take: an unreadable ledger refuses, and so
    # does a receipt ID or a phase slot the driver has already consumed.
    if consumed(ledger(view), value):
        raise Refused("receipt ID or phase slot already consumed: the driver has taken a receipt for this "
                      "request and round, so another one cannot advance it. Read current-context.json again")
    path = "receipts/" + value["receipt_id"] + ".json"
    try:
        view.atomic(path, raw)
    except FileExistsError as exc:
        raise Refused(path + " already exists and is left as it is; a receipt is written once. A corrected "
                      "receipt needs a new receipt_id") from exc
    return dict(status="written", path=path, sha256=digest(raw),
                check=dict(status=result["status"], reasons=result["reasons"]))


def write_receipt_from(shift, draft_path, expected_path, *, seconds=10):
    """`sortie receipt write`: take a filled template kept in the shift, check it as
    the receipt check will, and write it once as receipts/<receipt_id>.json.

    Both paths are relative to the shift. A draft the check refuses is not
    written. One that is well formed and bound to the expected context is
    written even when its content cannot satisfy the phase (a failed build
    check, "not acknowledged"): that is evidence too, and `check` says what the
    receipt check made of it. Nothing is recorded in the ledger and no phase
    advances; consuming the receipt is the driver's.
    """
    with session(shift, seconds) as view:
        value = decode(view.read(draft_path))
        validate_shape(value, "receipt")
        return written(view, value, expected_path)
