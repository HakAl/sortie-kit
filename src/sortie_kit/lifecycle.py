"""Driver-owned sequencing and mechanical evidence checks. No artifact execution."""
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

from . import channels, receipts, review as contract, review_dir
from .bounds import Budget, Sources, closing, wind_down as default_wind_down
from .layout import Elapsed, Refused
from .grants import moment
from .receipt_schema import decode, validate_shape

STAGES = {"ack", "plan", "plan_review", "build", "review", "repair_needed", "accepted", "closed", "blocked"}
PHASE = {"ack": "acknowledgment", "plan": "plan", "build": "build"}  # the stages that wait for a builder's receipt
REVIEW = {"plan_review": "plan", "review": "commit"}  # the stages at which the host runs a review, and of what
FINDINGS = {"plan": "plan", "plan_review": "plan", "build": "candidate", "review": "candidate"}
REVIEW_TRIES = 2  # a failed review is run once more; a second failure blocks the task
LATE = "exit seen after its limit"  # how a review ended that the host first saw over after its signed limit
OUTSTANDING = 64  # findings one repair round can be handed
SEVERITY = {"blocker": 0, "major": 1, "minor": 2}
TURN_DEFAULT, TURN_MAX, TURN_CAP = 1800, 7200, 32
TURN_LOG_LIMIT = 4 * 1024 * 1024
PUMP_READS = 64  # at most 4 MiB read per host loop pass
LAST_PUMPS = 16  # passes of that size at a review's close, to come to the end of both pipes
WRITE_RESERVE = 0.5  # seconds kept for writing a turn's end before any optional listing
PREPARE_ROOM = 3 * WRITE_RESERVE + 1  # seconds under the lock without which no further review attempt is prepared
RECEIPT_ENTRIES = 1024  # entries of receipts/ the host will walk for one observation
LOST = ("a builder turn from an earlier host process has no recorded end; "
        "its process may still run. No new turn; check for it by hand")
LOST_REVIEW = ("a review from an earlier host process has no recorded end; "
               "its process may still run. No new turn or review; check for it by hand")
HELD = ("wind-down: the review's signed limit does not fit in the working time left, so none is started; "
        "the work stays handed in and not reviewed. ")
WINDING = ("What a turn already handed in can still be taken with consume and close; then send finish "
           "with what is left and who is next. The host stops everything shortly before the signed deadline.")
DROPPED = ("GIT_", "SSH_AUTH_SOCK", "SSH_ASKPASS", "SSH_AGENT_PID", "GIT_ASKPASS", "SUDO_ASKPASS")
COMMANDS = {
    "brief": ({"task_id", "kind", "brief", "next_owner"}, {"turn_seconds"}),
    "consume": ({"task_id", "request_id", "receipt"}, {"target"}),
    "repair": ({"task_id", "request_id", "reason", "next_owner"}, set()),
    "progress": ({"task_id", "request_id", "evidence"}, set()),
    "continue": ({"task_id", "request_id"}, {"turn_seconds", "again"}),
    "close": ({"task_id", "request_id"}, set()),
    "stop-evidence": ({"task_id", "request_id", "receipt"}, set()),
    "finish": ({"reason", "next_owner"}, {"not_started"}),
    "note": ({"kind", "text"}, {"task_id"}),
}


def toolkit():
    """The command that runs this toolkit, as text for a shell: the interpreter running
    the host, by its full path, and this package's entry file, by its full path. Each
    seat is told it in its prompt, so neither needs a `sortie` on its PATH or has to
    look for one, and what runs is this package and not another `sortie_kit` that
    the seat's working directory or PYTHONPATH would offer (see entry.py). `-P` keeps
    the package's own directory off the module path."""
    entry = os.path.join(os.path.dirname(os.path.abspath(__file__)), "entry.py")
    return shlex.join([sys.executable, "-P", entry])


def builder_prompt(shift, task_words):
    """Everything the host tells the builder at the start of a turn. Host-authored: the
    same text for every turn apart from the task words and these local facts. A turn
    is a fresh process, so every turn is told all of it."""
    return ("Read builder.md and tiger.md in " + str(shift) + ". " + task_words + " Stop at the signed deadline.\n"
            "shift: " + str(shift) + "\n"
            "toolkit: " + toolkit() + "\n"
            "Where builder.md says TOOLKIT, run the command on the toolkit line exactly as it is written "
            "there, and where it says SHIFT, use the path on the shift line. Do not run a bare `sortie`: "
            "none may be on your PATH, and one that is may be another version.")


def builder_env(shift, base=None, *, temp=None):
    """The user's environment and real HOME (the CLI needs its own sign-in), without
    Git overrides or SSH agent and askpass variables. `temp` is the directory the
    host made for this shift's temporary files (see turn_temp.py): short and
    outside the repository. Without one they go to scratch under the shift."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not any(k == name or (name.endswith("_") and k.startswith(name)) for name in DROPPED)}
    where = str(shift / "scratch") if temp is None else str(temp)
    env.update(TMPDIR=where, TMP=where, TEMP=where, GIT_TERMINAL_PROMPT="0")
    return env


def receipt_marks(view, seconds=0.25, clock=time.monotonic):
    """The entries of the shift's receipts/ as names with their change marks, or
    None when they cannot all be listed within a small fixed bound of entries and
    time. Reached through the shift's own open directory; entries are never
    opened or followed. Names starting with a dot (the host's ledger, writers'
    temporary files) are walked and counted against the bound but not kept."""
    end, marks, seen = clock() + seconds, {}, 0
    try:
        with view.parent("receipts/entry") as (fd, _):
            with os.scandir(fd) as entries:
                for entry in entries:
                    seen += 1
                    if seen > RECEIPT_ENTRIES or clock() >= end:
                        return None
                    if not entry.name.startswith("."):
                        info = entry.stat(follow_symlinks=False)
                        marks[entry.name] = (info.st_ino, info.st_size, info.st_mtime_ns)
        return None if clock() >= end else marks
    except (OSError, ValueError):
        return None


def spawn_builder(argv, *, cwd, env):
    """One owned headless process group: no terminal input, output on a pipe the host bounds."""
    from .tick_supervise import OwnedChild
    return OwnedChild(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      cwd=cwd, env=env, process_group=0)


def spawn_reviewer(argv, *, cwd, env):
    """One owned headless process group for a review: no terminal input, and what it
    prints kept apart from its error output, each on a pipe the host bounds."""
    from .tick_supervise import OwnedChild
    return OwnedChild(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      cwd=cwd, env=env, process_group=0)


def reviewer_prompt(where):
    """Everything on the reviewer's command line besides its signed arguments. The
    instructions themselves are a file, so nothing long is in the argv or the receipt."""
    return ("You are the reviewer for one task of a sortie-kit shift. Read REVIEW.md in this directory ("
            + str(where) + ") and do exactly what it says. You inspect and report only: change nothing, run "
            "nothing that writes, and end your answer with the JSON object that REVIEW.md describes, between "
            "the two marker lines it names.")


RECEIPT_PATH = re.compile(r"receipts/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json")


class Unusable(Refused):
    """A driver command refused for what it says, found before it had any effect."""


class Uncertain(Refused):
    """Authority or storage failed after a command began to take effect. What
    was done is not known to be complete, so this is never a receipt to repair."""


@contextmanager
def asked():
    """Checks of what the driver wrote. Everything inside runs before the command
    changes anything, so a failure leaves the task and the shift as they were."""
    try:
        yield
    except Unusable:
        raise
    except FileNotFoundError as exc:
        raise Unusable("a file the command names is not there") from exc
    except (Refused, KeyError, TypeError) as exc:
        raise Unusable(str(exc)) from exc


class Driver:
    def __init__(self, admission, spawn=None, *, seconds, wind_down=None, sources=None, spawn_review=None):
        self.admission, self.spawn = admission, spawn or spawn_builder
        # A stand-in given for the builder start serves the reviewer start too, unless one is named.
        self.spawn_review = spawn_review or (spawn_reviewer if spawn is None else spawn)
        self.child, self.turn_end, self.turn_fd = None, 0.0, None
        self.turn_bytes = self.turn_dropped = 0
        self.pipe_end = {}  # per pipe of the owned process, how the last read of it ended: see `drain`
        # The one owned process is a builder turn or a review. A review's error output
        # has its own file; `reviewing` is what the host holds of the running attempt.
        self.kind, self.err_fd, self.err_bytes, self.err_dropped, self.reviewing = "turn", None, 0, 0, None
        # For the turn record: when the process was started and seen over, and what
        # receipts/ held before it.
        self.turn_began, self.turn_over, self.turn_receipts = None, None, None
        self.attention = False  # the host's own cleanup left something the developer must look at
        # stopping: why the owned turn is being ended while its stop is unconfirmed.
        # observed: an end this host saw but has not yet written. unconfirmed: a
        # builder group this host could not confirm stopped before it let go.
        self.stopping, self.observed, self.unconfirmed, self.cleaned = None, None, False, False
        self.cleanup_errors = []  # what went wrong while ending a turn at the host's own stop
        self.leftover_groups = []  # process group ids of builder turns whose stop was not confirmed
        self.halt = lambda: False  # the host's stop latch: cheap, asked right before a spawn
        self.lost_words = LOST  # what `settle` found lost: a builder turn, or a review
        self.stopped = False  # the host began its own stop: a failed review is not run again
        self.initialized = False
        self.shift = admission.shift
        self.now, self.clock = admission.now, admission.budget.clock
        self.observed_start, self.observed_mono = self.now().isoformat(), self.clock()
        self.budget = Budget(seconds, clock=self.clock)
        self.sources = sources or Sources()
        self.pin = receipts.digest(admission.raw)
        self.value = admission.value
        if wind_down is None:
            # From the signed window, not from what is left of it when this host starts.
            wind_down = default_wind_down((moment(self.value["until"]) - moment(self.value["start"])).total_seconds())
        self.wind_down = wind_down
        if not math.isfinite(self.wind_down) or self.wind_down <= 0:
            raise Refused("wind-down interval must be positive and finite")
        self.closing = closing(self.wind_down)
        self.repo = {}  # what the host saw of the target repository at its start and end, for the closeout
        self.final_end = min(self.budget.end, admission.budget.end) + self.value["finalization_seconds"]
        if set(self.value["seats"]) != {"driver", "builder", "reviewer"}:
            raise Refused("driver requires the signed three-seat map")
        # Signed identity only: neither a CLI name nor a receipt decides the family or reviewer.
        policy = self.value["review_policy"]
        self.family = policy["builder_family"]
        self.reviewer = dict(agent=self.value["seats"]["reviewer"]["agent"], model=policy["review_model"],
                             family=policy["review_family"], same_family=policy["same_family_review"],
                             choice=policy["review_choice"])
        self.review_limit = self.value["review_limit_seconds"]
        if type(self.review_limit) is not int or self.review_limit < 1:
            raise Refused("driver requires the signed review limit")
        self.last_command = None
        self.serving = False  # the command being applied came through the running host's channel
        self.turn_seen = (None, None)  # receipts/ before the turn and its elapsed time, for `record_turn`
        self.ending = None  # (stop, reaped, errors) of a turn whose group was signaled and not yet drained
        self.processes = {}  # what the host saw of its own child processes, for the closeout
        self.wake = None  # the host's hold against idle sleep: a call that gives its record, for the closeout
        # The directory the host made for the temporary files of turns and reviews
        # (turn_temp.TurnTemp), or None when this host was given none to make.
        self.temp = None

    def temp_path(self):
        return None if self.temp is None else self.temp.path

    def guard(self):
        return (self.budget.guard() or self.sources.guard() or self.admission.guard()
                or ("local work not authorized" if "local-work" not in self.value["allowed_actions"] else ""))

    def active_seconds(self):
        """Time left in which new work may start. At zero the shift is in wind-down:
        no new task, repair or builder turn; a running turn, `consume`, `close`
        and `finish` go on until `work_seconds` is used up."""
        return max(0, min(self.budget.remaining(), self.admission.budget.remaining()) - self.wind_down)

    def work_seconds(self):
        """Time left until the host stops the builder turn and the driver."""
        return max(0, min(self.budget.remaining(), self.admission.budget.remaining()) - self.closing)

    def final_seconds(self):
        wall = (moment(self.value["until"]) - self.now()).total_seconds() + self.value["finalization_seconds"]
        return max(0, min(5, self.final_end - self.clock(), wall))

    def live_seconds(self, cap=10):
        """Time a live operation may be given: never more than the working time
        left, so that one that stalls cannot hold the host past the stop. Zero
        once the working time is over: the operation is then not begun."""
        return min(cap, self.work_seconds())

    def step_seconds(self, cap=10):
        """Time for one step of the command being applied. Through the running host
        that is the working time left. A direct caller (the host's own
        finalization, a test) has the finalization time once the working time is over."""
        work = self.work_seconds()
        return min(cap, work if work > 0 or self.serving else self.final_seconds())

    def answer_seconds(self):
        """Time to write the answer to a command already taken: within the working
        time, and a quarter second when that ran out while the command was applied.
        Never more than is left of the finalization time; zero when that is over."""
        return min(self.final_seconds(), max(0.25, self.work_seconds()))

    @contextmanager
    def coordination(self, *, local_only=False, live=False, serving=False):
        """The shift's coordination lock. `live`: the wait for the lock and the work
        under it end with the working time while any is left. `serving`: the
        running host is at work in its loop, taking a driver command or writing
        the end of a turn; once the working time is over the lock is refused,
        for `finish` too. A direct caller keeps the finalization time then,
        which is how the host's own finalization closes the shift."""
        seconds, work = self.final_seconds(), self.work_seconds()
        if live and work > 0:
            seconds = min(seconds, work)
        elif serving:
            raise Elapsed("working time over: the host is stopping; no command is taken")
        if seconds <= 0:
            raise Elapsed("finalization window elapsed")
        with channels.locked(self.shift, "driver.lock", seconds=seconds, local_only=local_only) as view:
            bounded_guard = view.guard
            def guard():
                bounded_guard()
                if self.final_seconds() <= 0:
                    raise Elapsed("finalization window elapsed")
            view.guard = guard
            view.guard()
            yield view

    def require_live(self, new_work=False):
        if why := self.guard():
            raise Refused(why)
        if new_work and self.active_seconds() <= 0:
            raise Refused("wind-down: no new brief or repair. " + WINDING)

    def moment(self):
        return int(self.now().timestamp())

    def initial(self):
        return dict(version=1, grant_sha256=self.pin, direction_sha256=self.value["direction_sha256"],
                    mode="active", active=None, tasks={}, commands={}, pending=None, turn=None, reason="",
                    next_owner="driver", not_started=None, review=None, last_review=None, notes=[])

    def load(self, view):
        try:
            state = decode(view.read("driver-state.json"))
        except FileNotFoundError:
            return self.initial()
        if isinstance(state, dict):
            state.setdefault("not_started", None)  # a state from a host that had no such field
            state.setdefault("notes", [])
        channels.fields(state, self.initial())
        if (type(state["version"]) is not int or state["version"] != 1
                or state["grant_sha256"] != self.pin or state["direction_sha256"] != self.value["direction_sha256"]
                or state["mode"] not in ("active", "blocked", "closed")
                or not isinstance(state["tasks"], dict) or len(state["tasks"]) > 128
                or not isinstance(state["commands"], dict) or len(state["commands"]) > 512):
            raise Refused("invalid or differently bound driver state")
        if state["active"] is not None and state["active"] not in state["tasks"]:
            raise Refused("active task missing")
        if state["not_started"] is not None:
            channels.unstarted(state["not_started"])
        if any(note["task_id"] is not None and note["task_id"] not in state["tasks"]
               for note in channels.notes(state["notes"])):
            raise Refused("a note names a task the state does not hold")
        for key, task in state["tasks"].items():
            channels.token(key)
            channels.fields(task, self.new_task(key, "feature", {}, "builder"))
            if (task["id"] != key or task["kind"] not in ("feature", "defect") or task["stage"] not in STAGES
                    or type(task["round"]) is not int or not 0 <= task["round"] <= 3
                    or type(task["generation"]) is not int or type(task["last_progress"]) is not int
                    or not isinstance(task["receipts"], list) or len(task["receipts"]) > 128
                    or not isinstance(task["progress"], list) or len(task["progress"]) > 128
                    or not isinstance(task["turns"], list) or len(task["turns"]) > TURN_CAP
                    or not isinstance(task["reviews"], list) or len(task["reviews"]) > 128
                    or (task["base"] is not None and not (isinstance(task["base"], str)
                                                          and contract.COMMIT.fullmatch(task["base"])))):
                raise Refused("invalid task state")
            channels.token(task["request_id"])
            channels.fields(task["required"], {"plan", "candidate"})
            channels.fields(task["findings"], {"plan", "candidate"})
            for items in task["required"].values():
                contract.prior_checked(items)
            for kept in task["findings"].values():
                if kept is not None:
                    channels.fields(kept, {"path", "sha256"})
        for identity, item in state["commands"].items():
            channels.token(identity)
            # `at`: the host's time when it recorded the command. A state kept by a host
            # from before that has none.
            channels.fields(item, {"digest", "response"}, {"at"})
            if (not isinstance(item["response"], dict) or not isinstance(item["digest"], str)
                    or ("at" in item and (not isinstance(item["at"], str) or len(item["at"]) > 40
                                          or channels.timestamp(item["at"]) is None))):
                raise Refused("invalid command record")
        if state["pending"] is not None:
            channels.fields(state["pending"], {"id", "digest", "op"})
        if state["turn"] is not None:
            channels.fields(state["turn"], {"task_id", "turn", "started", "limit_seconds", "log"})
            if state["turn"]["task_id"] not in state["tasks"]:
                raise Refused("running turn names an unknown task")
        if state["review"] is not None:
            channels.fields(state["review"], {"task_id", "review", "request_id", "stage", "round", "target", "prior",
                                              "attempts", "running"})
            if (state["review"]["task_id"] not in state["tasks"] or state["turn"] is not None
                    or not isinstance(state["review"]["attempts"], list)
                    or len(state["review"]["attempts"]) > REVIEW_TRIES):
                raise Refused("running review names an unknown task or runs beside a turn")
        if state["last_review"] is not None and not isinstance(state["last_review"], dict):
            raise Refused("invalid last review")
        return state

    def save(self, view, state):
        view.atomic("driver-state.json", receipts.encoded(state), replace=True)
        if state["turn"] is None and (state["review"] is None or state["review"]["running"] is None):
            self.observed = None  # the end this host saw is on disk now, not before
            if self.child is None:
                # What a review's end is worked out from is kept until that end is on
                # disk, so that a write that ran out of time can be made again.
                self.kind, self.reviewing = "turn", None
        if state["active"]:
            task = state["tasks"][state["active"]]
            context = self.expected(task, None) if task["stage"] in PHASE else None
            # `findings`: at a plan or build stage, the file with the review findings the
            # builder's next receipt has to answer, or null when there are none.
            view.atomic("current-context.json", receipts.encoded(dict(
                version=3, task_id=task["id"], stage=task["stage"], request_id=task["request_id"],
                round=task["round"], brief=task["brief"], expected=context,
                stop_expected=self.stop_expected(task),
                findings=task["findings"][FINDINGS[task["stage"]]] if task["stage"] in PHASE and task["stage"] != "ack" else None,
                builder=dict(model=self.value["review_policy"]["builder_model"], family=self.family),
                reviewer=self.reviewer)), replace=True)

    def new_task(self, identity, kind, brief, owner):
        return dict(id=identity, kind=kind, brief=brief, next_owner=owner,
                    round=0, stage="ack", request_id=uuid.uuid4().hex, plan=None, candidate=None,
                    required={"plan": [], "candidate": []}, findings={"plan": None, "candidate": None},
                    base=None, reviews=[], receipts=[], progress=[], turns=[],
                    acknowledged=False, accepted=None, cessation=None, generation=0,
                    last_progress=self.moment(), repair_from=None, reason="")

    def stage(self, task, stage):
        task.update(stage=stage, request_id=uuid.uuid4().hex, last_progress=self.moment())
        task["generation"] += 1

    def expected(self, task, target):
        """The context a builder's receipt for the task's stage is checked against. A
        plan or a build names the findings of the last review that it has to answer."""
        phase = PHASE[task["stage"]]
        handed = task["required"][FINDINGS[task["stage"]]] if task["stage"] != "ack" else []
        return dict(version=3, shift_id=self.shift.name, task_id=task["id"], request_id=task["request_id"],
                    role="builder", phase=phase, round=task["round"], brief=task["brief"], target=target,
                    required_findings=[item["id"] for item in handed],
                    implementer_family=None, review=None, stop_scope=None)

    def review_expected(self, task):
        """The context the host's own review receipt is checked against: the plan or
        candidate the stage holds, the earlier findings the review had to answer,
        and the signed reviewer."""
        key = FINDINGS[task["stage"]]
        return dict(version=3, shift_id=self.shift.name, task_id=task["id"], request_id=task["request_id"],
                    role="reviewer", phase="review", round=task["round"], brief=task["brief"], target=task[key],
                    required_findings=[item["id"] for item in task["required"][key]],
                    implementer_family=self.family, review=self.reviewer, stop_scope=None)

    def stop_expected(self, task):
        """The context a builder seat's stop receipt is checked against: the task's
        current request and round, phase `stop`, the seat's own cessation."""
        return dict(version=3, shift_id=self.shift.name, task_id=task["id"], request_id=task["request_id"],
                    role="builder", phase="stop", round=task["round"], brief=task["brief"], target=None,
                    required_findings=[], implementer_family=None, review=None, stop_scope="seat")

    def task(self, state, command):
        task = state["tasks"].get(state["active"])
        if task is None or command["task_id"] != task["id"] or command["request_id"] != task["request_id"]:
            raise Unusable("command does not name the active task phase: take task_id and request_id from "
                           "current-context.json as it is now")
        return task

    def turn_limit(self, command):
        seconds = command.get("turn_seconds", TURN_DEFAULT)
        if type(seconds) is not int or not 1 <= seconds <= TURN_MAX:
            raise Refused("turn_seconds must be a whole number from 1 to " + str(TURN_MAX))
        return seconds

    def builder_turn(self, view, state, task, message, seconds):
        """Start one headless builder process for the active task. A started turn is
        delivery of a prompt to a new process, never acknowledgment or progress."""
        if state["turn"] is not None or state["review"] is not None or self.child is not None:
            return "blocked", self.busy(state)
        if len(task["turns"]) >= TURN_CAP:
            return "blocked", "turn limit reached for this task: " + str(TURN_CAP)
        if self.active_seconds() < 1:
            return "blocked", "wind-down: no new builder turn. " + WINDING
        # A turn started in time may run on through wind-down, never past the stop.
        seconds = int(min(seconds, self.work_seconds()))
        if seconds < 1:
            return "blocked", "wind-down: no new builder turn. " + WINDING
        self.require_live(new_work=True)
        cutoff = self.clock() + seconds  # fixed now: slow checks below shorten the turn, never extend it
        number = len(task["turns"]) + 1
        log = "logs/builder-turn-" + task["id"] + "-" + str(number) + ".log"
        # Intent first: a crash after this line leaves a turn the next host records as lost.
        state["turn"] = dict(task_id=task["id"], turn=number, started=self.now().isoformat(),
                             limit_seconds=seconds, log=log)
        self.save(view, state)
        fd = None
        try:
            # The host creates the log and keeps this descriptor for the whole turn. The
            # builder can write under logs/; whatever it later puts at that path is not opened.
            with view.parent(log) as (parent, leaf):
                fd = os.open(leaf, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
            cwd, env = str(self.admission.trust.repo), builder_env(self.shift, temp=self.temp_path())
            # An observation only, bounded, and taken before the start's own checks so
            # that nothing slow stands between those checks and the spawn.
            marks = receipt_marks(view)
            argv = self.admission.seat_start("builder", message)
            # The last decisions, with nothing left to prepare between them and the
            # spawn. A stop noted after this point is the cleanup's to handle.
            if self.clock() >= cutoff or self.active_seconds() < 1:
                raise Refused("wind-down or the turn limit was reached while checking the start")
            if self.halt():
                raise Refused("the host is stopping")
            self.turn_began, self.turn_over, self.turn_receipts = self.clock(), None, marks
            self.kind = "turn"
            self.child = self.spawn(argv, cwd=cwd, env=env)
        except (OSError, ValueError) as exc:
            # Nothing runs; say so on the task instead of leaving a turn nobody owns.
            if fd is not None:
                os.close(fd)
            self.ended(state, "not started: " + str(exc)[:500], None, False, 0, None)
            return "blocked", "builder turn " + str(number) + " did not start: " + str(exc)[:500]
        self.turn_end, self.turn_fd = cutoff, fd
        self.turn_bytes = self.turn_dropped = 0
        self.pipe_end = {}
        self.stopping = self.observed = None
        stream = getattr(self.child, "stdout", None)
        if stream is not None:
            os.set_blocking(stream.fileno(), False)
        return "recorded", "builder turn " + str(number) + " started; a started turn is not acknowledgment"

    def busy(self, state):
        """Why nothing new is started while the one owned process is still there."""
        what = "review" if state["review"] is not None or self.kind == "review" else "builder turn"
        if self.stopping:
            return "the last " + what + " could not be confirmed stopped; no new turn or review"
        return "a " + what + " is already running" if what == "builder turn" else "a review is running; wait for it to end"

    def may_review(self):
        """Whether a review may start now: at any time before wind-down, and in
        wind-down only when its whole signed limit fits in the working time left."""
        return self.active_seconds() >= 1 or self.work_seconds() >= max(1, self.review_limit)

    def start_review(self, view, state, task):
        """Begin the host's own review of the plan or candidate the task's stage holds,
        or run on one that was begun and has no attempt running. Returns a status and
        words for the driver: `recorded` when a reviewer process was started."""
        review = state["review"]
        if state["turn"] is not None or self.child is not None or (review is not None and review["running"] is not None):
            return "blocked", self.busy(state)
        if why := self.guard():
            return "blocked", "no review started: " + why
        if self.stopped or self.halt():
            return "blocked", "no review started: the host is stopping"
        if not self.may_review():
            return "blocked", HELD + WINDING
        if review is None or (review["task_id"], review["request_id"]) != (task["id"], task["request_id"]):
            key = FINDINGS[task["stage"]]
            state["review"] = dict(task_id=task["id"], review=len(task["reviews"]) + 1, request_id=task["request_id"],
                                   stage=task["stage"], round=task["round"], target=task[key],
                                   prior=[dict(id=item["id"], severity=item["severity"]) for item in task["required"][key]],
                                   attempts=[], running=None)
        elif len(review["attempts"]) >= REVIEW_TRIES:
            return self.finalize(view, state, task, "FAILED")
        return self.review_attempt(view, state, task)

    def handed(self, view, task, key):
        """For a repair round: the findings the builder was handed, in full, and what
        its last plan or build receipt says it did about each."""
        kept = task["findings"][key]
        findings = decode(view.artifact(kept))["findings"] if kept is not None else []
        phase = "plan" if key == "plan" else "build"
        last = next((item for item in reversed(task["receipts"])
                     if item["stage"] == phase and item["status"] == "satisfied"), None)
        if last is None:
            raise Refused("no consumed " + phase + " receipt holds the builder's answers")
        raw = view.read(last["path"])
        if receipts.digest(raw) != last["sha256"]:
            raise Refused("the consumed " + phase + " receipt changed since it was taken")
        return findings, decode(raw)["data"]["answers"]

    def review_attempt(self, view, state, task):
        """One run of the reviewer for the review in the state: its directory, its two
        output files, what the repository and receipts/ look like before it, and the
        process. Whatever keeps it from starting is a failed attempt, not an error."""
        review = state["review"]
        number = len(review["attempts"]) + 1
        seconds = int(min(self.review_limit, self.work_seconds()))
        cutoff = self.clock() + seconds  # fixed now: slow preparation shortens the review, never extends it
        name = review["request_id"] + "-" + str(number)
        log = "logs/review-" + task["id"] + "-" + str(review["review"]) + "-" + str(number)
        # Intent first: a crash after this line leaves a review the next host records as lost.
        review["running"] = dict(attempt=number, started=self.now().isoformat(), limit_seconds=seconds,
                                 directory=review_dir.FOLDER + "/" + name, marker=contract.nonce(),
                                 stdout=log + ".out", stderr=log + ".err")
        running = review["running"]
        self.save(view, state)
        # An earlier attempt's end is on disk with this one's intent: nothing of it is held.
        self.kind, self.reviewing, self.observed = "turn", None, None
        opened, made, sha = [], None, None
        try:
            if seconds < 1:
                raise Refused("no working time is left for a review")
            key, repo = FINDINGS[review["stage"]], self.admission.trust.repo
            findings, answers = self.handed(view, task, key) if review["prior"] else (None, None)
            plan = task["plan"]["artifact"] if review["stage"] == "review" and task["plan"] is not None else None
            # Under the coordination lock, which is short: what is left of it, less the
            # time the writes after it need. A repository that cannot be exported in
            # that time cannot be reviewed, and the attempt says so.
            made, sha = review_dir.make(
                view, name, kind=REVIEW[review["stage"]], target=review["target"], base=task["base"],
                brief=task["brief"], plan=plan, prior=review["prior"], findings=findings, answers=answers,
                nonce=running["marker"], seconds=view.budget.remaining() - 3 * WRITE_RESERVE)
            for path in (running["stdout"], running["stderr"]):
                # The host creates both files and keeps these descriptors for the whole
                # review; what it later reads back is read through them.
                with view.parent(path) as (parent, leaf):
                    opened.append(os.open(leaf, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o600, dir_fd=parent))
            before = review_dir.snapshot(repo, min(5, view.budget.remaining() - 2 * WRITE_RESERVE))
            marks = receipt_marks(view)
            cwd = os.path.join(str(self.shift), made)
            argv = self.admission.seat_start("reviewer", reviewer_prompt(cwd), cwd=cwd)
            if self.clock() >= cutoff or not self.may_review():
                raise Refused("the time for the review ran out while it was prepared")
            if self.stopped or self.halt():
                raise Refused("the host is stopping")
            self.turn_began, self.turn_over, self.turn_receipts = self.clock(), None, None
            self.kind = "review"
            # `printed` is what the host itself reads from the reviewer's two pipes. A
            # verdict is taken from these bytes alone: the logs are files in the shift,
            # which another seat can write to while the review runs.
            self.reviewing = dict(argv=[str(item) for item in argv], before=before, marks=marks, instructions=sha,
                                  printed=dict(stdout=bytearray(), stderr=bytearray()))
            self.child = self.spawn_review(argv, cwd=cwd, env=builder_env(self.shift, temp=self.temp_path()))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            for fd in opened:
                os.close(fd)
            self.kind, self.reviewing = "turn", None
            why = "not started: " + (str(exc)[:500] or type(exc).__name__)
            try:
                return self.attempt_done(view, state, task, dict(
                    attempt=number, directory=made, instructions_sha256=sha, marker=running["marker"], argv=[],
                    started=running["started"], ended=self.now().isoformat(), elapsed_seconds=None, exit_status=None,
                    ended_by="not started", stdout=None, stderr=None, output_dropped_bytes=0, status="failed",
                    verdict=None, reasons=[why], record=None, repository="not checked"))
            except Elapsed:
                # No process was started and none is held. The time under this lock ran
                # out before a receipt for the failed review could be recorded. Raised
                # further, this would block the whole shift as an effect of unknown
                # extent, which it is not; left begun, the review would be in no report
                # and known to no driver whose command this does not answer.
                review = state["review"]
                if review is None or review["running"] is not None or not review["attempts"]:
                    raise
                return self.failed_unrecorded(state, task, number, why[len("not started: "):])
        self.turn_end, self.turn_fd, self.err_fd = cutoff, opened[0], opened[1]
        self.turn_bytes = self.turn_dropped = self.err_bytes = self.err_dropped = 0
        self.pipe_end = {}
        self.stopping = self.observed = None
        for name in ("stdout", "stderr"):
            stream = getattr(self.child, name, None)
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
        return "recorded", ("review of the " + REVIEW[review["stage"]] + " started by the host (attempt " + str(number)
                            + " of at most " + str(REVIEW_TRIES) + ", limit " + str(seconds) + " seconds); wait for it")

    def review_ended(self, view, state, outcome, code, reaped, dropped, seen):
        """The running review attempt is over: say what it gave. A verdict is taken only
        from a reviewer that ended on its own with status 0, whose whole output the
        host kept and can read back, whose object keeps the contract, and after
        which the repository and receipts/ are as they were before it."""
        review = state["review"]
        running, task, held = review["running"], state["tasks"][review["task_id"]], self.reviewing or {}
        if held.get("final") is not None and all(
                held["final"]["value"][name] == wanted for name, wanted in
                (("task_id", task["id"]), ("request_id", review["request_id"]), ("round", task["round"]))):
            # An earlier try under a lock that ran out had come as far as the receipt and
            # could not save the state. What it built is finished, and nothing of the
            # end is worked out a second time: its own receipt file is in receipts/ now.
            data = held["final"]["value"]["data"]
            review["attempts"], review["running"] = data["attempts"], None
            return self.finalize(view, state, task, data["outcome"])
        reasons, result, kept = [], None, {}
        if str(outcome).startswith(LATE):
            reasons.append("the reviewer was first seen to have ended after its signed limit, so it is not known "
                           "to have ended within it")
        elif outcome != "exit" or code != 0:
            reasons.append("the reviewer did not end on its own with status 0 (" + str(outcome)[:300] + ", status "
                           + str(code) + ")")
        lost = held.get("dropped", dropped) or 0
        if lost:
            reasons.append("the reviewer printed more than the host keeps: " + str(lost) + " bytes were dropped")
        for name, path in (("out", running["stdout"]), ("err", running["stderr"])):
            if held.get(name) is not None:
                kept[name] = dict(path=path, sha256=receipts.digest(held[name]))
        if held.get("altered"):
            # The receipt names the logs as they are; they back no verdict.
            reasons.append("what is in the review's log (" + ", ".join(held["altered"]) + ") is not what the "
                           "reviewer printed: the log was written to by something else while the review ran")
        if held.get("undrained"):
            reasons.append("what the reviewer printed could not be read to its end")
        if "err" not in kept:
            # Both logs back the attempt, an empty one too; a verdict beside a log that
            # could not be verified is not taken.
            reasons.append("what the reviewer printed on stderr could not be read back")
        if "out" not in kept or held.get("said") is None:
            reasons.append("what the reviewer printed could not be read back")
        else:
            try:
                result = contract.check(held["said"].decode("utf-8", "replace"), running["marker"], review["target"],
                                        review["prior"])
                reasons += result["reasons"]
            except Refused as exc:
                reasons.append("the review could not be checked: " + str(exc)[:300])
        repository = "not checked"
        try:
            after = review_dir.snapshot(self.admission.trust.repo, min(5, view.budget.remaining() - 2 * WRITE_RESERVE))
            marks = receipt_marks(view)
            if held.get("before") is not None and held.get("marks") is not None and marks is not None:
                repository = "unchanged" if (after, marks) == (held["before"], held["marks"]) else "changed"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        if repository == "changed":
            reasons.append("the repository or the shift's receipts/ changed while the review ran")
        elif repository != "unchanged":
            reasons.append("whether the review changed the repository or receipts/ could not be checked")
        complete = not reasons and result is not None and result["status"] == "complete"
        elapsed = seen[0] if seen else None
        return self.attempt_done(view, state, task, dict(
            attempt=running["attempt"], directory=running["directory"], instructions_sha256=held.get("instructions"),
            marker=running["marker"], argv=held.get("argv", []), started=running["started"],
            ended=self.now().isoformat(), elapsed_seconds=elapsed if type(elapsed) is int else None,
            exit_status=code if type(code) is int and -255 <= code <= 255 else None,
            ended_by=str(outcome)[:600] or "unknown", stdout=kept.get("out"), stderr=kept.get("err"),
            output_dropped_bytes=lost, status="complete" if complete else "failed",
            verdict=result["verdict"] if complete else None, reasons=[item[:600] for item in reasons[:8]],
            record=result["record"] if complete else None, repository=repository))

    def attempt_done(self, view, state, task, entry):
        """Keep one attempt of the review. A complete one settles the review. A failed
        one is run once more when the host is not stopping and a review may still
        start; otherwise the review has failed."""
        review = state["review"]
        review["attempts"].append(entry)
        review["running"] = None
        if entry["status"] == "complete":
            return self.finalize(view, state, task, entry["verdict"])
        # The next attempt is prepared under this same lock: with too little of it left
        # that attempt could only fail too, so the review has failed once and `continue`
        # runs it on under a lock of its own.
        if (len(review["attempts"]) < REVIEW_TRIES and state["mode"] == "active" and not self.stopped
                and not self.halt() and not self.guard() and self.may_review()
                and self.room(view) >= PREPARE_ROOM):
            return self.review_attempt(view, state, task)
        return self.finalize(view, state, task, "FAILED")

    @staticmethod
    def room(view):
        """Seconds left under the coordination lock."""
        return view.budget.remaining()

    def outstanding(self, view, task, review, key):
        """What a CHANGES review leaves for the builder to answer: every earlier finding
        the reviewer says is not resolved, as it was first written and with the
        reviewer's reason, and every new finding. The worst first, at most OUTSTANDING."""
        record, old = review["attempts"][-1]["record"], {}
        try:
            if task["findings"][key] is not None:
                old = {item["id"]: item for item in decode(view.artifact(task["findings"][key]))["findings"]}
        except (OSError, ValueError, KeyError, TypeError):
            old = {}  # the earlier text is gone; the id, severity and the reviewer's reason are still handed on
        severity = {item["id"]: item["severity"] for item in review["prior"]}
        items = []
        for answer in record["prior"]:
            if not answer["resolved"]:
                was = old.get(answer["id"]) if isinstance(old.get(answer["id"]), dict) else {}
                items.append(dict({name: was.get(name, "see the earlier review") for name in
                                   ("location", "claim", "evidence", "recommendation")},
                                  id=answer["id"], severity=severity[answer["id"]], earlier=True,
                                  reviewer_reason=answer["reason"]))
        items += [dict(item, earlier=False, reviewer_reason=None) for item in record["findings"]]
        items.sort(key=lambda item: SEVERITY[item["severity"]])
        return items[:OUTSTANDING], len(items[OUTSTANDING:])

    def finalize(self, view, state, task, outcome):
        """The review is over: the host writes its receipt from what it ran and read,
        has it checked and recorded as any receipt is, and moves the task. ACCEPT
        advances; CHANGES waits for the driver's `repair` with the findings in a
        file; a review that failed twice blocks the task and not the shift.

        The receipt is built once and kept in this process (`reviewing["final"]`)
        until the state is saved. When the time under the lock runs out anywhere
        after that, the next try finishes that same receipt: a file already
        written is not written again, a slot already recorded is not recorded
        again, and both must be exactly what this process built. A receipt on
        record for this review that this process did not build backs nothing.
        A host process started later holds nothing of this and takes nothing up:
        for it the review is lost."""
        review = state["review"]
        stage, key, what = review["stage"], FINDINGS[review["stage"]], REVIEW[review["stage"]]
        tried = len(review["attempts"])
        summary = dict(review=review["review"], stage=stage, round=task["round"], request_id=review["request_id"],
                       target=review["target"], outcome=outcome, receipt=None, findings=[],
                       attempts=[{name: item[name] for name in ("attempt", "status", "verdict", "ended_by", "reasons",
                                                                "started", "ended", "elapsed_seconds", "repository")}
                                 for item in review["attempts"]])
        failures = "; ".join(item["reasons"][0] for item in review["attempts"] if item["reasons"])[:1200]

        def done(status, detail):
            summary["detail"] = detail[:2000]
            task["reviews"].append(summary)
            state["last_review"] = dict(task_id=task["id"], stage=stage, round=summary["round"], outcome=outcome,
                                        receipt=summary["receipt"], findings=summary["findings"], detail=detail[:2000])
            state["review"] = None
            return status, detail

        try:
            expected = self.review_expected(task)
            published = receipts.encoded(expected)
            path = "briefs/expected-" + task["request_id"] + ".json"
            view.atomic(path, published, replace=True)
            held = self.reviewing if isinstance(self.reviewing, dict) else {}
            if held.get("final") is None:
                held["final"] = dict(value=dict(
                    version=3, receipt_id="review-" + review["request_id"][:16] + "-" + uuid.uuid4().hex[:8],
                    shift_id=self.shift.name, task_id=task["id"], request_id=task["request_id"], role="reviewer",
                    phase="review", round=task["round"], brief=task["brief"],
                    data=dict(target=review["target"], base=task["base"], implementer_family=self.family,
                              reviewer=self.reviewer, limit_seconds=self.review_limit, outcome=outcome,
                              prior=review["prior"], attempts=review["attempts"])))
            value = held["final"]["value"]
            name, sha = "receipts/" + value["receipt_id"] + ".json", receipts.digest(receipts.encoded(value))
            slot = receipts.phase_slot(value)
            found = next((item for item in receipts.ledger(view)["entries"] if item["slot"] == slot), None)
            if found is not None:
                # Recorded already. Only by an earlier try of this process, or it backs nothing.
                if ((found["receipt_id"], found["sha256"], found["satisfies_phase"])
                        != (value["receipt_id"], sha, outcome == "ACCEPT") or receipts.digest(view.read(name)) != sha):
                    raise Refused("a receipt that this host did not write is on record for this review")
                wrote = dict(path=name, sha256=sha)
                result = dict(sha256=sha, status="satisfied" if found["satisfies_phase"] else "blocked")
            else:
                try:
                    there = view.read(name)
                except FileNotFoundError:
                    wrote = receipts.written(view, value, path, host=True)
                else:
                    # Written by an earlier try that could not record it.
                    if receipts.digest(there) != sha:
                        raise Refused("the review receipt changed since this host wrote it")
                    wrote = dict(path=name, sha256=sha)
                # Never longer than this lock still has, less what the writes after it need.
                result = receipts.validate_receipt(self.shift, wrote["path"], path, record=True,
                                                  seconds=min(10, self.work_seconds() or self.final_seconds(),
                                                              max(0.05, self.room(view) - 2 * WRITE_RESERVE)),
                                                  expected_sha256=receipts.digest(published))
                if result["sha256"] != wrote["sha256"] or not result["recorded"] or (
                        (outcome == "ACCEPT") != (result["status"] == "satisfied" and result["satisfies_phase"])):
                    raise Refused("the receipt check gave " + result["status"] + ": " + "; ".join(result["reasons"])[:500])
        except Elapsed:
            raise  # no time is left under this lock: nothing more can be written here either
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            # Without its receipt on record a review backs nothing, whatever it said.
            outcome = summary["outcome"] = "FAILED"
            task.update(stage="blocked", reason=("the review's receipt could not be written or recorded, so the "
                                                 "review backs nothing: " + str(exc)[:1000]))
            state["active"] = None
            return done("blocked", task["reason"])
        entry = dict(path=wrote["path"], sha256=result["sha256"], stage=stage, round=task["round"], status=result["status"])
        task["receipts"].append(entry)
        summary["receipt"] = dict(path=entry["path"], sha256=entry["sha256"])
        if outcome == "ACCEPT":
            task["required"][key], task["findings"][key], task["reason"] = [], None, ""
            if stage == "plan_review":
                self.stage(task, "build")
            else:
                task["accepted"] = dict(candidate=task["candidate"], receipt=entry)
                task["stage"] = "accepted"
            return done("advanced", "the reviewer accepted the " + what + "; " + task["stage"])
        if outcome == "CHANGES":
            items, left = self.outstanding(view, task, review, key)
            raw = receipts.encoded(dict(version=1, task_id=task["id"], stage=stage, round=task["round"],
                                        target=review["target"], review_receipt=entry["path"], findings=items,
                                        not_listed=left))
            kept = "briefs/findings-" + review["request_id"] + ".json"
            view.atomic(kept, raw, replace=True)
            summary["findings"] = [dict(id=item["id"], severity=item["severity"]) for item in items]
            task["findings"][key] = dict(path=kept, sha256=receipts.digest(raw))
            task["required"][key] = list(summary["findings"])
            names = ", ".join(item["id"] + " (" + item["severity"] + ")" for item in items)[:800]
            task.update(stage="repair_needed", repair_from=stage,
                        reason="the reviewer asks for changes to the " + what + ": " + names)
            return done("blocked", task["reason"] + ". The findings are in " + kept + "; send repair, and the "
                        "builder's next receipt answers each of them")
        if tried >= REVIEW_TRIES:
            task.update(stage="blocked", reason="review failed twice: " + failures)
            state["active"] = None
            return done("blocked", task["reason"] + ". The task is blocked for the developer; no repair round was used")
        # Failed once and not run again: the work stays handed in. A later review of it
        # is another review, with a request of its own.
        self.stage(task, stage)
        return done("blocked", "review failed (" + failures + ") and was not run again: the host is stopping or "
                    "the review no longer fits in the working time left. The work stays handed in and not reviewed")

    def review_lost(self, state):
        """A review recorded as running by an earlier host process: nothing is known of
        its end and nothing is taken from it."""
        self.closed_without_receipt(state, "lost", LOST_REVIEW)

    def closed_without_receipt(self, state, outcome, detail):
        """Move the review in the state onto its task with no receipt: it backs nothing."""
        review = state["review"]
        task = state["tasks"][review["task_id"]]
        task["reviews"].append(dict(
            review=review["review"], stage=review["stage"], round=review["round"], request_id=review["request_id"],
            target=review["target"], outcome=outcome, receipt=None, findings=[], detail=detail,
            attempts=[{name: item[name] for name in ("attempt", "status", "verdict", "ended_by", "reasons", "started",
                                                     "ended", "elapsed_seconds", "repository")}
                      for item in review["attempts"]]))
        state["last_review"] = dict(task_id=task["id"], stage=review["stage"], round=review["round"], outcome=outcome,
                                    receipt=None, findings=[], detail=detail)
        state["review"] = None

    def failed_unrecorded(self, state, task, number, why):
        """A review whose last attempt could not start, with no time left under the lock
        to record a receipt for it. It is kept as failed with no receipt, as a review
        whose receipt could not be written is, and the task moves as after any failed
        review: blocked after two attempts, and otherwise handed in still, for a review
        with a request of its own. Returns the status and words for the driver."""
        review = state["review"]
        stage, tried = review["stage"], len(review["attempts"])
        failures = "; ".join(item["reasons"][0] for item in review["attempts"] if item["reasons"])[:1200]
        detail = ("review attempt " + str(number) + " could not start (" + why + ") and the time under the host's "
                  "lock ran out before a receipt for the review could be recorded. The review is kept as failed "
                  "with no receipt and backs nothing. ")
        if tried >= REVIEW_TRIES:
            task.update(stage="blocked", reason="review failed twice: " + failures)
            state["active"] = None
            detail += "The task is blocked for the developer; no repair round was used"
        else:
            self.stage(task, stage)
            detail += ("The work stays handed in and not reviewed. Send `continue` at this stage: it starts "
                       "another review when one may still start")
        self.closed_without_receipt(state, "FAILED", detail[:2000])
        return "blocked", detail[:2000]

    def pump(self):
        """Move what the owned process printed into its log, up to a fixed size; the rest
        is read and dropped so the process never blocks on a full pipe. One call reads a
        bounded amount, so output can never keep the host from its other checks. A
        review has a second pipe and file for its error output."""
        self.turn_bytes, self.turn_dropped = self.drain("stdout", self.turn_fd, self.turn_bytes, self.turn_dropped)
        if self.kind == "review":
            self.err_bytes, self.err_dropped = self.drain("stderr", self.err_fd, self.err_bytes, self.err_dropped)

    def drain(self, name, fd, kept, dropped):
        """`pipe_end[name]` says how this read of the pipe ended: "end" when the pipe gave
        its end, which it does once nothing holds it open for writing; "nothing" when
        nothing was there to read, which is not the end; "more" when the pass's reads
        were used up. A process without that pipe has printed all it can: "end"."""
        stream = getattr(self.child, name, None)
        if stream is None:
            self.pipe_end[name] = "end"
            return kept, dropped
        if stream.closed:
            return kept, dropped
        self.pipe_end[name] = "more"
        for _ in range(PUMP_READS):
            try:
                chunk = os.read(stream.fileno(), 65536)
            except BlockingIOError:
                self.pipe_end[name] = "nothing"
                break
            if not chunk:
                self.pipe_end[name] = "end"
                break
            keep, written = chunk[:max(0, TURN_LOG_LIMIT - kept)], 0
            if keep and fd is not None:
                try:
                    written = os.write(fd, keep)
                except OSError:
                    pass
            kept += written
            dropped += len(chunk) - written
            if written and self.kind == "review" and self.reviewing is not None and "printed" in self.reviewing:
                self.reviewing["printed"][name] += keep[:written]
        return kept, dropped

    def ended(self, state, outcome, code, reaped, dropped, seen):
        """Move the running turn onto its task with how it ended. Never acceptance.

        `seen` is what this host observed of a turn it ran itself: the time from
        just before the start until it saw the process over (or stopped it), and
        whether the listing of receipts/ differed between the start and the end.
        Either is null when not observed. A difference is not a valid receipt and
        equal listings do not prove nothing was written. `request_id` is the
        phase the turn ran in: it cannot change while a turn runs."""
        turn = state["turn"]
        elapsed, changed = seen or (None, None)
        task = state["tasks"][turn["task_id"]]
        task["turns"].append(dict(
            turn, ended=self.now().isoformat(), outcome=outcome, exit=code, reaped=reaped,
            output_dropped_bytes=dropped, elapsed_seconds=elapsed, receipts_changed=changed,
            request_id=task["request_id"]))
        state["turn"] = None

    def settle(self, state, view=None):
        """Close a recorded turn or review that has no live process handle here. True
        when it was lost: recorded by an earlier host process, so whether its process
        still runs is unknown and nothing is signaled. The caller must then stop
        starting builders and reviews; the developer has to look. `view` is the
        shift under the coordination lock; a review's end is written through it."""
        if self.child is not None:
            return False
        if state["review"] is not None:
            if state["review"]["running"] is None:
                return False
            if self.observed is not None and self.kind == "review" and view is not None:
                self.review_ended(view, state, *self.observed)
                return False
            self.review_lost(state)
            self.lost_words = LOST_REVIEW
            return True
        if state["turn"] is None:
            return False
        if self.observed is not None:
            # This host saw the end but could not write it then. It is kept until a
            # save succeeds (see save), so another failed write does not lose it.
            self.ended(state, *self.observed)
            return False
        self.ended(state, "lost", None, False, 0, None)
        return True

    def lost(self, state):
        state.update(mode="blocked", next_owner="developer", reason=self.lost_words)

    def poll_turn(self, stop=None, record=True):
        """See the end of the owned builder turn: its own exit, its time limit, or a
        host stop; and, with `record`, write it (see `record_turn`).

        `stop` names why the host is ending it now. The group is signaled before
        any output is drained. While the group cannot be confirmed stopped the
        handle is kept, the turn stays recorded as running and no new turn can
        start; only a host stop records it anyway, marked as not reaped. At its
        own stop the host passes `record=False`: it stops the driver next and
        writes the turn's end after that, so no wait for a lock stands between
        the two stops.
        """
        from .tick_supervise import stop_group
        if stop is not None:
            self.stopped = True
        child = self.child
        if child is None:
            return
        code = child.poll()
        if self.stopping is None:
            if code is None and stop is None and self.clock() < self.turn_end:
                self.pump()
                return
            # A review that is over when the host looks after its limit is not known to
            # have ended within it: never an ordinary exit, whatever it printed.
            late = code is not None and self.kind == "review" and self.clock() >= self.turn_end
            self.stopping = "timeout" if code is None else LATE if late else "exit" if code >= 0 else "signal"
            self.turn_over = self.clock()  # before the stop, the drain and anything else below
            first = True
        else:
            first = False
        # What was seen of the group is fixed first; nothing below can change or lose it.
        errors = []
        wait = 1.0 if first or stop else 0.1
        if stop is None:
            # While serving, never past the stop: a group not confirmed gone by then
            # is taken up again at the host's own stop, which gives it its second.
            wait = min(wait, self.work_seconds())
        try:
            reaped = stop_group(child, wait)
        except (OSError, ValueError) as exc:
            reaped = False
            errors.append("builder stop failed: " + str(exc)[:200])
        self.ending = (stop, reaped, errors)
        if record:
            self.record_turn(live=stop is None)

    def close_turn(self):
        """After the group was signaled: drain what the builder printed into its log,
        close both ends and keep what was seen in `observed`. Writes to the log, so
        at its own stop the host does this only after the driver was stopped."""
        if self.ending is None or self.child is None:
            return
        child, (stop, reaped, errors) = self.child, self.ending
        self.ending = None
        review = self.kind == "review" and self.reviewing is not None
        try:
            self.pump()
            if review and (reaped or stop is not None):
                # The last look at a review's pipes: on, in bounded passes, while a pass
                # ended only because its reads were used up.
                for _ in range(LAST_PUMPS):
                    if "more" not in self.pipe_end.values():
                        break
                    self.pump()
        except (OSError, ValueError) as exc:
            errors.append("builder output drain failed: " + str(exc)[:200])
            if review:
                self.reviewing["undrained"] = True  # what was printed is not known to be whole
        if not reaped and stop is None:
            return
        if review and any(self.pipe_end.get(name) != "end" for name in ("stdout", "stderr")):
            # The end of a pipe was not seen: something still holds it open, or more was
            # printed than these passes read. What was read is not known to be whole,
            # and a verdict in it is not taken.
            self.reviewing["undrained"] = True
        outcome = (stop if self.stopping == "timeout" else self.stopping) if stop else self.stopping
        if not reaped:
            outcome += "; stop not confirmed"
            self.unconfirmed = True
            if type(getattr(child, "pid", None)) is int and len(self.leftover_groups) < 4:
                self.leftover_groups.append(child.pid)
        if self.kind == "review" and self.reviewing is not None:
            # Read back through the descriptors the host opened: whatever a seat put at
            # those paths since is not what is read. What is read back is what the
            # receipt names; it must be, byte for byte, what the host took from the
            # pipes, and the verdict is worked out from that (`said`), never from a file.
            printed = self.reviewing.pop("printed", None) or {}
            for name, pipe, fd in (("out", "stdout", self.turn_fd), ("err", "stderr", self.err_fd)):
                try:
                    self.reviewing[name] = None if fd is None else os.pread(fd, TURN_LOG_LIMIT + 1, 0)
                except OSError as exc:
                    errors.append("review output not read back: " + str(exc)[:200])
                    self.reviewing[name] = None  # no evidence for this log: the attempt fails on it
                    continue
                if self.reviewing[name] is not None and self.reviewing[name] != bytes(printed.get(pipe, b"")):
                    self.reviewing.setdefault("altered", []).append(pipe)
            self.reviewing["said"] = bytes(printed["stdout"]) if "stdout" in printed else None
            self.reviewing["dropped"] = self.turn_dropped + self.err_dropped
        for name in ("stdout", "stderr"):
            stream = getattr(child, name, None)
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError) as exc:
                errors.append("builder output not closed: " + str(exc)[:200])
        for name in ("turn_fd", "err_fd"):
            try:
                if getattr(self, name) is not None:
                    os.close(getattr(self, name))
            except OSError as exc:
                errors.append("builder log not closed: " + str(exc)[:200])
            setattr(self, name, None)
        # Kept for the closeout however the turn ended; `reaped` stays what was seen of the group.
        self.cleanup_errors += errors[:max(0, 4 - len(self.cleanup_errors))]
        over = self.clock() if self.turn_over is None else self.turn_over
        elapsed = None if self.turn_began is None else int(max(0, over - self.turn_began))
        # What was seen of the process is kept first. The receipts listing is an
        # extra taken under the lock in `record_turn`; without it the turn is still recorded.
        self.observed = (outcome, child.poll(), reaped, self.turn_dropped, (elapsed, None))
        self.child, self.stopping = None, None
        self.turn_seen = (self.turn_receipts, elapsed)

    def record_turn(self, live=False):
        """Finish what `poll_turn` began (`close_turn`) and write the turn's end.
        `live` is the serving loop: the lock is waited for no longer than the
        working time, and is not asked for once that is over, also when it runs
        out on the way to the lock. The end is then kept in `observed` and
        written at the host's stop, after the driver was stopped, or by `finish`."""
        self.close_turn()
        if self.observed is None or self.child is not None:
            return
        if self.final_seconds() <= 0 or (live and self.work_seconds() <= 0):
            return
        before, elapsed = self.turn_seen
        try:
            with self.coordination(local_only=True, live=live, serving=live) as view:
                state = self.load(view)
                if state["review"] is not None and state["review"]["running"] is not None and self.kind == "review":
                    self.review_ended(view, state, *self.observed)
                    self.save(view, state)
                elif state["turn"] is not None:
                    # The record of the end comes first: the listing gets only time left
                    # above what the write is kept, and is skipped when there is none.
                    room = min(0.25, view.budget.remaining() - WRITE_RESERVE)
                    after = None if before is None or room <= 0 else receipt_marks(view, seconds=room)
                    if after is not None:
                        self.observed = self.observed[:4] + ((elapsed, before != after),)
                    self.ended(state, *self.observed)
                    self.save(view, state)
                else:
                    self.observed = None
        except Elapsed:
            if not live or self.work_seconds() > 0:
                raise
            # Out of working time at the lock: kept, and written at the host's stop.

    @staticmethod
    def unusable(why):
        return ("this command is not usable as written: " + why[:1500] + ". Nothing changed: the task is at the "
                "same stage and round. Correct the command and send it again under a new id; the builder has "
                "nothing to redo.")

    def consume(self, view, state, task, command, stop=False):
        before = task["stage"]
        if not stop and before in REVIEW:
            return "invalid", ("the host runs this review and takes its receipt itself; wait, or send continue "
                               "when none is running")
        if not stop and before not in PHASE:
            return "invalid", "not awaiting a phase receipt"
        # What the driver wrote in this command is checked first. A mistake there is
        # the driver's to correct: the task keeps its stage and its repair count,
        # and no builder turn is needed.
        try:
            if stop:
                expected = self.stop_expected(task)
            else:
                target, kind = command.get("target"), {"plan": "plan", "build": "commit"}.get(before)
                if target is not None and kind is None:
                    raise Refused("an acknowledgment has no target, and a target cannot be rebound")
                # The shape check below takes either kind of target, or none. Which one
                # this phase needs is the driver's to get right as well.
                if kind and (not isinstance(target, dict) or target.get("kind") != kind):
                    raise Refused("consuming a " + before + " receipt needs a target of kind " + kind
                                  + (" naming the plan file by path and sha256" if kind == "plan"
                                     else " naming the full commit ID"))
                expected = self.expected(task, command.get("target"))
            validate_shape(expected, "expected")
            receipts.relative(command["receipt"])
            # The validator takes a receipt only under its receipt-ID filename. Another
            # file of the shift named here is a mistyped reference, not a returned receipt.
            if not RECEIPT_PATH.fullmatch(command["receipt"]):
                raise Refused("a receipt is a file directly under receipts/, named <receipt_id>.json; this path is not one")
        except (Refused, KeyError, TypeError) as exc:
            return "invalid", self.unusable(str(exc))
        try:
            try:
                raw = view.read(command["receipt"])
            except FileNotFoundError:
                # A path the driver mistyped, or a receipt the builder has not written:
                # neither is a returned receipt.
                return "invalid", self.unusable("no file at " + command["receipt"][:200])
            except (Refused, OSError) as exc:
                # There, but not a file the host will read (a link, a directory, too
                # large): the reference is unusable, which is not a returned receipt.
                return "invalid", self.unusable("the receipt path cannot be read as a file of the shift ("
                                                + str(exc)[:300] + ")")
            decode(raw)
            path = "briefs/expected-" + task["request_id"] + ("-stop" if stop else "") + ".json"
            published = receipts.encoded(expected)
            try:
                view.atomic(path, published, replace=True)
                self.require_live()
            except (Refused, OSError) as exc:
                raise Uncertain(str(exc)) from exc
            seconds = self.step_seconds()
            if seconds <= 0:
                raise Unusable("working time over: the host is stopping and the receipt was not checked")
            sha = receipts.digest(raw)

            def held():
                return len(receipts.ledger(view)["entries"])
            try:
                had = held()
            except (Refused, OSError, KeyError, TypeError):
                had = None  # the check below fails on the same ledger, before it writes
            # The published file is builder-writable; bind validation to the context built here.
            try:
                result = receipts.validate_receipt(self.shift, command["receipt"], path, record=True,
                                                  seconds=seconds, expected_sha256=receipts.digest(published))
            except (Refused, OSError, KeyError, TypeError) as exc:
                # The validator writes the ledger and can fail after that write landed.
                # Only a ledger that can be read and has gained no entry (of these bytes or
                # of others put in their place) shows that nothing was recorded; anything
                # else is an effect of unknown extent.
                try:
                    now = held()
                except (Refused, OSError, KeyError, TypeError) as unread:
                    raise Uncertain("the receipt check failed (" + str(exc)[:500] + ") and the ledger could not "
                                    "be read afterwards (" + str(unread)[:300] + ")") from exc
                if now != had:
                    raise Uncertain("the receipt check failed after the ledger took the receipt: " + str(exc)[:800]) from exc
                if isinstance(exc, Elapsed) and self.serving and self.work_seconds() <= 0:
                    # Out of time, with nothing recorded: not something the builder has to repair.
                    raise Unusable("working time over: the host is stopping and the receipt was not checked") from exc
                raise
            try:
                self.require_live()  # the ledger may hold this receipt by now
            except Refused as exc:
                raise Uncertain(str(exc)) from exc
            if result["sha256"] != sha:
                if result.get("recorded"):
                    # The ledger slot went to bytes that are not the ones read here.
                    raise Uncertain("the receipt changed while it was checked, after the ledger took it")
                raise Refused("receipt changed between observation and consumption")
            task["receipts"].append(dict(path=command["receipt"], sha256=result["sha256"],
                                         stage="stop" if stop else before, round=task["round"], status=result["status"]))
            if not (result["status"] == "satisfied" and result["satisfies_phase"] and result["recorded"]):
                raise Refused("receipt did not satisfy phase: " + result["status"] + "; " + "; ".join(result["reasons"]))
            if stop:
                task["cessation"] = task["receipts"][-1]
                return "recorded", "builder seat cessation evidence recorded"
            task["reason"] = ""  # the reason of an earlier repair is not the reason for anything after it
            if before == "ack":
                task["acknowledged"] = True
                self.stage(task, "plan" if task["kind"] == "feature" else "build")
            elif before == "plan":
                task["plan"] = expected["target"]
                self.stage(task, "plan_review")
            else:
                task["candidate"] = expected["target"]
                self.stage(task, "review")
        except (Uncertain, Unusable):
            raise  # an incomplete effect, or nothing changed and nothing to repair; see `apply`
        except (Refused, OSError, KeyError, TypeError) as exc:
            if not stop:
                task.update(stage="repair_needed", repair_from=before, reason=str(exc)[:2000])
            return "blocked", str(exc)
        if task["stage"] not in REVIEW:
            return "advanced", "bound receipt recorded; " + task["stage"]
        # The receipt is taken and stays taken. The review of what it handed in is the
        # host's own work and begins here, outside the checks of the receipt above.
        stage = task["stage"]
        status, detail = self.start_review(view, state, task)
        if status == "recorded" or task["stage"] == stage:
            return "advanced", "bound receipt recorded; " + stage + "; " + detail
        return status, "bound receipt recorded; " + detail

    def dispatch(self, view, state, command):
        op = command["op"]
        if op in ("brief", "repair"):
            self.require_live()
            if self.active_seconds() <= 0:
                # Refused, and the shift goes on: what is already handed in can still be taken.
                return "blocked", "wind-down: no new brief or repair. " + WINDING
        if op == "brief":
            with asked():
                identity = channels.token(command["task_id"])
                if state["active"] is not None or identity in state["tasks"] or len(state["tasks"]) >= 128:
                    raise Refused("one task at a time; task identities cannot be rebriefed")
                if command["kind"] not in ("feature", "defect"):
                    raise Refused("unknown task kind")
                owner = channels.text(command["next_owner"], 200)
                view.artifact(command["brief"])
                # Where the repository is now: a review is of the task's whole change from here.
                base = review_dir.head(self.admission.trust.repo, min(5, view.budget.remaining()))
            task = self.new_task(identity, command["kind"], command["brief"], owner)
            task["base"] = base
            state["tasks"][identity], state["active"] = task, identity
            seconds = self.turn_limit(command)
            self.save(view, state)
            return self.builder_turn(view, state, task, builder_prompt(
                self.shift, "Task brief " + task["brief"]["path"] + " SHA256 " + task["brief"]["sha256"]
                + ". Read current-context.json for the bound request and round."), seconds)
        if op == "finish":
            with asked():
                reason, owner = channels.text(command["reason"]), channels.text(command["next_owner"], 200)
                # Given: what the driver says the direction held and it never briefed, an
                # empty list for nothing. Not given: the driver said nothing about it.
                left = channels.unstarted(command["not_started"]) if "not_started" in command else None
            state.update(mode="closed", reason=reason, next_owner=owner, not_started=left)
            return "closed", state["reason"]
        if op == "note":
            # The driver's own statement for the report, under the kind it gives it: a
            # decision, or a question. The host judges neither. Taken whenever
            # commands are, bound to no phase, and it changes nothing but this list.
            with asked():
                if command["kind"] not in channels.NOTE_KINDS:
                    raise Refused("kind is decision or question")
                said = channels.text(command["text"], channels.NOTE_TEXT)
                # A task_id of null is a note about no task, as when it is left out.
                about = channels.token(command["task_id"]) if command.get("task_id") is not None else None
                if about is not None and about not in state["tasks"]:
                    raise Refused("task_id names no task briefed in this shift; leave it out for a note about no task")
            if len(state["notes"]) >= channels.NOTES:
                # Not `invalid`: that answer asks for the command again under a new id.
                return "blocked", ("32 notes are kept already and no more are taken in this shift; nothing changed. "
                                   "Send no further note; what is left to say goes in the reason of your finish")
            state["notes"].append(dict(kind=command["kind"], text=said, task_id=about,
                                       at=self.now().replace(microsecond=0).isoformat()))
            return "recorded", "note kept for the report; nothing else changed, and a note gets no answer in the shift"
        task = self.task(state, command)
        if op == "continue":
            if task["stage"] in REVIEW:
                return self.start_review(view, state, task)
            if task["stage"] not in PHASE:
                return "blocked", "no builder phase is open; stage is " + task["stage"]
            last = task["turns"][-1] if task["turns"] else {}
            if (state["turn"] is None and self.child is None and command.get("again") is not True
                    and last.get("receipts_changed") is True and last.get("request_id") == task["request_id"]):
                # A turn with nothing to do: what the last one handed in is still there.
                return "blocked", (
                    "builder turn " + str(last.get("turn")) + " of this phase changed receipts/ and nothing has "
                    "been consumed since. No turn was started. Consume what it handed in: a `consume` you published "
                    "before this command and that was never answered was replaced by it, so publish it again "
                    "under a new id. Only if what the turn wrote is not this phase's receipt, send `continue` "
                    'with "again": true.')
            return self.builder_turn(view, state, task, builder_prompt(
                self.shift, "Read current-context.json and continue task " + task["id"] + " at stage "
                + task["stage"] + " round " + str(task["round"])
                + ". Do not restart the task or reset its repair count."), self.turn_limit(command))
        if state["turn"] is not None and op in ("consume", "repair", "close"):
            return "blocked", "a builder turn is running; wait for it to end"
        if state["review"] is not None and op in ("consume", "repair", "close"):
            return "blocked", ("a review is running; wait for it to end" if state["review"]["running"] is not None
                               else "a review was begun and is not finished; send continue to run it on")
        if op in ("consume", "stop-evidence"):
            return self.consume(view, state, task, command, op == "stop-evidence")
        if op == "repair":
            with asked():
                if task["stage"] != "repair_needed":
                    raise Refused("repair requires a returned verdict or receipt; the stage is " + task["stage"])
                reason, owner = channels.text(command["reason"]), channels.text(command["next_owner"], 200)
            task.update(reason=reason, next_owner=owner)
            if task["round"] == 3:
                task.update(stage="blocked", reason="three repairs exhausted: " + reason)
                state["active"] = None
                return "blocked", task["reason"]
            task["round"] += 1
            restart = ("ack" if task["repair_from"] == "ack" else
                       "plan" if task["repair_from"] in ("plan", "plan_review") else "build")
            self.stage(task, restart)
            task["candidate"] = None
            if restart == "plan":
                task["plan"] = None
            return "advanced", "repair round " + str(task["round"])
        if op == "progress":
            with asked():
                view.artifact(command["evidence"])
                if command["evidence"] not in task["progress"] and len(task["progress"]) >= 128:
                    raise Refused("progress limit reached")
            if command["evidence"] in task["progress"]:
                return "replayed", "same progress artifact"
            task["progress"].append(command["evidence"])
            task.update(last_progress=self.moment(), generation=task["generation"] + 1)
            return "recorded", "progress is not acceptance"
        if task["stage"] != "accepted":
            raise Unusable("close requires local acceptance of the candidate; the stage is " + task["stage"])
        task["stage"], state["active"] = "closed", None
        return "closed", "locally accepted; no remote delivery claim"

    def apply(self, command, *, serving=False):
        """Take one driver command. `serving` is the running host taking it from the
        channel: it is given no more than the working time and none after it."""
        intent = sha = None
        self.serving = serving
        try:
            if not isinstance(command, dict) or command.get("op") not in COMMANDS:
                raise Refused("unknown driver command")
            required, optional = COMMANDS[command["op"]]
            channels.fields(command, {"version", "id", "op"} | required, optional)
            if type(command["version"]) is not int or command["version"] != 1:
                raise Refused("unsupported command version")
            identity = channels.token(command["id"])
            if "turn_seconds" in command:
                self.turn_limit(command)  # refused here, before any state changes
            if "again" in command and command["again"] is not True:
                raise Refused('again is given only as true: "again": true')
            sha = receipts.digest(receipts.encoded(command))
            with self.coordination(live=True, serving=serving) as view:
                state = self.load(view)
                if identity in state["commands"]:
                    old = state["commands"][identity]
                    if old["digest"] != sha:
                        raise Refused("changed reuse of a command ID")
                    return channels.response(identity, "replayed", "already processed; no effects repeated", sha=sha)
                if state["pending"] is not None:
                    state.update(mode="blocked", reason="unresolved effect intent; no automatic replay", next_owner="developer")
                    self.save(view, state)
                    self.report(view, state)
                    return channels.response(identity, "blocked", state["reason"], sha=sha)
                if state["mode"] != "active" or len(state["commands"]) >= 512:
                    raise Refused("lifecycle closed/blocked or command limit reached")
                if command["op"] != "finish":
                    self.require_live()
                if self.settle(state, view):
                    self.lost(state)
                    self.save(view, state)
                    self.report(view, state)
                    return channels.response(identity, "blocked", state["reason"], sha=sha)
                state["pending"] = dict(id=identity, digest=sha, op=command["op"])
                # From here a failed write may or may not have reached the disk (the file
                # is replaced before its directory is synced), so the answer is never
                # `invalid` again.
                intent = identity
                self.save(view, state)
                try:
                    status, detail = self.dispatch(view, state, command)
                except Unusable as exc:
                    # Found before any effect: answered, recorded, and nothing else changes.
                    status, detail = "invalid", self.unusable(str(exc))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    state.update(mode="blocked", reason="effect incomplete: " + str(exc)[:1500], next_owner="developer")
                    self.save(view, state)
                    self.early_report(view, state)
                    return channels.response(identity, "blocked", state["reason"], sha=sha)
                stage = state["tasks"][state["active"]]["stage"] if state["active"] else None
                result = channels.response(identity, status, detail, state=stage, sha=sha)
                state["commands"][identity] = dict(digest=sha, response=result,
                                                   at=self.now().replace(microsecond=0).isoformat())
                state["pending"] = None
                self.save(view, state)
                intent = None
                if state["mode"] == "closed":
                    self.early_report(view, state)
                return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if intent is not None:
                # A write failed after the command began. Which writes reached the disk
                # is not known here. That is not a mistake the driver can correct.
                return channels.response(intent, "blocked", "the outcome of this command could not be recorded ("
                                         + str(exc)[:1000] + "). It is not certain what of it reached the disk: "
                                         "it may have taken effect, in part or in whole, and the host may take "
                                         "no further command. Send nothing again: tell the developer, who has "
                                         "to look at the shift.", sha=sha)
            return channels.response(command.get("id") if isinstance(command, dict) else None, "invalid", str(exc))

    def publish_initial(self):
        """Write the initial state if the shift has none. From then on a reader can
        tell a shift that has not begun from one whose state went missing."""
        with channels.locked(self.shift, "driver.lock", seconds=self.live_seconds(), authorize=self.guard) as view:
            try:
                view.read("driver-state.json")
            except FileNotFoundError:
                self.save(view, self.initial())
        self.initialized = True

    def poll(self):
        if not self.initialized:
            self.publish_initial()  # before any answer, also an `invalid` one
        self.last_command = channels.poll_bytes(self.shift, "driver-command.json", "driver-response.json",
                                                lambda command: self.apply(command, serving=True),
                                                self.last_command, self.answer_seconds, self.live_seconds)

    def early_report(self, view, state):
        """The report written as soon as a command closed or blocked the shift. The
        command's result is on disk by now and stays the answer if this fails; the
        host writes the report again at its own end."""
        try:
            self.report(view, state)
        except (OSError, ValueError):
            pass

    def report(self, view, state, final=False):
        """Write the closeout and the report. Only the host's own report at its end is
        `final`: it alone asks the system about the hold against idle sleep and has the
        `wake` entry. Every report a command writes comes while both seats still run
        and waits on nothing; it says nothing of the hold."""
        from . import report
        view.atomic("closeout.json", receipts.encoded(dict(
            version=1, source="foreground host", start=self.observed_start, end=self.now().isoformat(),
            elapsed_seconds=int(max(0, self.clock() - self.observed_mono)),
            elapsed_precision="whole seconds, rounded down", grant_sha256=self.pin,
            direction_sha256=self.value["direction_sha256"], reason=state["reason"],
            toolkit="local finalization entered; finalizer exit not yet observed",
            processes=dict(self.processes, cleanup=(
                "not yet performed" if not self.cleaned else "done" if not self.cleanup_errors
                else "incomplete: " + "; ".join(self.cleanup_errors)),
                           note="exit of processes the host started; builder turns are "
                           "listed per task. Process exit is not a seat's own stop statement."),
            seats="unknown unless retained stop receipts establish reported cessation",
            **({"repo": self.repo} if self.repo else {}),
            **({"temp": self.temp.record()} if self.temp is not None else {}),
            **({"wake": self.wake()} if self.wake is not None and final else {}))), replace=True)
        report.write(view)

    def finish(self, reason):
        """Bounded local finalization only: no guard, agent start or execution.
        Returns whether a report was published."""
        seconds = self.final_seconds()
        self.stopped = True  # nothing is started from here on, a second review attempt included
        if seconds <= 0:
            return False
        with self.coordination(local_only=True) as view:
            try:
                state = self.load(view)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.report(view, dict(reason="State unreadable; no acceptance inferred: " + str(exc)[:1000]), final=True)
                return True
            if self.settle(state, view):
                reason = reason[:1500] + "; " + self.lost_words  # first seen here: the developer still has to look
            # A driver that closed the shift named who acts next; the host's own ending
            # does not replace that. Every other ending is the developer's to pick up.
            # A process that may still run, or a terminal not put back, is the developer's.
            kept = state["mode"] == "closed" and not self.unconfirmed and not self.attention
            owner = state["next_owner"] if kept else "developer"
            state.update(mode="closed", reason=reason[:2000], next_owner=owner)
            self.save(view, state)
            self.report(view, state, final=True)
            return True
