"""Builder-requested reproduction in pinned scratch; never a driver operation."""
import os
import selectors
import shlex
import subprocess
import time

from . import channels, receipts
from .bounds import Budget, Sources
from .layout import Refused
from .receipt_schema import decode
from .tick_supervise import OwnedChild, Supervisor

SPEC_FIELDS = {"version", "id", "task_id", "request_id", "round", "candidate", "baseline", "baseline_reason",
               "finding_id", "review_request", "argv", "timeout_seconds", "baseline_exit", "candidate_exit",
               "baseline_contains", "candidate_contains", "harness", "harness_reason"}


def check_spec(spec):
    channels.fields(spec, SPEC_FIELDS)
    if type(spec["version"]) is not int or spec["version"] != 1:
        raise Refused("unsupported check request version")
    for key in ("id", "task_id", "request_id", "finding_id"):
        channels.token(spec[key])
    if type(spec["round"]) is not int or not 0 <= spec["round"] <= 3:
        raise Refused("invalid check round")
    if type(spec["timeout_seconds"]) is not int or not 1 <= spec["timeout_seconds"] <= 3600:
        raise Refused("check requires a finite timeout")
    if not isinstance(spec["argv"], list) or not 1 <= len(spec["argv"]) <= 64:
        raise Refused("check requires bounded argv")
    for arg in spec["argv"]:
        channels.text(arg, 4096)
        if "\0" in arg:
            raise Refused("NUL in check argv")
    for key in ("baseline_exit", "candidate_exit"):
        if type(spec[key]) is not int or not -255 <= spec[key] <= 255:
            raise Refused("invalid expected exit")
    channels.text(spec["candidate_contains"])
    if spec["baseline"] is None:
        channels.text(spec["baseline_reason"])
    else:
        channels.text(spec["baseline_contains"])
        if spec["baseline_reason"] is not None:
            raise Refused("unexpected baseline applicability reason")
    if spec["harness"] is None:
        channels.text(spec["harness_reason"])
    elif spec["harness_reason"] is not None:
        raise Refused("unexpected harness applicability reason")


def queue_check(shift, path):
    """Publish a request, without admission, execution, or a claim of completion."""
    with channels.locked(shift, "builder-queue.lock") as view:
        raw = view.read(path)
        spec = decode(raw)
        check_spec(spec)
        try:
            pending = decode(view.read("builder-check.json"))
            if pending != spec:
                answer = decode(view.read("builder-check-response.json"))
                if answer.get("id") != pending.get("id") or answer.get("digest") != receipts.digest(receipts.encoded(pending)):
                    raise Refused("wait for the pending check response before replacing the slot")
        except FileNotFoundError:
            # An existing request without a response remains pending.
            try:
                view.read("builder-check.json")
            except FileNotFoundError:
                pass
            else:
                raise Refused("check request already pending")
        view.atomic("builder-check.json", receipts.encoded(spec), replace=True)
        return channels.response(spec["id"], "queued", "request published; no execution or acceptance claimed",
                                 sha=receipts.digest(receipts.encoded(spec)))


def run_command(argv, cwd, env, *, seconds, guard, output_limit=1024 * 1024):
    """Finite owned process group, bounded pipe, no shell or inherited terminal."""
    budget = Budget(seconds)
    if why := guard():
        raise Refused(why)
    owner = Supervisor(budget, Sources(), authorize=guard)
    data, reason, code = bytearray(), "", None
    with selectors.DefaultSelector() as selector:
        child = OwnedChild(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           cwd=cwd, env=env, process_group=0)
        owner.child = child
        try:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            eof = False
            while not (eof and child.poll() is not None):
                reason = budget.guard() or guard()
                if reason:
                    break
                for key, _ in selector.select(min(0.05, budget.remaining())):
                    chunk = os.read(key.fd, min(65536, output_limit - len(data) + 1))
                    if not chunk:
                        eof = True
                        selector.unregister(key.fileobj)
                    data.extend(chunk)
                    if len(data) > output_limit:
                        data = data[:output_limit]
                        reason = "output limit exceeded"
                        break
                if reason:
                    break
            code = child.poll()
        finally:
            try:
                # Cleanup has its own finite grace after the execution budget.
                # macOS can transiently deny group observation while the leader
                # exits; retry only the owned handle, never execution or sends.
                cleanup = Budget(1)
                owner.budget = cleanup
                while True:
                    try:
                        if not owner.stop_child():
                            reason = "owned execution cleanup unconfirmed"
                        break
                    except PermissionError:
                        if cleanup.guard():
                            reason = "owned execution cleanup permission denied; cessation unknown"
                            break
                        time.sleep(min(.02, cleanup.remaining()))
            finally:
                child.stdout.close()
    return code, bytes(data), reason


class BuilderHost:
    def __init__(self, admission, driver, enabled=True):
        self.admission, self.driver, self.shift = admission, driver, admission.shift
        # Off: a queued check is answered without running anything. The host runs
        # as the developer, outside whatever the builder's own agent setup allows.
        self.enabled = enabled
        self.last_request = None

    def guard(self):
        return self.driver.guard() or ("wind-down before execution" if self.driver.active_seconds() <= 0 else "")

    def git(self, args, seconds):
        if why := self.guard():
            raise Refused(why)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", HOME=str(self.shift / "scratch"),
                   TMPDIR=str(self.shift / "scratch"), TMP=str(self.shift / "scratch"), TEMP=str(self.shift / "scratch"))
        result = subprocess.run(["git", "--no-optional-locks", "--no-replace-objects", "-C", str(self.admission.trust.repo),
                                 "-c", "core.hooksPath=" + str(self.shift / "scratch" / "empty-hooks"),
                                 "-c", "core.fsmonitor=false", *args], env=env, capture_output=True,
                                timeout=min(5, seconds, self.driver.active_seconds()))
        if result.returncode:
            raise Refused("scratch Git operation refused: " + result.stderr.decode("utf-8", "replace")[:500])
        if why := self.guard():
            raise Refused(why)
        return result.stdout.strip().decode()

    def execute(self, view, spec, kind, seconds):
        commit = spec[kind]
        view.commit(commit)
        work = "scratch/check-" + spec["id"] + "-" + kind
        path = self.shift / work
        # Every component must still be local and unused; retain checkout as evidence.
        view.scratch_path(work)
        if os.path.lexists(path):
            raise Refused("scratch checkout already exists; no execution replay")
        self.git(["worktree", "add", "--detach", str(path), commit], seconds)
        actual = self.git(["-C", str(path), "rev-parse", "HEAD"], seconds)
        if actual != commit:
            raise Refused("scratch checkout candidate differs")
        argv = list(spec["argv"])
        if spec["harness"] is not None:
            raw = view.artifact(spec["harness"])
            harness = path / ".sortie-check"
            with harness.open("xb") as stream:
                stream.write(raw)
            argv = [str(harness) if arg == "{harness}" else arg for arg in argv]
        elif "{harness}" in argv:
            raise Refused("missing requested harness")
        home, temp = path / ".sortie-home", path / ".sortie-tmp"
        home.mkdir(); temp.mkdir()
        env = dict(PATH=os.environ.get("PATH", os.defpath), HOME=str(home), TMPDIR=str(temp), TMP=str(temp),
                   TEMP=str(temp), PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
        limit = min(spec["timeout_seconds"], seconds, self.driver.active_seconds())
        if why := self.guard():
            raise Refused(why)
        code, raw, reason = run_command(argv, str(path), env, seconds=limit, guard=self.guard)
        output_path = "logs/check-" + spec["id"] + "-" + kind + ".log"
        with receipts.session(self.shift, self.driver.final_seconds()) as final:
            final.atomic(output_path, raw or (reason + "\n").encode())
        observed = raw.decode("utf-8", "replace")
        matched = not reason and code == spec[kind + "_exit"] and spec[kind + "_contains"] in observed
        return dict(commit=actual, worktree=work, command=shlex.join(argv),
                    timeout_seconds=spec["timeout_seconds"], expected_exit=spec[kind + "_exit"],
                    exit_status=code if code is not None else -255,
                    output=dict(path=output_path, sha256=receipts.digest(raw or (reason + "\n").encode())),
                    expected_behavior=spec[kind + "_contains"], observed_behavior=(reason or observed.strip() or "empty output")[:2000],
                    outcome="matched" if matched else "unavailable" if reason else "mismatched")

    def apply(self, spec):
        identity = None
        try:
            check_spec(spec)
            identity = spec["id"]
            sha = receipts.digest(receipts.encoded(spec))
            if not self.enabled:
                return channels.response(
                    identity, "blocked", "the host does not run check commands in this shift; nothing was "
                    "executed. Run the reproduction yourself, within what your own configuration allows.", sha=sha)
            with channels.locked(self.shift, "builder-host.lock") as view:
                try:
                    state = decode(view.read("builder-check-state.json"))
                    channels.fields(state, {"version", "grant_sha256", "requests"})
                    if (type(state["version"]) is not int or state["version"] != 1
                            or state["grant_sha256"] != self.driver.pin or not isinstance(state["requests"], dict)
                            or len(state["requests"]) > 128):
                        raise Refused("invalid builder check state")
                except FileNotFoundError:
                    state = dict(version=1, grant_sha256=self.driver.pin, requests={})
                if identity in state["requests"]:
                    old = state["requests"][identity]
                    if old["digest"] != sha:
                        raise Refused("changed reuse of check ID")
                    return channels.response(identity, "replayed", "check already attempted; no re-execution", evidence=old.get("evidence"), sha=sha)
                if len(state["requests"]) >= 128:
                    raise Refused("check request limit reached")
                if why := self.guard():
                    return channels.response(identity, "blocked", why, sha=sha)
                with channels.locked(self.shift, "driver.lock") as driver_view:
                    coordination = self.driver.load(driver_view)
                    task = coordination["tasks"].get(coordination["active"])
                    if (coordination["mode"] != "active" or coordination["pending"] is not None or task is None
                            or task["stage"] != "build" or task["id"] != spec["task_id"]
                            or task["request_id"] != spec["request_id"] or task["round"] != spec["round"]):
                        # A check is the builder's own reproduction, asked for while it
                        # builds. No candidate is bound then; the commits it names are
                        # checked to exist below. The host's reviewer runs no code.
                        raise Refused("check not bound to the active build phase and round")
                view.artifact(spec["review_request"])
                view.commit(spec["candidate"])
                if spec["baseline"] is not None:
                    view.commit(spec["baseline"])
                state["requests"][identity] = dict(digest=sha, evidence=None, status="intent")
                view.atomic("builder-check-state.json", receipts.encoded(state), replace=True)
                result = dict(finding_id=spec["finding_id"], review_request=spec["review_request"],
                              baseline={"kind": "not_applicable", "reason": spec["baseline_reason"]},
                              candidate=None, harness=spec["harness"], harness_reason=spec["harness_reason"],
                              status="blocked", reason="execution incomplete; no reviewer assessment")
                try:
                    # Allow a longer execution budget than the bounded metadata IO session.
                    execution_budget = Budget(min(spec["timeout_seconds"] * 2 + 10, self.driver.active_seconds()))
                    view.budget = execution_budget
                    if spec["baseline"] is not None:
                        result["baseline"] = {"kind": "run", "run": self.execute(view, spec, "baseline", execution_budget.remaining())}
                    result["candidate"] = self.execute(view, spec, "candidate", execution_budget.remaining())
                    runs = [result["candidate"]] + ([result["baseline"]["run"]] if result["baseline"]["kind"] == "run" else [])
                    if all(run["outcome"] == "matched" for run in runs):
                        result.update(status="recorded", reason="execution matched; final reviewer assessment still required")
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    result["reason"] = str(exc)[:2000]
                # Evidence finalization is local and bounded even after execution expiry.
                with receipts.session(self.shift, self.driver.final_seconds()) as final:
                    path = "logs/reproduction-" + identity + ".json"
                    final.atomic(path, receipts.encoded(result))
                    evidence = dict(path=path, sha256=receipts.digest(receipts.encoded(result)))
                    state["requests"][identity].update(evidence=evidence, status=result["status"])
                    final.atomic("builder-check-state.json", receipts.encoded(state), replace=True)
                return channels.response(identity, result["status"], result["reason"], evidence=evidence, sha=sha)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            return channels.response(identity, "invalid", str(exc))

    def poll(self):
        self.last_request = channels.poll_bytes(self.shift, "builder-check.json", "builder-check-response.json", self.apply, self.last_request, self.driver.answer_seconds,
                                                self.driver.live_seconds)
