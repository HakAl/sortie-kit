"""One command for a shift: prepare, sign in the terminal, admit, start the driver, serve it."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import threading

from .bounds import Budget, Sources
from .layout import Refused, Request, prepare

ROLES = ("driver", "builder")  # the seats the developer names; the reviewer is picked unless named


def signed_seats(repo, choices, *, builder_network=False, home=None, search=None):
    """Turn the developer's choices into pinned seats.

    choices: {role: {"agent", "model", "family"}}. Nothing is defaulted
    here; a missing model or family refuses in the profile lookup.
    `builder_network` asks for the builder's form with the network on, and
    refuses a builder profile that has none before any seat's program is
    looked for.
    """
    from . import agents
    profiles = {role: agents.load(choices[role]["agent"], repo, home=home) for role in ROLES}
    if builder_network:
        if profiles["builder"]["value"]["headless_argv"] is None:
            raise Refused("agent " + profiles["builder"]["value"]["name"]
                          + " has no headless start and cannot be the builder")
        # With the builder's model and the repository in their places, as it would be started.
        chosen = choices["builder"]
        model, _ = agents.choose_model(profiles["builder"], chosen.get("model"), chosen.get("family"))
        agents.network_argv(profiles["builder"], model, repo)
    seats = {}
    for role in ROLES:
        choice, profile = choices[role], profiles[role]
        model, family = agents.choose_model(profile, choice.get("model"), choice.get("family"))
        executable, _ = agents.pin_executable(profile, repo, search=search)
        seats[role] = agents.seat(role, profile, model, family, executable,
                                  network=bool(builder_network) and role == "builder")
    return seats


def review_order(repo, *, home=None):
    """Every installed profile name in the order a reviewer is picked from: the
    built-in ones as `agents.REVIEW_ORDER` has them, then the developer's own by name."""
    from . import agents
    names = agents.available(repo, home=home)
    return [n for n in agents.REVIEW_ORDER if n in names] + [n for n in names if n not in agents.REVIEW_ORDER]


def found(profile, search=None):
    """Whether the profile's program is there to be started. Nothing is hashed or run."""
    import shutil
    program = profile["value"]["executable"]
    return bool(os.path.isfile(program) if os.path.isabs(program) else shutil.which(program, path=search))


def reviewers(repo, *, home=None, search=None):
    """The profiles that can review and whose program is found, in pick order, each
    with the family of its review model. One that does not load is left out."""
    from . import agents
    able = []
    for name in review_order(repo, home=home):
        try:
            profile = agents.load(name, repo, home=home)
            if not agents.can_review(profile) or not found(profile, search):
                continue
            model, family = agents.choose_model(profile, profile["value"]["review_model"])
        except (OSError, ValueError):
            continue
        able.append(dict(name=name, profile=profile, model=model, family=family))
    return able


def no_reviewer(repo, *, home=None):
    """The refusal of a start that has nobody to review with."""
    return Refused("no installed agent can review: none of " + ", ".join(review_order(repo, home=home))
                   + " has both a review form in its profile and its program on this machine, so install "
                   "and sign in to one of them or name a profile of your own with --agent reviewer=NAME; "
                   "nothing was prepared or signed")


def pick_reviewer(repo, builder, given=None, *, same_family=False, home=None, search=None, pin=None):
    """The signed reviewer seat and how it was chosen.

    `builder` is the builder's seat or choice (its agent name and family).
    `given` is what `--agent reviewer=` named ({"agent", "model", "family"})
    or None. With none named, the first installed agent that can review and is
    of another family than the builder is picked; when there is none, the
    builder's own CLI (or, when that cannot review, the first that can), and
    the review is same-family. A same-family reviewer named by flag while one
    of another family is installed needs `same_family`. Returns (seat, choice)
    with choice one of `picked-other-family`, `picked-same-family`, `given`.
    `pin` stands for `agents.pin_executable`; `sortie doctor` passes one that does
    not read again a file it has hashed and that is still the same file.
    """
    from . import agents
    from .grants import family as canonical
    able = reviewers(repo, home=home, search=search)
    mine = canonical(builder["family"])
    others = [item for item in able if item["family"] != mine]
    if given is not None:
        profile = agents.load(given["agent"], repo, home=home)
        if not agents.can_review(profile):
            raise Refused("agent " + given["agent"] + " has no review start and cannot be the reviewer")
        model, family = agents.choose_model(profile, given.get("model") or profile["value"]["review_model"],
                                            given.get("family"))
        choice = "given"
        if family == mine and others and not same_family:
            raise Refused("the reviewer named with --agent reviewer= is of the builder's own family (" + mine
                          + ") while an agent of another family is installed (" + others[0]["name"]
                          + "); name that one, or pass --allow-same-family-review to keep this choice")
    elif others:
        item = others[0]
        profile, model, family, choice = item["profile"], item["model"], item["family"], "picked-other-family"
    elif able:
        item = next((item for item in able if item["name"] == builder["agent"]), able[0])
        profile, model, family, choice = item["profile"], item["model"], item["family"], "picked-same-family"
    else:
        raise no_reviewer(repo, home=home)
    if same_family and family != mine:
        raise Refused("--allow-same-family-review was given, but the builder and reviewer families differ ("
                      + mine + " and " + family + ")")
    try:
        executable, _ = (pin or agents.pin_executable)(profile, repo, search=search)
    except Refused as exc:
        raise Refused("reviewer " + profile["value"]["name"] + ": " + str(exc)) from exc
    return agents.seat("reviewer", profile, model, family, executable), choice


def all_seats(repo, choices, *, same_family=False, builder_network=False, home=None, search=None):
    """The three pinned seats of a shift and how the reviewer was chosen: the driver
    and the builder as named, the reviewer as named or picked."""
    seats = signed_seats(repo, choices, builder_network=builder_network, home=home, search=search)
    seats["reviewer"], choice = pick_reviewer(repo, seats["builder"], choices.get("reviewer"),
                                              same_family=same_family, home=home, search=search)
    return seats, choice


def start(request: Request, *, prepare_only=False, sign_only=False, trust_path=None,
          key=None, choices=None, same_family=False, review_seconds=None, builder_network=False,
          home=None, search=None, wake=None, temp=None):
    """--prepare-only and --sign-only return the shift. With neither, run the whole
    shift and return (shift,) with what `run` returns and, last, the driver's seat as
    it was signed.

    `choices` names the driver and the builder and may name the reviewer
    (`--agent reviewer=`); without one the reviewer is picked, see
    `pick_reviewer`. `review_seconds` is the limit of one run of the reviewer.
    `builder_network` signs the builder's form with the network on, and is refused
    for a builder profile that has none and for a preparation, which signs nothing.
    `wake` is the hold against idle sleep for a shift that runs (`wake.Wake`), or
    None for no hold; it is said to the signer and is no part of the grant.
    `temp` makes the directory for the temporary files of builder turns and
    reviews when the shift runs (`turn_temp.make`), or is None for none; where
    it will be is said to the signer and is no part of the grant either."""
    if prepare_only and not sign_only:
        if builder_network:
            raise Refused("--builder-network applies only when signing; a preparation names no seat")
        return prepare(request, Sources())
    from .grants import REVIEW_LIMIT, Trust, make_grant, review_limit, review_policy
    from .signing import sign, terminal
    what = "sign-only" if sign_only else "starting a shift"
    if prepare_only or trust_path is None or key is None:
        raise Refused(what + " requires --trust and --key, without --prepare-only")
    if (not isinstance(choices, dict) or set(choices) - {"reviewer"} != set(ROLES)
            or any(not isinstance(c, dict) or not c.get("agent") for c in choices.values())):
        raise Refused("name an agent for the driver and for the builder before signing")
    review_seconds = review_limit(REVIEW_LIMIT if review_seconds is None else review_seconds)
    os.close(terminal())
    if not sign_only:
        # The driver is handed the terminal by its own device name. Learn now whether
        # that works, not after the passphrase is typed and the one-use grant is spent.
        from .tick_supervise import own_terminal
        os.close(own_terminal())
    repo, _ = request.check()
    trust = Trust(trust_path, repo)
    # Every seat's program is found and pinned before anything is written, so an agent
    # CLI that is not installed, or no agent that can review, ends the start with
    # nothing prepared.
    seats, choice = all_seats(repo, choices, same_family=same_family, builder_network=builder_network,
                              home=home, search=search)
    sources = Sources()
    shift = prepare(request, sources)
    policy = review_policy(seats["builder"]["model"], seats["builder"]["family"],
                           seats["reviewer"]["model"], seats["reviewer"]["family"], choice)
    raw = make_grant(shift, trust, seats, request.seconds, policy=policy, review_seconds=review_seconds)
    notes = []
    if not sign_only:
        from . import turn_temp
        notes += [] if wake is None else [wake.plan()]
        notes += [] if temp is None else [turn_temp.words(repo)]
    # What a pin does not cover, for a seat whose pinned file is a script: read from the
    # files as they are now and, like the two lines above, no part of the grant.
    from . import agents
    notes += agents.launcher_words(seats)
    # A linked working tree is taken without a refusal, and is no copy of the repository.
    from .layout import linked_words
    notes += linked_words(repo)
    sign(shift, trust, key, raw, seconds=request.seconds, sources=sources, note=notes or None)
    if sign_only:
        return shift
    # See run for the rest. The driver's seat is last: the lines for a driver whose CLI
    # ended by itself are made from it, with or without a report.
    return (shift,) + run(shift, trust, seats, request.seconds, sources=sources, wake=wake, temp=temp) + (seats["driver"],)


def driver_prompt(shift, value):
    """Everything the host tells the driver at its start. Host-authored; the same
    text for every shift apart from these signed or local facts."""
    from .lifecycle import toolkit
    tool = toolkit()  # the builder is given the same command, spelled the same way
    return ("You are the driver seat of a sortie-kit shift. Read driver.md in the shift directory "
            "first and follow it.\n"
            "shift: " + str(shift) + "\n"
            "direction: direction.md in the shift, SHA256 " + value["direction_sha256"] + "\n"
            "deadline: " + value["until"] + "\n"
            "toolkit: " + tool + "\n"
            "Publish each command as driver-command.json in the shift, then run\n"
            + tool + " wait --shift " + shlex.quote(str(shift)) + "\n"
            "instead of idling. Run that line exactly as written, as a command line of its own: never from a "
            "shell variable and never rewrapped. Wait for its answer before you publish the next command. "
            "The host starts the builder and the reviewer for you; you start no agent yourself.")


def host_log(shift, line):
    """The host writes nothing to the terminal while the driver has it."""
    try:
        # Never waits: the builder can write under logs/, and a pipe put at this
        # path must not hold the host while the driver has the terminal.
        fd = os.open(shift / "logs" / "host.log",
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        return
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            os.write(fd, (dt.datetime.now(dt.timezone.utc).isoformat() + " " + line[:2000] + "\n").encode())
    except OSError:
        pass
    finally:
        os.close(fd)


def kept(shift, name, limit=2 * 1024 * 1024):
    """The first bytes of one regular file in the shift, read without following a link."""
    fd = os.open(shift / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        chunks = []
        while len(chunks) < 64 and (chunk := os.read(fd, 65536)):
            chunks.append(chunk)
        return b"".join(chunks)[:limit]
    finally:
        os.close(fd)


def closing_lines(raw, code, inert, shutdown=None, seen=""):
    """The last lines of a live start: what the shift produced and, apart from it,
    whether the host saw everything it started stop.

    `raw` is the report.json of the final report, the same bytes `report_read`
    matched to report.md, or None without one: the tasks come only from it.
    `shutdown` is what the host itself saw (`processes.shutdown` as it gave it to
    the closeout) and `seen` its driver line; they are used whether or not a
    report was written. Only without them is the report's own closeout read.
    `inert` makes text safe to print.
    """
    from . import report
    show = lambda text, limit=600: inert(str(text)[:limit])
    lines, doc, label, failed = [], None, "", (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError)
    # Each line by itself: a summary that cannot be read costs the Tasks line and
    # never what the host saw of its own shutdown.
    try:
        doc = json.loads(raw) if raw is not None else None
        doc = doc if isinstance(doc, dict) else None
        if doc is not None:
            lines.append("Tasks: " + report.tasks_words(doc, show))
    except failed:
        pass
    try:
        # What the driver noted as a decision or a question is in the report, not here.
        noted = report.driver_notes(doc) if doc is not None else None
        if noted:
            lines.append("Notes from the driver: " + str(len(noted)) + "; see the report.")
    except failed:
        pass
    try:
        # A review by the builder's own model family is said wherever the result is.
        same = report.family_words(doc["grant"]["review_policy"]) if doc is not None else ""
        if same:
            lines.append("Reviews: " + show(same) + ".")
    except failed:
        pass
    try:
        if isinstance(shutdown, dict):
            window = dict(processes=dict(cleanup="done", driver=str(seen), shutdown=shutdown))
            label, wrong, lead, command = report.shutdown_words(window, show)
        elif doc is not None and isinstance(doc.get("actual_window"), dict):
            label, wrong, lead, command = report.shutdown_words(doc["actual_window"], show, doc)
        if label:
            lines.append("Shutdown: " + label + "." + (" " + wrong if wrong else "") + (" " + lead + command if command else ""))
    except failed:
        label = ""
    if not label:
        lines.append("Shutdown: not known; " + ("the report's summary could not be read." if raw is not None
                                                else "the final report was not written."))
    if code == 3 and label.startswith("NOT"):
        # The task result above stands; the status is the host's, for whatever runs this.
        lines.append("Exit status 3 is about this shutdown, not about the tasks.")
    return lines


def report_state(shift):
    """`final` when report.md belongs to the report.json beside it and that summary
    was written after the host's cleanup; `preliminary` when a report.md exists
    but that cannot be established; `none` without one.

    The Markdown is replaced last, so an earlier one can outlive a newer closeout
    or summary. It names the digest of its own summary; only that pair counts.
    """
    return report_read(shift)[0]


def report_read(shift):
    """`report_state`, and with `final` the bytes of report.json that were matched to
    report.md, so that what is shown from the summary is what was checked."""
    read = lambda name: kept(shift, name)
    try:
        rendered = read("report.md")
    except OSError:
        return "none", None
    try:
        raw = read("report.json")
        if ("SHA256 of report.json: " + hashlib.sha256(raw).hexdigest() + "\n").encode() not in rendered:
            return "preliminary", None
        cleanup = json.loads(raw)["actual_window"]["processes"]["cleanup"]
    except (OSError, ValueError, KeyError, TypeError):
        return "preliminary", None
    final = isinstance(cleanup, str) and cleanup.startswith(("done", "incomplete"))
    return ("final", raw) if final else ("preliminary", None)


# Printed just before the driver's CLI takes the terminal, always: the host cannot know
# what a CLI has been told to trust. A driver is only ever started interactive, here.
DRIVER_QUESTION = ("sortie: the driver's CLI starts now. Its first screen may be a question of its own, such as "
                   "whether you trust the folder. Read it before pressing Enter: its preselected answer can be the "
                   "one that declines and exits, and a driver that exits ends the shift.")


def run(shift, trust, seats, seconds, *, sources=None, poll=0.5, launch=None, serve=None, wake=None, temp=None):
    """Admit the signed shift, start the interactive driver in this terminal and
    serve the seats until the shift ends. Returns (code, reason, driver process,
    whether the host published its final report, what the host saw of its own
    shutdown: the closeout's `processes.shutdown`, or None when the loop never ran).

    `wake`, when given, is held from admission on: taken here before the driver is
    started, renewed and let go by the host loop once its report is written, and
    let go here when the start fails before that loop. `temp` is handed to the
    host loop, which makes and removes the directory; nothing is made before it."""
    from .grants import Admission, moment
    from .host import run_shift
    from .tick_supervise import ForegroundSeat
    launch, serve = launch or ForegroundSeat, serve or run_shift
    seat, interrupted, previous, closed, status = [], [], {}, [], {}

    def close():
        # Once: gives the terminal back and says what was seen of the driver process.
        if not closed:
            closed.append(seat[0].close() if seat else "not started")
        return closed[0]

    # From before the terminal is handed over until it is back: a stop signal to the
    # host is noted and handled by the cleanup below, never by dying with the
    # driver still holding the terminal.
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda signum, frame: interrupted.append(signum))
        with Admission(shift, trust, seconds=seconds, expected_seats=seats, sources=sources) as admission:
            value = admission.value
            if wake is not None:
                wake.acquire()  # a refusal is in its record; it never stops the shift

            def begin(guard):
                early = "foreground host interrupted before the driver start"
                if interrupted:
                    raise Refused(early)
                cwd, env = str(trust.repo), dict(os.environ)
                argv = admission.seat_start("driver", driver_prompt(shift, value))
                # The last decision, with nothing left to prepare before the terminal
                # changes hands. A stop noted after it closes the driver right below.
                if interrupted:
                    raise Refused(early)
                try:
                    print(DRIVER_QUESTION, flush=True)
                except (OSError, ValueError):
                    pass  # a terminal that takes no line is no reason to start no driver
                seat.append(launch(argv, cwd=cwd, env=env))
                if interrupted:
                    raise Refused("foreground host interrupted while starting the driver")
                host_log(shift, "driver started; the terminal is the driver's until it ends")

            def host(remaining):
                return serve(admission, seconds=remaining, poll=poll, driver_seat=seat[0], interrupted=interrupted,
                             status=status, **({} if wake is None else {"wake": wake}),
                             **({} if temp is None else {"temp": temp}))

            try:
                code, reason = admitted_start(shift, admission, seconds=seconds, until=moment(value["until"]),
                                              begin=begin, host=host, sources=sources, closing=close, wake=wake)
            finally:
                if wake is not None:
                    wake.release()  # once: the host loop already did when it ran
                # The host's own last word on the driver, the one its closeout has; no later
                # look is taken here, so the terminal and the report cannot disagree.
                outcome = status.get("driver") or close()
                host_log(shift, "shift ended; driver process: " + outcome)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return code, reason, outcome, bool(status.get("published")), status.get("shutdown")


def admitted_start(shift, admission, *, seconds, until, begin, host, sources=None, closing=None, wake=None):
    """Run the steps between admission and the host loop, then the host loop.

    `begin(guard)` does whatever must happen before the loop (the driver launch,
    from the next slice on); `host(remaining_seconds)` is the loop. A failure in
    `begin` is not retried and never reaches `host`, but the shift still gets a
    report whose closeout says the admitted start failed. `closing()` stops
    whatever `begin` started and returns what it saw; it runs before that
    report, so the terminal is back first and the report says how the driver
    process ended. Once `host` is running, finalization is its own job.
    """
    budget = Budget(seconds, until=until)
    sources = sources or Sources()

    def guard():
        return budget.guard() or sources.guard() or admission.guard()

    started_wall, started_mono = dt.datetime.now(dt.timezone.utc), budget.clock()
    try:
        if why := guard():
            raise Refused(why)
        begin(guard)
    except (OSError, ValueError, KeyboardInterrupt, subprocess.SubprocessError) as exc:
        processes = None
        if closing is not None:
            try:
                processes = dict(driver=closing(), cleanup="done")
            except (OSError, ValueError) as failure:
                processes = dict(driver="stop not confirmed: " + str(failure)[:200], cleanup="incomplete")
        start_failed(shift, admission, budget, until, started_wall, started_mono, exc, processes, wake)
        raise
    return host(max(0.001, budget.remaining()))


def start_failed(shift, admission, budget, until, started_wall, started_mono, exc, processes=None, wake=None):
    """Publish the report for an admitted shift that never reached its host loop."""
    from . import report
    grace = admission.value["finalization_seconds"]
    now = dt.datetime.now(dt.timezone.utc)
    remaining = min(5, budget.end + grace - budget.clock(), (until - now).total_seconds() + grace)
    if remaining <= 0:
        return
    try:
        report.finalize(shift, remaining, closeout=dict(
            version=1, source="admitted start failed", start=started_wall.isoformat(),
            end=now.isoformat(), elapsed_seconds=int(max(0, budget.clock() - started_mono)),
            elapsed_precision="whole seconds, rounded down",
            grant_sha256=hashlib.sha256(admission.raw).hexdigest(),
            direction_sha256=admission.value["direction_sha256"], reason=str(exc)[:2000],
            toolkit="start failed; finalizer exit not yet observed", seats="cessation unknown",
            **({} if processes is None else {"processes": processes}),
            **({} if wake is None else {"wake": wake.record()})))
    except (OSError, ValueError):
        pass  # Keep the original failure; an exhausted or unsafe output cannot be forced.
