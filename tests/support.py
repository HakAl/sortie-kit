import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch" / "tests"
NOW = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)


def no_real_wake(*arguments, **options):
    raise AssertionError("a test reached the real hold against idle sleep; hand Wake a fake system or starter")


# No test holds the machine awake: whatever road a test takes to sortie_kit.wake, the
# real system call is not behind it.
from sortie_kit import wake as _wake  # noqa: E402
_wake.mac_libraries = _wake.start_inhibitor = no_real_wake


BLOCK_BEGIN = b"<!-- this repository only: begin -->\n"
BLOCK_END = b"<!-- this repository only: end -->\n"
BLOCK_WORDS = b"this repository only"


def without_repository_block(source):
    """AGENTS.md as it is packaged: without its one block marked as for this repository
    only. A tree that was published has no such block and is returned as it is. One
    marker without the other, a second block, an end before its beginning, or marker
    text that is not one of the two exact lines is an error."""
    counts = source.count(BLOCK_BEGIN), source.count(BLOCK_END)
    if source.lower().count(BLOCK_WORDS) != sum(counts):
        raise ValueError("AGENTS.md: text of a repository-only marker that is not a whole marker line")
    if counts == (0, 0):
        return source
    if counts != (1, 1) or source.index(BLOCK_END) < source.index(BLOCK_BEGIN):
        raise ValueError("AGENTS.md: the repository-only block needs one begin marker and one end marker after it")
    head, rest = source.split(BLOCK_BEGIN)
    block, tail = rest.split(BLOCK_END)
    if (head and not head.endswith(b"\n")) or (block and not block.endswith(b"\n")):
        raise ValueError("AGENTS.md: a repository-only marker has text before it on its line")
    return head + tail


class LocalTest(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.scratch = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def own_home(self):
        """HOME is an empty directory of the test's own, so nothing a test sees depends on
        what the person running it keeps in theirs (a trust file, agent profiles, skills)."""
        from unittest.mock import patch
        home = self.root / "own-home"
        home.mkdir()
        changed = patch.dict(os.environ, HOME=str(home))
        changed.start()
        self.addCleanup(changed.stop)
        return home

    def repo(self):
        repo = self.root / "repo with spaces"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True,
                       capture_output=True, timeout=10)
        (repo / "direction.md").write_text("Implement the selected task.\n")
        return repo


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("negative sleep")
        self.value += seconds

    def utc(self):
        return NOW + dt.timedelta(seconds=self.value)


class FakeChild:
    """A builder process that never runs anything; the test decides when it ends."""
    def __init__(self, stdout=None, stubborn=False):
        self.code, self.signals, self.stdout, self.stubborn = None, [], stdout, stubborn

    def poll(self):
        return self.code

    def terminate(self):
        self.signals.append("terminate")
        if self.code is None and not self.stubborn:
            self.code = -15

    def kill(self):
        self.signals.append("kill")
        if self.code is None:
            self.code = -9


class FakeSpawn:
    """Stands in for the headless builder start; records what would have run."""
    def __init__(self):
        self.started, self.failure, self.stdout, self.stubborn = [], None, None, False
        self.lingering = False  # a descendant keeps the group alive after the leader is reaped

    def __call__(self, argv, *, cwd, env):
        if self.failure is not None:
            raise self.failure
        child = FakeChild(self.stdout, self.stubborn)
        if self.lingering:
            child.group_alive = lambda: self.lingering
        self.started.append(dict(argv=list(argv), cwd=cwd, env=dict(env), child=child))
        return child

    @property
    def child(self):
        return self.started[-1]["child"]


def agent_fixture(root, repo, home=None):
    """A custom agent profile and a harmless program, both outside the repo."""
    from sortie_kit import agents
    home, outside = Path(home or Path(root) / "agent-home"), Path(root) / "agent-files"
    directory = agents.user_directory(home)
    directory.mkdir(parents=True)
    outside.mkdir()
    program = outside / "fixture-agent"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    program = Path(os.path.realpath(program))
    (directory / "fixture.json").write_text(json.dumps(dict(
        version=1, name="fixture", executable=str(program),
        interactive_argv=["--model", "{model}", "{prompt}"],
        headless_argv=["run", "--model", "{model}", "--cd", "{cwd}", "{prompt}"],
        review_argv=["review", "--read-only", "--model", "{model}", "--cd", "{cwd}", "{prompt}"],
        review_model="fixture-review-model",
        models={"fixture-model": "fixture-family", "fixture-review-model": "gemini"})))
    choices = {role: {"agent": "fixture", "model": "fixture-model", "family": None}
               for role in ("driver", "builder")}
    # Named, so that no test's reviewer depends on which agent CLIs the machine running it has.
    choices["reviewer"] = {"agent": "fixture", "model": "fixture-review-model", "family": None}
    return {"home": home, "program": program, "choices": choices}
