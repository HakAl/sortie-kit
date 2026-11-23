"""Foreground admitted host; independently services the two seats' file channels."""
import time
import signal
import threading

from . import repo_state
from .lifecycle import Driver
from .builder_verification import BuilderHost
from .layout import Elapsed, Refused
from .wake import ASK

ENDING = "working time over: stopped before the signed deadline"
REPO_SECONDS = 3.0  # for one look at the target repository
# Of the finalization time, kept for writing the closeout and the report: two seconds for
# the writing, and what the question about the hold against idle sleep may take.
REPORT_RESERVE = 2.0 + ASK
LOOK_FLOOR = 0.25  # a look given less than this is not started
REVIEW_END_TRIES = 3  # passes in a row that may run out of their lock while one review's end is written


def end_look(final_seconds):
    """Seconds for the look at the repository when the shift ends, or None when
    taking it would spend the time the report needs."""
    seconds = min(REPO_SECONDS, final_seconds - REPORT_RESERVE)
    return seconds if seconds >= LOOK_FLOOR else None


def run_shift(admission, spawn=None, *, seconds, wind_down=None, poll=0.1, sleep=time.sleep, driver_seat=None,
              checks=False, interrupted=None, status=None, wake=None):
    """Serve both seats' file channels until the shift ends.

    `driver_seat` is the foreground driver process when the host started one; its
    exit ends the shift. The headless builder is owned through the Driver.
    `checks` lets the host run builder-requested check commands itself; off, such
    a request is answered `blocked` and nothing runs. `interrupted` is the
    caller's list of signals already received before this loop began. `status`,
    a dict, receives `published`: whether this host wrote its final report, and
    `driver` and `shutdown`: what the host saw of the driver process and of its
    own shutdown, as the closeout has them. `wake` is the hold against idle sleep
    the launcher took at admission, or None: this loop renews it on every pass
    that goes on, never on the one that stops and never during the stop, the
    closeout records it, and it is let go once the final report is written.
    """
    if not 0 < poll <= 10:
        raise Refused("host poll interval must be finite and bounded")
    driver = Driver(admission, spawn, seconds=seconds, wind_down=wind_down)
    if wake is not None:
        driver.wake = wake.record  # read whenever a closeout is written
    # Real time, whatever clock a test drives the shift with: file times are real.
    repo, began = getattr(getattr(admission, "trust", None), "repo", None), time.time()
    if repo is not None:
        # The driver already runs: this look comes out of the working time and never
        # reaches past the moment work stops.
        left = driver.work_seconds()
        driver.repo["start"] = (repo_state.observe(repo, min(REPO_SECONDS, left)) if left >= LOOK_FLOOR
                                else dict(error="not looked at: no working time was left"))
    builder = BuilderHost(admission, driver, enabled=checks)
    reason, code = "host stopped", 0
    unsaved = 0
    interrupted, previous = [] if interrupted is None else interrupted, {}
    guard = driver.guard
    driver.guard = lambda: "foreground host interrupted" if interrupted else guard()
    driver.halt = lambda: bool(interrupted)
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda signum, frame: interrupted.append(signum))
        while True:
            if why := driver.guard():
                reason, code = why, 3
                break
            # Wind-down does not end the shift: it only starts nothing new (the Driver
            # refuses that). The turn that runs, the driver and this loop go on until
            # the working time is used up.
            if driver.work_seconds() <= 0:
                reason = ENDING
                break
            if driver_seat is not None and driver_seat.ended():
                reason = "driver exited"
                break
            if wake is not None:
                # Only on a pass that goes on: a call into the system can take seconds, and
                # a shift that is to stop does not wait on one. It is told the working time
                # left and is not begun with less than its calls can take, so it never
                # reaches past the stop. What was renewed within the last 30 s is held for
                # 90 s more, which is longer than the stop takes.
                wake.renew(driver.work_seconds())
            driver.poll()
            from .channels import locked
            try:
                driver.poll_turn()
                # No wait in this loop is given more than the working time left, so that
                # one held up (a lock someone else holds) cannot carry it past the stop.
                with locked(admission.shift, "driver.lock", seconds=min(10, driver.work_seconds()),
                            authorize=driver.guard) as view:
                    state = driver.load(view)
                    # A review's end that was seen and not yet written is written by `settle`.
                    ended = driver.observed is not None and driver.kind == "review"
                    lost = driver.settle(state, view)
                    if lost:
                        driver.lost(state)
                    if lost or ended:
                        driver.save(view, state)
                    if state["mode"] != "active":
                        reason = state["reason"] or "driver closed"
                        break
                unsaved = 0
            except Elapsed:
                # The time under one lock ran out while a review's end was written. What
                # was seen of that end is kept, and the receipt once it is built, so the
                # next pass finishes the same write under a lock of its own. The shift
                # goes on for that; a few passes in a row that all run out do end it.
                pending = driver.child is None and driver.observed is not None and driver.kind == "review"
                unsaved += 1
                if not pending or unsaved > REVIEW_END_TRIES or driver.work_seconds() <= 0 or driver.guard():
                    raise
                sleep(min(poll, driver.work_seconds()))
                continue
            builder.poll()
            # Never past the moment work stops: the stop and the report need what follows it.
            sleep(min(poll, driver.work_seconds()))
    except KeyboardInterrupt:
        reason, code = "foreground host interrupted", 130
    except Elapsed as exc:
        # An operation that ran out of the time it was given, exactly because the
        # working time ran out, is the ordinary ending. Nothing else is.
        reason, code = (ENDING, 0) if driver.work_seconds() <= 0 and not driver.guard() else (str(exc), 3)
    except (OSError, ValueError) as exc:
        reason, code = str(exc), 3
    finally:
        try:
            # No builder process outlives the shift. Both seats are stopped first; the
            # turn's end is written after that, so that no wait for a lock or for the
            # disk stands between the builder's stop and the driver's.
            try:
                driver.poll_turn(stop="stopped: " + reason[:200], record=False)
            except (OSError, ValueError) as exc:
                # An end that was seen but not written is kept for the report below.
                # A turn still held here was not ended at all.
                if driver.child is not None:
                    driver.unconfirmed = True
                    driver.cleanup_errors.append("builder turn not ended: " + str(exc)[:200])
            later = getattr(driver_seat, "recheck", None)
            if driver_seat is not None:
                # Also gives the developer's terminal back before the report is written.
                try:
                    driver.processes["driver"] = driver_seat.close()
                except (OSError, ValueError) as exc:
                    # What the seat found out about the terminal before it failed is kept.
                    driver.processes["driver"] = ("stop not confirmed: " + str(exc)[:200] + (
                        "; terminal not restored" if getattr(driver_seat, "unrestored", False) else ""))
            try:
                driver.record_turn()
            except (OSError, ValueError):
                pass  # the end that was seen is kept and goes into the state with the final report
            if repo is not None:
                # After both seats' processes were stopped, before the report: what is
                # uncommitted now, so that half a change left behind is named.
                seconds = end_look(driver.final_seconds())
                # Held to the Git directories the look at the start read. When that look
                # failed there is nothing to hold it to, and the look says `pinned: false`.
                first = driver.repo.get("start")
                where = {name: first[name] for name in ("git_dir", "common_dir")
                         if isinstance(first, dict) and isinstance(first.get(name), str)}
                looked = time.monotonic()
                driver.repo["end"] = (repo_state.observe(repo, seconds, since=began, **where) if seconds is not None
                                      else dict(error="not looked at: the time left was kept for the report"))
                if seconds is not None:
                    # Whether a commit made since holds what was uncommitted at the start:
                    # in what the look above left of its own time, and never the report's.
                    left = min(seconds - (time.monotonic() - looked), end_look(driver.final_seconds()) or 0)
                    found = repo_state.held(repo, first, driver.repo["end"], left if left >= LOOK_FLOOR else 0)
                    if found is not None:
                        driver.repo["held"] = found
            driver.cleaned = True
            if later is not None:
                # A driver that was still exiting when its stop ran out of time is usually
                # gone by now. One look, no wait and no signal, before the report says so.
                try:
                    driver.processes["driver"] = later()
                except (OSError, ValueError):
                    pass  # what close() saw stands
            seen, problems, groups = driver.processes.get("driver", ""), [], []
            # A normal ending is not reported as clean while a started process may still
            # run, the terminal was not given back as it was, or cleanup failed. That is
            # the host's own finding and is kept apart from the reason, which stays what
            # ended the shift: often the driver's own closing words.
            if "not observed" in seen or "not confirmed" in seen:
                problems.append("the driver process was not seen to stop (" + seen[:300] + ")")
                group = getattr(driver_seat, "group", None)
                groups += [group] if type(group) is int and group > 1 else []
            if driver.unconfirmed:
                problems.append("a builder turn was not seen to stop")
                groups += driver.leftover_groups
            if "not restored" in seen:
                problems.append("the terminal could not be restored")
            if driver.cleanup_errors:
                problems.append("cleanup was incomplete: " + "; ".join(driver.cleanup_errors)[:500])
            driver.processes["shutdown"] = dict(confirmed=not problems, groups=groups, problems=problems)
            if problems:
                code = code or 3  # automation can tell; the report and the last lines say which
                driver.attention = True  # whoever the driver named, the developer has to look
            if status is not None:
                # For the terminal's last lines, which must not depend on the report being written.
                status.update(driver=seen, shutdown=driver.processes["shutdown"])
            try:
                driver.published = bool(driver.finish(reason))
            except (OSError, ValueError):
                driver.published = False
            if not driver.published:
                # An earlier report.md, if any, predates this cleanup.
                reason, code = reason + "; the final report was not written", code or 3
            if status is not None:
                status["published"] = driver.published
        finally:
            if wake is not None:
                wake.release()  # after the closeout and the report, whether or not they were written
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return code, reason
