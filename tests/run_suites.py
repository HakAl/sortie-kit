"""Run every test in this directory, found by discovery.

    python3 tests/run_suites.py              all of them
    python3 tests/run_suites.py test_report  only the named modules, classes or methods
    python3 tests/run_suites.py --list       print what discovery finds and run nothing

No list of modules decides what runs: a file named `test_*.py` added to this
directory is run.
There is one suite, not a safe one and a signing one. The whole run has the
environment the signing tests need, which costs the others nothing:

- Git reads an empty global and system configuration and an empty hooks
  directory, both made here, so no fixture commit signs, prompts, or runs a hook
  of the person at the keyboard.
- HOME is an empty directory made here and XDG_CONFIG_HOME is removed. Parts of
  the product start Git without any GIT_ variable, and Git then reads the
  configuration and the ignore file of whoever's home it is given.
- The SSH agent, the askpass program and the display are removed and askpass is
  set to never, so a signing test can reach only the throwaway key it makes.

Scratch is kept beneath `.scratch/` in the checkout, which this creates. The exit
status is 0 only when at least one test ran, every test passed, no module failed
to load and discovery found no name twice.
"""
import os
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SCRATCH = ROOT / ".scratch" / "suite"


def isolate():
    """Set this process's environment, which every test and every child inherits."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    empty, hooks, home = SCRATCH / "empty-gitconfig", SCRATCH / "empty-hooks", SCRATCH / "empty-home"
    empty.write_bytes(b"")
    hooks.mkdir(exist_ok=True)
    home.mkdir(mode=0o700, exist_ok=True)
    if any(home.iterdir()):
        raise SystemExit("a test wrote into " + str(home) + ", which is to stay empty; look at it and remove it")
    for name in tuple(os.environ):
        if name.startswith("GIT_") or name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "DISPLAY",
                                               "XDG_CONFIG_HOME"):
            del os.environ[name]
    source = str(ROOT / "src")
    os.environ.update(
        HOME=str(home), SSH_ASKPASS_REQUIRE="never",
        GIT_CONFIG_GLOBAL=str(empty), GIT_CONFIG_SYSTEM=str(empty), GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="false",
        GIT_CONFIG_KEY_1="core.hooksPath", GIT_CONFIG_VALUE_1=str(hooks),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join([source] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                                               if p and p != source]))
    sys.path[:0] = [str(TESTS), source]


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    listing = "--list" in arguments
    quiet = "--quiet" in arguments
    names = [a for a in arguments if a not in ("--list", "--quiet")]
    if any(a.startswith("-") for a in names):
        print(__doc__, file=sys.stderr)
        return 2
    isolate()
    loader = unittest.TestLoader()
    if names:
        tests = list(flatten(loader.loadTestsFromNames(names)))
    else:
        tests = list(flatten(loader.discover(str(TESTS), pattern="test_*.py", top_level_dir=str(TESTS))))
    identities = [test.id() for test in tests]
    twice = sorted({name for name in identities if identities.count(name) > 1})
    if listing:
        print("\n".join(identities))
    print("Python " + sys.version.split()[0] + ": " + str(len(tests)) + " tests"
          + ("" if names else " found by discovery in " + str(TESTS)), flush=True)
    if twice:
        # A class reachable under two names in one module is loaded twice and counted twice.
        print("found more than once:\n  " + "\n  ".join(twice), file=sys.stderr)
        return 1
    if loader.errors:
        print("\n".join(loader.errors), file=sys.stderr)
        return 1
    if listing:
        return 0 if tests else 1
    result = unittest.TextTestRunner(verbosity=1 if quiet else 2).run(unittest.TestSuite(tests))
    return 0 if tests and result.wasSuccessful() and result.testsRun == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
