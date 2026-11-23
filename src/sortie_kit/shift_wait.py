"""`sortie wait`: the driver blocks here instead of idling. Reads only; no authority.

Nothing is verified, admitted, started or written. The grant's `start` and
`until` are read without checking its signature because they only bound how
long this command itself may sleep and what time it reports as left. Those
times follow the same rule the host applies (`bounds.wind_down`,
`bounds.closing`): `seconds_left` runs to the moment the host stops the
builder turn and the driver, not to the signed deadline after it.
"""
import datetime as dt
import math
import time

from . import receipts
from .bounds import closing, wind_down
from .grants import moment
from .layout import Refused
from .receipt_schema import decode

CAP = 540  # under the ten-minute limit agent tools put on one command
FLOOR = 0.05  # no reading is started with less time than this left for it
MODES = ("active", "blocked", "closed")


def document(raw):
    """A JSON object, None when absent, or False when present but unusable."""
    if raw is None or raw is False:
        return raw
    try:
        value = decode(raw)
    except Refused:
        return False
    return value if isinstance(value, dict) else False


def usable(state, pin):
    """Whether the state has every field readiness rests on, as the host writes
    them, and belongs to this shift's grant. A missing field is not a null one."""
    if any(key not in state for key in ("grant_sha256", "mode", "tasks", "active", "turn")):
        return False
    tasks, active, turn = state["tasks"], state["active"], state["turn"]
    if state["grant_sha256"] != pin or state["mode"] not in MODES or not isinstance(tasks, dict):
        return False
    if active is not None and (not isinstance(active, str) or active not in tasks):
        return False
    if turn is not None and (not isinstance(turn, dict) or not isinstance(turn.get("task_id"), str)
                             or turn["task_id"] not in tasks):
        return False
    # A state from before the host ran reviews has neither field; that is no review.
    review, last = state.get("review"), state.get("last_review")
    if review is not None and (not isinstance(review, dict) or not isinstance(review.get("task_id"), str)
                               or review["task_id"] not in tasks
                               or not isinstance(review.get("running"), (dict, type(None)))):
        return False
    if last is not None and not isinstance(last, dict):
        return False
    return all(isinstance(task, dict) and isinstance(task.get("turns"), list)
               and all(isinstance(row, dict) for row in task["turns"]) for task in tasks.values())


def turn_note(row):
    """Plain words for a turn after which receipts/ looked as it did before, so the
    driver is told what the host saw rather than left to infer it from an exit
    status. The host saw a directory listing, not a cause."""
    if row.get("receipts_changed") is not False:
        return None
    return ("builder turn " + str(row.get("turn")) + " ended (" + str(row.get("outcome")) + ", exit "
            + str(row.get("exit")) + ") and receipts/ looked the same before and after it, so it probably "
            "wrote no receipt. Read " + str(row.get("log")) + " before starting another turn. If the log "
            "shows the builder was refused file writes or commands, that is the sandbox or permission setup "
            "of the builder's own CLI: record it as the blocker with the developer as next owner.")


def schedule(grant):
    """(when wind-down begins, when the host stops all work), from the grant's window."""
    start, until = moment(grant["start"]), moment(grant["until"])
    reserve = wind_down((until - start).total_seconds())
    return until - dt.timedelta(seconds=reserve), until - dt.timedelta(seconds=closing(reserve))


def observe(shift, seconds=5):
    """One reading of the shift's coordination files, within `seconds`. Returns the
    result without its time fields, and the grant's two times (see `schedule`)."""
    with receipts.session(shift, seconds, local_only=True) as view:
        def optional(path):
            try:
                return view.read(path)
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                return False  # there, but not a readable bounded regular file
        try:
            raw_grant = view.read("grant.json")
            until = schedule(decode(raw_grant))
        except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
            raise Refused("not a signed shift: grant.json missing or unreadable") from exc
        raw_command, raw_reply, raw_state = (optional(name) for name in (
            "driver-command.json", "driver-response.json", "driver-state.json"))
        stopped = optional("STOP") is not None
        view.guard()  # a reading that ran out of time is refused, not reported as damage
    command, response, state = document(raw_command), document(raw_reply), document(raw_state)
    # The host answers every reading of the command file, also bytes it could not
    # parse (with a null id). An answer is for this command only when it carries the
    # digest of these exact bytes and, where the command has an id, that id.
    answered = raw_command is None or bool(
        raw_command is not False and response and response.get("digest") == receipts.digest(raw_command)
        and (not command or response.get("id") == command.get("id")))
    if state is None:
        # No state yet is a shift whose host has not begun. The host publishes its
        # initial state before it answers anything, so with any response on disk
        # (also an unreadable one) a missing state is lost state.
        damaged = raw_reply is not None
    else:
        damaged = state is False or not usable(state, receipts.digest(raw_grant))
    review = last_review = None
    if damaged:
        turn, mode, task = None, "unknown", None
    elif state is None:
        turn, mode, task = None, "active", None
    else:
        turn, mode, task = state["turn"], state["mode"], state["tasks"].get(state["active"])
        review, last_review = state.get("review"), state.get("last_review")
    turns = task["turns"] if task else None
    # A review the host runs is waited for as a builder turn is. One that was begun and
    # has no attempt running is not: the driver's `continue` runs it on.
    reviewing = review is not None and review.get("running") is not None
    # Order matters: a stopped shift needs no state to be reported as stopped;
    # damage outranks everything read from the state. Expiry is applied by `timed`.
    status = ("stopped" if stopped else "damaged" if damaged else "closed" if mode != "active"
              else "ready" if answered and turn is None and not reviewing else "waiting")
    result = dict(status=status, answered=answered, mode=mode, turn=turn,
                  review=(dict({key: review.get(key) for key in ("task_id", "stage", "round")},
                               **{key: review["running"].get(key) for key in ("attempt", "started", "limit_seconds")})
                          if reviewing else None),
                  last_review=last_review,
                  last_turn=turns[-1] if turns else None, stage=task.get("stage") if task else None,
                  response=({k: response.get(k) for k in ("id", "status", "detail", "state")}
                            if answered and raw_command is not None else None))
    note = turn_note(turns[-1]) if turns and turn is None else None
    if note:
        result["note"] = note
    if damaged:
        result["detail"] = ("driver-state.json is missing, unreadable, malformed or from another grant; "
                            "whether a builder turn runs is unknown. Do not treat this as ready.")
    return result, until


def timed(result, times, now):
    """Expiry outranks every other status and is said from FLOOR before the stop,
    when no further reading could be made. `seconds_left` is the time until the
    host stops all work, rounded down; `wind_down` says that no new work is
    started any more, and `seconds_before_wind_down` is rounded up so that it is
    zero only then."""
    winding, stop = times
    left, before = (stop - now()).total_seconds(), (winding - now()).total_seconds()
    return dict(result, status="expired" if left < FLOOR else result["status"], seconds_left=max(0, int(left)),
                wind_down=before <= 0, seconds_before_wind_down=max(0, math.ceil(before)))


def wait(shift, seconds=CAP, *, clock=time.monotonic, sleep=time.sleep,
         now=lambda: dt.datetime.now(dt.timezone.utc), poll=0.2):
    """Return when the outstanding driver command is answered and neither a builder
    turn nor a review runs, when the shift is closed, stopped, expired or its state is damaged, or
    after `seconds` (at most CAP, never past the moment the host stops all
    work, which is shortly before the signed deadline; see `schedule`). A wait
    begun before wind-down also returns when wind-down begins.

    The first reading always happens and has at most five seconds; the two
    times are only known from it. Every later reading starts only when at least
    FLOOR of both the requested time and the time until the stop is left, and
    is given no more than that remainder.
    """
    end, entered = clock() + min(seconds, CAP), now()
    result, times = observe(shift, max(FLOOR, min(5, seconds)))
    winding, stop = times
    # A wait that began before wind-down returns when it begins, so that a driver
    # blocked on a running turn learns of it then and not nine minutes later.
    # "Began" is when it was called: the first reading may itself cross the line.
    early = winding > entered
    while True:
        left = (stop - now()).total_seconds()
        room = min(end - clock(), left)
        if result["status"] != "waiting" or room <= 0 or (early and (winding - now()).total_seconds() <= 0):
            return timed(result, times, now)
        if early:
            room = min(room, max(FLOOR, (winding - now()).total_seconds()))
        sleep(min(poll, room))
        left = (stop - now()).total_seconds()
        room = min(end - clock(), left)
        if room < FLOOR:
            return timed(result, times, now)
        try:
            result, times = observe(shift, min(5, room))
        except Refused:
            # This reading could not be completed in its time; the last complete one stands.
            return timed(result, times, now)
