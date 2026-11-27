"""One-command start, end to end, with fixture agents in a disposable PTY.

No real agent CLI runs and no developer pane or credential is touched: the
"agents" are one small script from a test profile, the key is synthetic, and a
trap `herdr` on PATH proves the pane tool is never called.
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from sortie_kit import grants, lifecycle, receipts, report as report_words
from sortie_kit.receipt_schema import decode
import test_grants
from test_grants import terminal_run

AGENT = r'''#!{python}
"""Fixture agent: `run ...` is the headless builder, anything else the interactive driver."""
import hashlib, json, os, subprocess, sys
from pathlib import Path
audit = Path({audit!r})
def facts():
    return dict(argv=sys.argv[1:], cwd=os.getcwd(), tty=[os.isatty(n) for n in (0, 1, 2)],
                env={{k: os.environ.get(k) for k in ("HOME", "TMPDIR", "SSH_AUTH_SOCK", "SSH_ASKPASS",
                                                     "GIT_DIR", "GIT_TERMINAL_PROMPT")}})
prompt = sys.argv[-1]
def told(key):
    return [line[len(key):] for line in prompt.splitlines() if line.startswith(key)]
if sys.argv[1] == "run":
    # The builder uses only what its prompt gave it: the toolkit command and the shift.
    import shlex, shutil
    seen = dict(facts(), stdin=os.read(0, 10).decode(), sortie=shutil.which("sortie"), toolkit=told("toolkit: "))
    # What a project's tests do: a Unix socket under the temporary directory, reached through
    # tempfile alone with no path on any command line, and a look for a checkout around it.
    import socket, tempfile
    where = tempfile.gettempdir()
    name = os.path.join(where, "s" * 48)  # a tail of 49 bytes with its separator
    try:
        bound = socket.socket(socket.AF_UNIX)
        bound.bind(name)
        bound.close()
        os.unlink(name)
        seen["socket"] = "bound"
    except OSError as exc:
        seen["socket"] = "failed: " + str(exc)
    os.mkdir(os.path.join(where, "left-by-the-builder"))
    seen.update(temp=where, temp_mode=os.stat(where).st_mode & 0o777, toplevel=subprocess.run(
        ["git", "-C", where, "rev-parse", "--show-toplevel"], capture_output=True, text=True).returncode)
    if told("toolkit: ") and told("shift: "):
        tool, shift = shlex.split(told("toolkit: ")[0]), Path(told("shift: ")[0])
        def toolkit(*args):
            return subprocess.run(tool + list(args), capture_output=True, text=True, timeout=30)
        expected = json.loads((shift / "current-context.json").read_text())["expected"]
        value = json.loads(toolkit("receipt", "template", "acknowledgment").stdout)
        for key in ("shift_id", "task_id", "request_id", "role", "phase", "round", "brief"):
            value[key] = expected[key]
        value["receipt_id"] = "ack-" + expected["request_id"]
        (shift / "logs" / "ack.txt").write_text("read the brief\n")
        value["data"]["evidence"] = [dict(path="logs/ack.txt", sha256=hashlib.sha256(b"read the brief\n").hexdigest())]
        (shift / "logs" / "ack-expected.json").write_text(json.dumps(expected))
        (shift / "logs" / "ack-draft.json").write_text(json.dumps(value))
        done = toolkit("receipt", "write", "--shift", str(shift), "--expected", "logs/ack-expected.json",
                       "--from", "logs/ack-draft.json")
        seen.update(write=done.stdout, write_error=done.stderr, write_code=done.returncode)
    (audit / "builder.json").write_text(json.dumps(seen))
    print("builder fixture output")
    sys.exit(0)
shift = Path([line[7:] for line in prompt.splitlines() if line.startswith("shift: ")][0])
seen = dict(facts(), foreground=os.tcgetpgrp(0) == os.getpgrp(), driver_md=(shift / "driver.md").is_file())
(shift / "briefs" / "task.md").write_text("Fixture task.\n")
brief = dict(path="briefs/task.md", sha256=hashlib.sha256(b"Fixture task.\n").hexdigest())
command = dict(version=1, id="first", op="brief", task_id="task", kind="defect", brief=brief,
               next_owner="builder", turn_seconds=30)
(shift / "driver-command.tmp").write_text(json.dumps(command))
os.replace(shift / "driver-command.tmp", shift / "driver-command.json")
wait = [line for line in prompt.splitlines() if " wait --shift " in line][0]
done = subprocess.run(wait, shell=True, capture_output=True, text=True, timeout=30)
seen.update(wait=json.loads(done.stdout), wait_code=done.returncode)
(audit / "driver.json").write_text(json.dumps(seen))
print("driver fixture done")
sys.exit(0)
'''


FULL = r'''#!{python}
"""Fixture agent for a whole shift: `run ...` is one headless builder turn, `review ...`
one run of the reviewer, anything else the interactive driver. It keeps to what
driver.md, builder.md and REVIEW.md say and to nothing else."""
import hashlib, json, os, re, shlex, shutil, subprocess, sys
from pathlib import Path
audit = Path({audit!r})
prompt = sys.argv[-1]
def told(key):
    return [line[len(key):] for line in prompt.splitlines() if line.startswith(key)]
def note(name, value):
    with open(audit / name, "a") as stream:
        stream.write(json.dumps(value) + "\n")
def digest(raw):
    return hashlib.sha256(raw).hexdigest()

if sys.argv[1] == "review":
    # The reviewer has its directory and REVIEW.md, nothing else: no shift path, no toolkit.
    home = Path(os.environ["HOME"])
    told_here = Path("REVIEW.md").read_text()
    marker = re.search(r"=====BEGIN SORTIE REVIEW ([0-9a-f]{{16}})=====", told_here).group(1)
    shown = json.loads(told_here.split("```json\n")[1].split("```")[0])
    earlier = json.loads(Path("answers.json").read_text()) if Path("answers.json").exists() else None
    commit = shown["target"]["kind"] == "commit"
    first = commit and not (audit / "asked-once").exists()
    if first:
        (audit / "asked-once").write_text("")
    record = dict(schema=shown["schema"], verdict="CHANGES" if first else "ACCEPT", target=shown["target"],
                  findings=[dict(id="F1", severity="major", location="change.patch", claim="The fixture asks once.",
                                 evidence="Nothing: this is the fixture's one request for changes.",
                                 recommendation="Answer it.")] if first else [],
                  prior=[dict(id=item["id"], resolved=True, reason="The builder's answer holds.")
                         for item in (earlier or {{}}).get("earlier_findings", [])],
                  examined=sorted(os.listdir(".")), not_examined=[])
    if "requirements" in shown:  # a commit review answers the brief requirement by requirement
        record["requirements"] = [dict(asked="What the fixture's brief asks for.", status="met", where="change.patch")]
        for item in record["findings"]:  # and each finding names the requirement it says is not delivered, or none
            item["requirement"] = None
    note("reviews.jsonl", dict(argv=sys.argv[1:-1], cwd=os.getcwd(), files=sorted(os.listdir(".")),
                               verdict=record["verdict"], kind=shown["target"]["kind"], answers=earlier,
                               patch=Path("change.patch").read_text() if commit else None,
                               tree=sorted(os.listdir("tree")), prompt_names_toolkit="toolkit: " in prompt,
                               skills=[name for name in (".agents", ".claude", ".codex") if (home / name).exists()],
                               agy=shutil.which("agy"), tty=[os.isatty(n) for n in (0, 1, 2)]))
    print("I read REVIEW.md and what is beside it.")
    print("=====BEGIN SORTIE REVIEW " + marker + "=====")
    print(json.dumps(record))
    print("=====END SORTIE REVIEW " + marker + "=====")
    sys.exit(0)

shift = Path(told("shift: ")[0])
def read(name):
    return json.loads((shift / name).read_text())
def kept(name, content):
    (shift / name).write_text(content)
    return dict(path=name, sha256=digest(content.encode()))

if sys.argv[1] == "run":
    tool = shlex.split(told("toolkit: ")[0])
    def toolkit(*args):
        return subprocess.run(tool + list(args), capture_output=True, text=True, timeout=30)
    context = read("current-context.json")
    expected, request = context["expected"], context["expected"]["request_id"]
    phase, task = expected["phase"], expected["task_id"]
    value = json.loads(toolkit("receipt", "template", phase).stdout)
    for key in ("shift_id", "task_id", "request_id", "role", "phase", "round", "brief"):
        value[key] = expected[key]
    value["receipt_id"] = phase + "-" + request
    data, target = value["data"], None
    answers = [dict(id=key, disposition="rejected", reason="The change is what the brief asks for.", evidence=None)
               for key in expected["required_findings"]]
    if phase == "acknowledgment":
        data["evidence"] = [kept("logs/ack-" + request + ".txt", "read the brief\n")]
    elif phase == "plan":
        data["plan"] = kept("briefs/plan-" + task + "-" + str(expected["round"]) + ".md", "Add one file.\n")
        data["answers"], target = answers, dict(kind="plan", artifact=data["plan"])
    else:
        name = task + ".txt"
        if not Path(name).exists():
            Path(name).write_text("made for " + task + "\n")
            git = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                   "-c", "commit.gpgsign=false"]
            subprocess.run(git + ["add", "--", name], check=True, capture_output=True, timeout=20)
            subprocess.run(git + ["commit", "-qm", "fixture: " + task, "--", name], check=True,
                           capture_output=True, timeout=20)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=20).decode().strip()
        data["commit"], data["touched_files"], data["answers"] = head, [name], answers
        data["checks"][0]["output"] = kept("logs/tests-" + request + ".log", "tests pass\n")
        target = dict(kind="commit", commit=head)
    (shift / "logs" / ("expected-" + request + ".json")).write_text(json.dumps(dict(expected, target=target)))
    (shift / "logs" / ("draft-" + request + ".json")).write_text(json.dumps(value))
    done = toolkit("receipt", "write", "--shift", str(shift), "--expected", "logs/expected-" + request + ".json",
                   "--from", "logs/draft-" + request + ".json")
    if target is not None:
        # What the builder tells the driver: the exact target of the receipt it wrote.
        (shift / "logs" / ("target-" + request + ".json")).write_text(json.dumps(target))
    note("turns.jsonl", dict(phase=phase, task=task, round=expected["round"], code=done.returncode, said=done.stdout,
                             findings=context["findings"], required=expected["required_findings"],
                             argv=sys.argv[1:-1], review_tools=[name for name in ("agy", "claude", "codex")
                                                                if shutil.which(name)]))
    print("builder fixture: " + phase + " receipt " + ("written" if done.returncode == 0 else "REFUSED " + done.stdout))
    sys.exit(0)

# The driver: one command, then the wait, then whatever the stage asks for.
wait_line = [line for line in prompt.splitlines() if " wait --shift " in line][0]
sent = []
def send(op, **fields):
    command = dict(version=1, id="cmd-" + str(len(sent) + 1), op=op, **fields)
    (shift / "driver-command.tmp").write_text(json.dumps(command))
    os.replace(shift / "driver-command.tmp", shift / "driver-command.json")
    while True:
        answer = json.loads(subprocess.run(wait_line, shell=True, capture_output=True, text=True, timeout=120).stdout)
        if answer["status"] != "waiting":
            break
    sent.append(dict(op=op, status=(answer.get("response") or {{}}).get("status"), wait=answer["status"],
                     stage=answer.get("stage"), review=(answer.get("last_review") or {{}}).get("outcome"),
                     detail=(answer.get("response") or {{}}).get("detail")))
    note("driver.jsonl", sent[-1])
    return answer
def drive():
    for task, kind in (("feature-task", "feature"), ("defect-task", "defect")):
        brief = kept("briefs/" + task + ".md", "Add the file " + task + ".txt.\n")
        answer = send("brief", task_id=task, kind=kind, brief=brief, next_owner="builder", turn_seconds=60)
        for _ in range(24):
            if answer["status"] != "ready":
                break
            state = read("driver-state.json")
            if state["active"] != task:
                break
            entry = state["tasks"][task]
            stage, ids = entry["stage"], dict(task_id=task, request_id=entry["request_id"])
            phase = dict(ack="acknowledgment", plan="plan", build="build").get(stage)
            if phase:
                receipt = "receipts/" + phase + "-" + entry["request_id"] + ".json"
                if not (shift / receipt).exists():
                    answer = send("continue", turn_seconds=60, **ids)
                elif phase == "acknowledgment":
                    answer = send("consume", receipt=receipt, **ids)
                else:
                    answer = send("consume", receipt=receipt, target=read("logs/target-" + entry["request_id"] + ".json"), **ids)
            elif stage == "repair_needed":
                answer = send("repair", reason="The review asked for changes: " + answer["last_review"]["detail"][:200],
                              next_owner="builder", **ids)
            elif stage == "accepted":
                answer = send("close", **ids)
                break
            else:
                break
    send("finish", reason="Both tasks were taken as far as the fixture goes.", next_owner="developer", not_started=[])
try:
    drive()
except BaseException:
    # Kept for the test to show: the driver's own terminal is the shift's, not the test's.
    import traceback
    (audit / "driver-crash.txt").write_text(traceback.format_exc())
    raise
print("driver fixture done")
sys.exit(0)
'''


class LiveStartTests(test_grants.GrantTests):
    def no_agents(self):
        """The environment of a machine with no agent CLI on its search path and no skills
        directory in its home: git, and the system's own tools, and nothing else."""
        import shutil
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "git").symlink_to(shutil.which("git"))
        search = os.pathsep.join([str(tools), "/usr/bin", "/bin"])
        for name in ("claude", "codex", "agy", "sortie"):
            if shutil.which(name, path=search):
                self.skipTest(name + " is among the system's own tools here; this test needs a path without it")
        for name in (".agents", ".claude", ".codex"):
            self.assertFalse((self.protected / name).exists())
        return dict(self.env, PATH=search)

    def test_a_whole_shift_runs_with_no_skills_directory_and_no_agent_cli_on_the_path(self):
        """The acceptance test of the reviewer inside sortie-kit: a feature and a defect, each
        through its reviews by the host, both accepted; the reviewer is picked, not named."""
        audit = self.root / "audit"
        audit.mkdir()
        self.fixture["program"].write_text(FULL.format(python=sys.executable, audit=str(audit)))
        secret = self.encrypted_fixture()
        subprocess.run(["git", "-C", str(self.repo_path), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-q",
                        "--allow-empty", "-m", "base"], check=True, capture_output=True, timeout=10)
        before = set((self.repo_path / ".sortie/shifts").iterdir())
        code, output, prompts = terminal_run(
            [sys.executable, "-B", "-m", "sortie_kit", "start", "--repo", str(self.repo_path),
             "--direction", str(self.request.direction), "--for", "45m",
             "--trust", str(self.trust_path), "--key", str(self.key),
             "--agent", "driver=fixture:fixture-model", "--agent", "builder=fixture:fixture-model"],
            self.no_agents(), secret, timeout=150)
        text = output.decode(errors="replace").replace("\r", "")
        rows = lambda name: [json.loads(line) for line in (audit / name).read_text().splitlines()]
        trace = rows("driver.jsonl") if (audit / "driver.jsonl").exists() else []
        crash = (audit / "driver-crash.txt").read_text() if (audit / "driver-crash.txt").exists() else "no driver crash"
        self.assertEqual(code, 0, text + repr(trace) + crash)
        self.assertEqual(prompts, 1)
        # `finish` ends the shift and the host then stops the driver, which may or may not have
        # read the answer and printed its last line by then: neither is looked for.
        self.assertEqual(crash, "no driver crash")
        self.assertIn("\nShift ended: Both tasks were taken as far as the fixture goes.\n", text, repr(trace))
        shift, = set((self.repo_path / ".sortie/shifts").iterdir()) - before
        # Shown and signed, never asked: who reviews and how that was chosen.
        signed = text[text.index("Shift to sign"):text.index("\nShift ended: ")]
        self.assertIn("picked: the first installed agent of another family than the builder", signed)
        value = grants.decoded(grants.verify(shift, self.trust)[0])
        self.assertEqual((value["version"], value["review_policy"]["review_choice"], value["seats"]["reviewer"]["agent"]),
                         (4, "picked-other-family", "fixture"))
        tail = text.split("\nShift ended: ")[1]
        self.assertIn("Tasks: 2 accepted locally of 2 briefed", tail)
        self.assertIn("Shutdown: confirmed.\n", tail)
        # The driver sent what driver.md says and never a review receipt or a review target.
        # (A `finish` row, when the driver lived to write one, may have no answer: the shift is closed.)
        self.assertEqual([(row["op"], row["status"]) for row in trace
                          if row["op"] != "finish" and row["status"] not in ("advanced", "recorded", "closed")], [], trace)
        self.assertEqual([row["op"] for row in trace if row["op"] != "finish"],
                         ["brief", "consume", "continue", "consume", "continue", "consume",                # feature
                          "repair", "continue", "consume", "close",
                          "brief", "consume", "continue", "consume", "close"])                              # defect
        self.assertEqual([row["review"] for row in trace if row["op"] in ("consume", "close")][-2:], ["ACCEPT", "ACCEPT"])
        # Four reviews, each started by the host in a directory of its own with the signed review form.
        reviews = rows("reviews.jsonl")
        self.assertEqual([(row["kind"], row["verdict"]) for row in reviews],
                         [("plan", "ACCEPT"), ("commit", "CHANGES"), ("commit", "ACCEPT"), ("commit", "ACCEPT")])
        for row in reviews:
            self.assertEqual(row["argv"][:4], ["review", "--read-only", "--model", "fixture-review-model"])
            self.assertEqual(Path(row["cwd"]).parent, (shift / "reviews").resolve())
            self.assertEqual((row["argv"][5], row["tty"], row["skills"], row["agy"], row["prompt_names_toolkit"]),
                             (row["cwd"], [False] * 3, [], None, False))
            self.assertIn("REVIEW.md", row["files"])
        self.assertEqual(len({row["cwd"] for row in reviews}), 4)
        # The first commit review is of the feature's whole change; the builder's answer to its
        # one finding reaches the next review, whose `prior` settles it.
        self.assertIn("+made for feature-task", reviews[1]["patch"])
        self.assertIsNone(reviews[1]["answers"])
        self.assertEqual([(item["id"], item["disposition"]) for item in reviews[2]["answers"]["builder_answers"]],
                         [("F1", "rejected")])
        self.assertIn("feature-task.txt", reviews[2]["tree"])
        self.assertNotIn("feature-task", reviews[3]["patch"])  # the defect's patch begins where the defect began
        self.assertIn("+made for defect-task", reviews[3]["patch"])
        # No builder turn ran, named or found a review tool; the findings reached it as a file.
        turns = rows("turns.jsonl")
        self.assertEqual([(row["task"], row["phase"], row["round"], row["code"]) for row in turns],
                         [("feature-task", "acknowledgment", 0, 0), ("feature-task", "plan", 0, 0),
                          ("feature-task", "build", 0, 0), ("feature-task", "build", 1, 0),
                          ("defect-task", "acknowledgment", 0, 0), ("defect-task", "build", 0, 0)])
        self.assertEqual([row["review_tools"] for row in turns], [[]] * 6)
        self.assertEqual((turns[3]["required"], turns[3]["findings"]["path"].startswith("briefs/findings-")), (["F1"], True))
        for log in sorted((shift / "logs").glob("turn-*")):
            self.assertNotRegex(log.read_text(), "agy|skill|check-review")
        # The report: both accepted, each backed by a review receipt the host wrote.
        report = decode((shift / "report.json").read_bytes())
        self.assertEqual({task["id"]: task["acceptance"] for task in report["tasks"]},
                         {"feature-task": report_words.ACCEPTED, "defect-task": report_words.ACCEPTED})
        state = decode((shift / "driver-state.json").read_bytes())
        for task in state["tasks"].values():
            receipt = decode((shift / task["accepted"]["receipt"]["path"]).read_bytes())
            self.assertEqual((receipt["version"], receipt["role"], receipt["phase"], receipt["data"]["outcome"]),
                             (3, "reviewer", "review", "ACCEPT"))
            attempt = receipt["data"]["attempts"][-1]
            self.assertEqual(attempt["argv"][0], str(self.fixture["program"]))
            self.assertEqual(hashlib.sha256((shift / attempt["stdout"]["path"]).read_bytes()).hexdigest(),
                             attempt["stdout"]["sha256"])
        self.assertEqual({name: len(task["reviews"]) for name, task in state["tasks"].items()},
                         {"feature-task": 3, "defect-task": 1})

    def test_one_command_signs_admits_runs_both_seats_and_reports(self):
        audit = self.root / "audit"
        audit.mkdir()
        # The profile already names this path; its content is pinned when the start signs.
        self.fixture["program"].write_text(AGENT.format(python=sys.executable, audit=str(audit)))
        # Another sortie_kit in the target repository, where both seats are started:
        # the command the host gives them must not run it.
        (self.repo_path / "sortie_kit").mkdir()
        (self.repo_path / "sortie_kit/__init__.py").write_text("")
        (self.repo_path / "sortie_kit/__main__.py").write_text("print('another toolkit')\nraise SystemExit(7)\n")
        secret = self.encrypted_fixture()
        trap = self.root / "trap-bin"
        trap.mkdir()
        (trap / "herdr").write_text("#!/bin/sh\ntouch '" + str(audit / "herdr-ran") + "'\nexit 1\n")
        (trap / "herdr").chmod(0o755)
        # No `sortie` anywhere on the PATH the seats get: the README starts a shift as
        # venv/bin/sortie without activating the environment.
        bare = [d for d in os.environ["PATH"].split(os.pathsep) if d and not os.path.exists(os.path.join(d, "sortie"))]
        env = dict(self.env, PATH=os.pathsep.join([str(trap)] + bare),
                   SSH_AUTH_SOCK=str(self.root / "agent-socket"), GIT_DIR=str(self.root / "elsewhere"))
        before = set((self.repo_path / ".sortie/shifts").iterdir())
        code, output, prompts = terminal_run(
            [sys.executable, "-B", "-m", "sortie_kit", "start", "--repo", str(self.repo_path),
             "--direction", str(self.request.direction), "--for", "30m",
             "--trust", str(self.trust_path), "--key", str(self.key),
             "--agent", "driver=fixture:fixture-model", "--agent", "builder=fixture:fixture-model",
             "--agent", "reviewer=fixture:fixture-review-model"], env, secret, timeout=60)
        text = output.decode(errors="replace")
        self.assertEqual(code, 0, text)
        self.assertEqual(prompts, 1)
        shift, = set((self.repo_path / ".sortie/shifts").iterdir()) - before
        self.assertIn("driver fixture done", text)
        tail = text.split("driver fixture done")[1]
        for part in ("Shift ended: driver exited\nTasks: 0 accepted locally of 1 briefed; whether the direction held more is not known to the "
                     "host.\nShutdown: confirmed.\n",
                     "Driver process: exited on its own with status 0",
                     "Report: " + str(shift / "report.md")):
            self.assertIn(part, tail.replace("\r", ""))
        self.assertFalse((audit / "herdr-ran").exists(), "the pane tool was called")

        driver = json.loads((audit / "driver.json").read_text())
        self.assertEqual((driver["tty"], driver["foreground"], driver["driver_md"]), ([True] * 3, True, True))
        self.assertEqual(driver["cwd"], os.path.realpath(self.repo_path))
        self.assertEqual(driver["argv"][:2], ["--model", "fixture-model"])
        self.assertEqual((driver["wait_code"], driver["wait"]["status"], driver["wait"]["turn"]), (0, "ready", None))
        self.assertEqual((driver["wait"]["response"]["status"], driver["wait"]["last_turn"]["outcome"],
                          driver["wait"]["last_turn"]["exit"]), ("recorded", "exit", 0))
        # The fixture builder wrote a receipt, so the driver is not told that none appeared.
        self.assertNotIn("looked the same", str(driver["wait"].get("note")))

        builder = json.loads((audit / "builder.json").read_text())
        # With no `sortie` to find, the builder ran the toolkit as its prompt named it
        # and wrote a receipt the toolkit takes.
        self.assertIsNone(builder["sortie"])
        self.assertEqual(builder["toolkit"], [lifecycle.toolkit()])
        self.assertEqual(builder.get("write_code"), 0, builder)
        written = json.loads(builder["write"])
        self.assertEqual((written["status"], written["check"]["status"]), ("written", "diagnostic"))
        self.assertRegex(written["path"], r"^receipts/ack-[0-9a-f]{32}\.json$")
        self.assertEqual(hashlib.sha256((shift / written["path"]).read_bytes()).hexdigest(), written["sha256"])
        self.assertEqual(receipts.validate_receipt(shift, written["path"], "logs/ack-expected.json")["status"],
                         "diagnostic")
        self.assertEqual(builder["argv"][:5], ["run", "--model", "fixture-model", "--cd", os.path.realpath(self.repo_path)])
        self.assertIn("briefs/task.md", builder["argv"][5])
        self.assertEqual((builder["cwd"], builder["tty"], builder["stdin"]),
                         (os.path.realpath(self.repo_path), [False] * 3, ""))
        report = decode((shift / "report.json").read_bytes())
        window = report["actual_window"]
        # The builder's temporary directory is the one the host made for this shift in the
        # system's short location: outside the repository and any checkout, short enough
        # for a Unix socket with a tail of 49 bytes, the developer's alone, and gone at the end.
        from sortie_kit import turn_temp
        self.assertTrue(turn_temp.BASES, "no short system location is named in turn_temp.BASES")
        temp = window["temp"]
        self.assertEqual((temp["state"], temp["entries"], temp["removed_entries"]), ("removed", 1, 1), temp)
        self.assertEqual(os.path.dirname(temp["path"]), os.path.realpath(turn_temp.BASES[0]))
        self.assertLessEqual(temp["bytes"], 28)
        self.assertEqual(builder["env"], dict(HOME=str(self.protected), TMPDIR=temp["path"],
                                              SSH_AUTH_SOCK=None, SSH_ASKPASS=None, GIT_DIR=None,
                                              GIT_TERMINAL_PROMPT="0"))
        self.assertEqual((builder["temp"], builder["temp_mode"], builder["socket"]), (temp["path"], 0o700, "bound"))
        self.assertNotEqual(builder["toplevel"], 0)  # Git finds no checkout there
        self.assertFalse(os.path.lexists(temp["path"]))
        self.assertIn("- Temporary directory of builder turns and reviews: " + report_words.plain(temp["path"], 400)
                      + " (" + str(temp["bytes"]) + " bytes), outside the repository. The host removed it at the "
                      "end, with 1 entry in it.\n", (shift / "report.md").read_text())
        self.assertEqual((window["reason"], window["processes"]["driver"]),
                         ("driver exited", "exited on its own with status 0"))
        turn, = report["tasks"][0]["turns"]
        self.assertEqual((turn["turn"], turn["outcome"], turn["exit"], turn["reaped"]), (1, "exit", 0, True))
        self.assertEqual((turn["receipts_changed"], turn["limit_seconds"]), (True, 30))
        self.assertLessEqual(turn["elapsed_seconds"], 30)
        self.assertEqual((shift / turn["log"]).read_text(), "builder fixture output\n")
        self.assertEqual(report["tasks"][0]["acceptance"], "acceptance missing")
        self.assertIn("driver started", (shift / "logs/host.log").read_text())
        # The one real hold in the suites: this start is the command itself, so on macOS the
        # host's own power assertion was held for these seconds and let go.
        shown = text.replace("\r", "")
        self.assertLess(shown.index("\nNot part of what is signed:\n  Keep awake: "), shown.index("\nExact bytes being signed:\n"))
        # Where the temporary files will be is said to the signer too, and is not signed either.
        aside = shown.split("\nNot part of what is signed:\n")[1].split("\nExact bytes being signed:\n")[0]
        self.assertIn("\n  Temporary files: builder turns and reviews get a directory the host makes when the shift "
                      "starts, outside the repository and open to your user alone. As things are now that would be "
                      "under " + os.path.dirname(temp["path"]) + "; the place is looked at again then", aside)
        self.assertNotIn("Temporary files", shown.split("\nExact bytes being signed:\n")[1].split("\n")[0])
        # The fixture agent is a script: the signer is told what its pin does not cover, last and not signed.
        self.assertIn("\n  Pinned script: the pinned file of the driver, the builder and the reviewer, ", aside)
        self.assertLess(aside.index("Temporary files: "), aside.index("Pinned script: "))
        self.assertNotIn("Pinned script", shown.split("\nExact bytes being signed:\n")[1].split("\n")[0])
        self.assertTrue(window["wake"]["asked"])
        if sys.platform == "darwin":
            self.assertEqual((window["wake"]["whole"], window["wake"]["refusals"], window["wake"]["gaps"]), (True, [], []))
            self.assertIn("Kept awake: for the whole shift, by a macOS power assertion", (shift / "report.md").read_text())
        value = grants.decoded(grants.verify(shift, self.trust)[0])
        self.assertEqual(sorted(value["allowed_actions"]),
                         ["local-work", "spawn-builder", "spawn-driver", "spawn-reviewer"])
        self.assertEqual(value["review_policy"]["review_choice"], "given")
        # The grant is spent: the same shift cannot be admitted again.
        with self.assertRaisesRegex(Exception, "replay refused|already admitted|stale|expired|closed"):
            grants.Admission(shift, self.trust, seconds=60, expected_seats=value["seats"])


    def test_guided_start_asks_in_the_terminal_then_signs_what_was_answered(self):
        secret = self.encrypted_fixture()
        before = set((self.repo_path / ".sortie/shifts").iterdir())
        code, output, prompts = terminal_run(
            [sys.executable, "-B", "-m", "sortie_kit", "start", "--repo", str(self.repo_path),
             "--direction", str(self.request.direction), "--sign-only",
             "--trust", str(self.trust_path), "--key", str(self.key), "--agent", "driver=fixture:fixture-model"],
            self.no_agents(), secret, timeout=60, answers=[
                (b"Builder agent:", b"fixture"), (b"Builder model:", b"not a model"), (b"Builder model:", b"1"),
                (b"How long may the shift run:", b"30m")])
        text = output.decode(errors="replace").replace("\r", "")
        self.assertEqual(code, 0, text)
        self.assertEqual(prompts, 1)
        # Every question comes before the scope is shown and before the passphrase is asked for.
        self.assertLess(text.index("How long may the shift run"), text.index("Shift to sign"))
        self.assertIn("that is not a model ID", text)
        self.assertNotIn("Driver agent", text)
        # Who reviews is not asked: it is picked, and shown in what is signed. On the search path
        # of this test no agent CLI is installed but the fixture, whose review model is of another family.
        asked = text[:text.index("Shift to sign")]
        self.assertNotIn("Review", asked)
        shown = text[text.index("Shift to sign"):]
        self.assertIn("picked: the first installed agent of another family than the builder", shown)
        self.assertIn("What the reviewer can reach:", shown)
        self.assertNotIn("same-family review", shown)
        shift, = set((self.repo_path / ".sortie/shifts").iterdir()) - before
        value = grants.decoded(grants.verify(shift, self.trust)[0])
        self.assertEqual([(value["seats"][r]["agent"], value["seats"][r]["model"]) for r in ("driver", "builder")],
                         [("fixture", "fixture-model")] * 2)
        policy = value["review_policy"]
        self.assertEqual((policy["review_model"], policy["review_family"], policy["review_choice"],
                          policy["same_family_review"]),
                         ("fixture-review-model", "gemini", "picked-other-family", False))
        reviewer = value["seats"]["reviewer"]
        self.assertEqual((reviewer["agent"], reviewer["model"], reviewer["mode"], reviewer["executable"]["path"]),
                         ("fixture", "fixture-review-model", "review", str(self.fixture["program"])))
        self.assertEqual(value["review_limit_seconds"], 600)
        window = grants.moment(value["until"]) - grants.moment(value["start"])
        self.assertEqual(window.total_seconds(), 1800)

    def test_guided_start_without_a_terminal_exits_2_listing_what_is_missing(self):
        before = set((self.repo_path / ".sortie/shifts").iterdir())
        done = subprocess.run(
            [sys.executable, "-B", "-m", "sortie_kit", "start", "--repo", str(self.repo_path),
             "--direction", str(self.request.direction), "--trust", str(self.trust_path), "--key", str(self.key),
             "--agent", "driver=fixture:fixture-model"],
            env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("sortie: missing: --agent builder=NAME:MODEL; --for DURATION; "
                      "no terminal to ask in", done.stderr)
        self.assertEqual(set((self.repo_path / ".sortie/shifts").iterdir()), before)

    def launch_failure(self, launch, message):
        from sortie_kit import launcher
        before = {s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        with self.assertRaisesRegex(Exception, message):
            launcher.run(self.shift, self.trust, self.seats, 60, launch=launch,
                         serve=lambda *a, **k: self.fail("the host loop must not start"))
        self.assertEqual({s: signal.getsignal(s) for s in before}, before)
        report = decode((self.shift / "report.json").read_bytes())
        self.assertEqual(report["actual_window"]["source"], "admitted start failed")
        self.assertRegex(report["actual_window"]["reason"], message)

    def test_stop_signal_during_the_handoff_is_held_until_the_terminal_is_back(self):
        closed = []
        class Seat:
            def __init__(inner, argv, *, cwd, env):
                self.assertEqual(argv[0], str(self.fixture["program"]))
                os.kill(os.getpid(), signal.SIGTERM)  # would end the host if it were unhandled
            def close(inner):
                # The terminal comes back before the report is written, and only once matters.
                closed.append((self.shift / "report.json").exists())
                return "stopped by the host"
        self.launch_failure(Seat, "interrupted while starting the driver")
        self.assertEqual(closed[0], False)
        window = decode((self.shift / "report.json").read_bytes())["actual_window"]
        self.assertEqual(window["processes"], {"driver": "stopped by the host", "cleanup": "done"})
        self.assertIn("driver process: stopped by the host", (self.shift / "logs/host.log").read_text())

    def test_a_stop_signal_noted_before_the_driver_start_starts_no_driver(self):
        from unittest.mock import patch
        real, argv = grants.Admission.guard, []
        def noting(admission):
            os.kill(os.getpid(), signal.SIGTERM)
            return real(admission)
        def never(argv, *, cwd, env):
            self.fail("the driver must not start")
        with patch.object(grants.Admission, "guard", noting), \
                patch.object(grants.Admission, "seat_start", lambda *a: argv.append(a)):
            self.launch_failure(never, "interrupted before the driver start")
        self.assertEqual(argv, [])  # not even the start checks ran

    def test_a_stop_signal_noted_while_the_driver_argv_is_checked_starts_no_driver(self):
        from unittest.mock import patch
        real = grants.Admission.seat_start
        def noting(admission, *args):
            os.kill(os.getpid(), signal.SIGTERM)
            return real(admission, *args)
        def never(argv, *, cwd, env):
            self.fail("the driver must not start")
        with patch.object(grants.Admission, "seat_start", noting):
            self.launch_failure(never, "interrupted before the driver start")

    def test_host_note_never_waits_on_a_pipe_at_the_log_path(self):
        import threading
        from sortie_kit import launcher
        path = self.shift / "logs" / "host.log"
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            path.unlink()
        os.mkfifo(path)
        thread = threading.Thread(target=launcher.host_log, args=(self.shift, "note"), daemon=True)
        thread.start()
        thread.join(3)
        stuck = thread.is_alive()
        if stuck:  # release the blocked open so the test process can end
            os.close(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            thread.join(3)
        self.assertFalse(stuck)

    def test_the_hold_is_taken_after_admission_and_let_go_when_the_start_fails_and_the_report_has_it(self):
        from test_run4 import FakeWake
        held, seen = FakeWake(self.shift), []

        def launch(argv, *, cwd, env):
            seen.append(list(held.calls))
            raise subprocess.SubprocessError("no driver for this fixture")
        from sortie_kit import launcher
        with self.assertRaisesRegex(Exception, "no driver for this fixture"):
            launcher.run(self.shift, self.trust, self.seats, 60, launch=launch, wake=held,
                         serve=lambda *a, **k: self.fail("the host loop must not start"))
        self.assertEqual(seen, [["acquire"]])  # held before the driver is started
        self.assertEqual((held.calls[0], held.calls[-1], held.calls.count("release")), ("acquire", "release", 1))
        self.assertEqual(held.files, [True, True])
        report = decode((self.shift / "report.json").read_bytes())
        self.assertEqual(report["actual_window"]["wake"]["means"], "a fixture")

    def test_the_hold_is_handed_to_the_host_loop_and_let_go_once_after_it(self):
        from test_run4 import FakeWake
        from sortie_kit import launcher
        held, got = FakeWake(self.shift), {}

        class Seat:
            def __init__(self, argv, *, cwd, env):
                pass

            def close(self):
                return "stopped by the host"

        def serve(admission, **options):
            got.update(options)
            return 0, "served"
        launcher.run(self.shift, self.trust, self.seats, 60, launch=Seat, serve=serve, wake=held)
        self.assertIs(got["wake"], held)
        self.assertEqual((held.calls[0], held.calls.count("release")), ("acquire", 1))

    def test_one_line_is_printed_just_before_the_driver_takes_the_terminal(self):
        """In every new clone the first screen of a real driver was its CLI's own question
        whether to trust the folder, with the answer that exits preselected: one Enter ended
        a signed shift. The host cannot know what a CLI has trusted, so the line is always said."""
        import contextlib
        import io
        from sortie_kit import launcher
        out, seen = io.StringIO(), []

        class Seat:
            def __init__(inner, argv, *, cwd, env):
                seen.append(out.getvalue())  # what the terminal held when the driver took it

            def close(inner):
                return "stopped by the host"

        with contextlib.redirect_stdout(out):
            launcher.run(self.shift, self.trust, self.seats, 60, launch=Seat, serve=lambda admission, **options: (0, "served"))
        self.assertEqual(seen, [launcher.DRIVER_QUESTION + "\n"])
        self.assertEqual(out.getvalue().count(launcher.DRIVER_QUESTION), 1)
        said = launcher.DRIVER_QUESTION
        self.assertNotIn("\n", said)
        for part in ("a question of its own", "trust the folder", "Read it before pressing Enter",
                     "preselected answer can be the one that declines and exits", "a driver that exits ends the shift"):
            self.assertIn(part, said)

    def test_no_line_on_the_drivers_first_question_when_no_driver_is_started(self):
        import contextlib
        import io
        from sortie_kit import launcher
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            def refused(inner, role, prompt):
                raise ValueError("driver agent executable replaced or rewritten after admission")
            from unittest.mock import patch
            with patch.object(grants.Admission, "seat_start", refused), self.assertRaises(Exception):
                launcher.run(self.shift, self.trust, self.seats, 60,
                             launch=lambda *a, **k: self.fail("no driver may start"),
                             serve=lambda *a, **k: self.fail("the host loop must not start"))
        self.assertNotIn(launcher.DRIVER_QUESTION, out.getvalue())

    def test_the_maker_of_the_temporary_directory_is_handed_to_the_host_loop_and_makes_nothing_before_it(self):
        from sortie_kit import launcher
        got, made = {}, []

        class Seat:
            def __init__(self, argv, *, cwd, env):
                pass

            def close(self):
                return "stopped by the host"

        def serve(admission, **options):
            got.update(options)
            return 0, "served"
        maker = lambda repo: made.append(repo)
        launcher.run(self.shift, self.trust, self.seats, 60, launch=Seat, serve=serve, temp=maker)
        self.assertIs(got["temp"], maker)
        self.assertEqual(made, [])  # the host loop makes it, and this stand-in loop made none

    def test_child_setup_failure_still_gets_the_start_failed_report(self):
        def launch(argv, *, cwd, env):
            raise subprocess.SubprocessError("Exception occurred in preexec_fn.")
        self.launch_failure(launch, "preexec_fn")


for _name in tuple(vars(test_grants.GrantTests)):
    if _name.startswith("test_"):
        setattr(LiveStartTests, _name, None)
