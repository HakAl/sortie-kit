"""Run the documented commands, as written, from an install into a fresh venv.

The commands are those of README.md and of the pages under docs/ that the README's
longer sections moved to. Each is looked for in the one page that documents it, named
where it is run below, so a command that leaves its page or stops working fails this run.

Usage: python3 tests/readme_run.py [EXPORTED_TREE]
Without a tree, the commit this checkout is at is exported with `git archive` and
used, so that the install leaves nothing in the checkout. Everything this makes is
in a directory of its own under `.scratch/readme-run/`, which it creates; a tree
given by name also gets what `pip install .` and the test runner leave in it.
A fixture agent stands in for claude and codex and is the picked reviewer; PATH holds
no real agent CLI, so none is pinned or started. The key is made here with a random
passphrase that is never a user credential. HOME is a directory of this run's own.
"""
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "tests"), str(root / "src")]
from test_grants import terminal_run  # noqa: E402
from test_start_live import AGENT  # noqa: E402
from support import agent_fixture  # noqa: E402

scratch = root / ".scratch" / "readme-run"
scratch.mkdir(parents=True, exist_ok=True)
work = Path(tempfile.mkdtemp(prefix="run-", dir=scratch))
home, venv, repo, audit, building = work / "home", work / "venv", work / "project", work / "audit", work / "build"
home.mkdir(mode=0o700)
audit.mkdir()
building.mkdir()
if len(sys.argv) > 1:
    tree = Path(sys.argv[1]).resolve()
else:
    tree = work / "tree"
    tree.mkdir()
    archive = subprocess.run(["git", "-C", str(root), "archive", "HEAD"], check=True, capture_output=True, timeout=60,
                             env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive.stdout, check=True, timeout=60)
GRANTS = "docs/grants.md"
README, WALK, WHO, EXAMPLE, RUNNING, DEVELOPMENT = ("README.md", "docs/walkthrough.md", "docs/who-reviews.md",
                                                      "docs/example.md", "docs/running.md", "docs/development.md")
pages = {name: (tree / name).read_text() for name in (README, WALK, WHO, EXAMPLE, RUNNING, DEVELOPMENT, GRANTS)}
readme = pages[README]
done = []


def said(text, page=README):
    """The command must be in that page as it is run here, apart from the stated substitutions."""
    assert text in pages[page], "not in " + page + ": " + text
    return text


base = {k: v for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "DISPLAY",
                     "XDG_CONFIG_HOME", "VIRTUAL_ENV", "EMAIL")
        and not k.startswith("GIT_")}
empty = work / "empty-gitconfig"
empty.touch()
# The seats' PATH: this run's interpreter and the system directories, and no agent CLI of
# the machine, so that the reviewer the start picks can only be the fixture.
import shutil  # noqa: E402
tools = work / "tools"
tools.mkdir()
for name in ("python3", "python"):
    (tools / name).symlink_to(sys.executable)
seat_path = os.pathsep.join([str(venv / "bin"), str(tools), "/usr/bin", "/bin"])
for name in ("claude", "codex", "agy"):
    assert shutil.which(name, path=seat_path) is None, name + " is on the PATH of this run"
env = dict(base, HOME=str(home), TMPDIR=str(building), PYTHONDONTWRITEBYTECODE="1",
           PATH=seat_path, GIT_CONFIG_GLOBAL=str(empty),
           GIT_CONFIG_SYSTEM=str(empty), GIT_TERMINAL_PROMPT="0")


def sh(command, cwd, expected=0, note="", env=None):
    result = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, env=env or globals()["env"], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == expected, (command, result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    done.append(dict(command=command, exit=result.returncode, note=note))
    return result


def tty(command, cwd, secret, answers=(), expected=0, prompts=None, note=""):
    here = os.getcwd()
    os.chdir(cwd)
    try:
        code, output, asked = terminal_run(["/bin/sh", "-c", command], env, secret, timeout=120, answers=answers)
    finally:
        os.chdir(here)
    text = output.decode(errors="replace").replace("\r", "")
    assert code == expected, (command, code, text[-3000:])
    assert prompts is None or asked == prompts, (command, asked, text[-3000:])
    done.append(dict(command=command, exit=code, passphrase_prompts=asked, note=note))
    return text


# Step 1: install, from the checkout, into a fresh environment: the two commands as written, with
# `.venv` as this run's own directory so that nothing is made in the tree. `python3` is this
# run's interpreter, from the directory of tools above.
import shlex  # noqa: E402
for line in ("python3 -m venv .venv", ".venv/bin/python -m pip install ."):
    sh(said(line).replace(".venv", shlex.quote(str(venv))), tree,
       note="README: " + line + ", with .venv as this run's directory")
assert Path(sh("command -v sortie", work).stdout.strip()) == venv / "bin/sortie"

# Step 2: the look before anything is made. This run's PATH has no agent CLI, so no
# program of an agent is run: the report says that none was found, that this installation's
# sortie is the first on the PATH, and that there is no key yet. It writes nothing.
looked = sh(said("sortie doctor"), work, expected=1, note="before setup: exit 1 with what to do").stdout
assert looked.startswith("sortie-kit ") and ", Python " in looked.splitlines()[0], looked
assert "The sortie first on your PATH: " + str(venv / "bin/sortie") + " (this installation)." in looked, looked
assert "- Run sortie setup once in your own terminal" in looked and "No agent CLI was found" in looked, looked
assert not (home / ".config").exists()

# Step 3: the key and the trust file by the one command, with ssh-keygen's own prompts
# answered in a disposable terminal. Without a terminal the command refuses and makes nothing.
secret = secrets.token_urlsafe(24).encode()  # never a user credential
refused = sh(said("sortie setup"), work, expected=3, note="no terminal: refused").stderr
assert "sortie setup needs a terminal and has none it can use" in refused, refused
assert not (home / ".config").exists()
shown = tty(said("sortie setup"), work, secret, prompts=2, note="synthetic passphrase typed at ssh-keygen's two prompts")
config = home / ".config/sortie-kit"
assert "run with this home directory, " + str(home) + ", finds both there" in shown, shown
assert ("--trust " + shlex.quote(str(config / "trust.json")) + " --key " + shlex.quote(str(config / "signing-key"))
        in shown), shown
assert stat.S_IMODE(config.stat().st_mode) == 0o700, oct(config.stat().st_mode)
assert sorted(p.name for p in config.iterdir()) == ["signing-key", "signing-key.pub", "trust.json"]
assert all(stat.S_IMODE((config / n).stat().st_mode) == 0o600 for n in ("signing-key", "trust.json"))
kept = {p.name: p.read_bytes() for p in config.iterdir()}
shown = tty("sortie setup", work, secret, prompts=0, note="a second run changes nothing")
assert "Nothing was changed" in shown and kept == {p.name: p.read_bytes() for p in config.iterdir()}, shown
looked = sh("sortie doctor", work, expected=1, note="after setup: the key and the trust file are found").stdout
assert "and the trust file names this key." in looked and "Run sortie setup" not in looked, looked
said("sortie doctor", WALK)
# The by-hand way is on the grants page, as the README says; its lines are not run here.
for line in ('mkdir -p "$HOME/.config/sortie-kit"', 'chmod 700 "$HOME/.config/sortie-kit"',
             'ssh-keygen -t ed25519 -C sortie-kit -f "$HOME/.config/sortie-kit/signing-key"', "sortie trust init"):
    said(line, GRANTS)

# Step 4: a repository and the direction file from the README.
repo.mkdir()
sh("git init -q .", repo, note="not a README command: the throwaway repository")
example = readme.split("named `direction.md`:\n\n   ```\n", 1)[1].split("   ```", 1)[0]
(work / "direction.md").write_text("\n".join(line.strip() for line in example.splitlines()) + "\n")

# The stand-in: a fixture agent profile that can take every seat, the reviewer's included.
fixture = agent_fixture(work, repo, home=home)
fixture["program"].write_text(AGENT.format(python=sys.executable, audit=str(audit)))
flags = "--agent driver=fixture:fixture-model --agent builder=fixture:fixture-model"
PICKED = "picked: the first installed agent of another family than the builder"
said(PICKED, WALK)
said(PICKED, WHO)

# No agent that can review: the start refuses before anything is prepared or signed.
profile = home / ".config/sortie-kit/agents/fixture.json"
whole = json.loads(profile.read_text())
profile.write_text(json.dumps({k: v for k, v in whole.items() if k not in ("review_argv", "review_model")}))
refused = tty("sortie start --repo project --direction direction.md --for 4h " + flags, work, None, expected=3,
              prompts=0, note="no installed agent can review: refused before anything is prepared or signed")
assert "review" in refused and not (repo / ".sortie").exists(), refused
profile.write_text(json.dumps(whole))

# Step 5: the start as the README gives it, every question answered in the terminal.
said("sortie start --repo PATH --direction direction.md")
text = tty("sortie start --repo project --direction direction.md", work, secret, prompts=1, answers=[
    (b"Driver agent:", b"fixture"), (b"Driver model:", b"1"), (b"Builder agent:", b"fixture"),
    (b"Builder model:", b"1"), (b"How long may the shift run:", b"45m")],
    note="PATH is the throwaway repository; fixture agent chosen for both seats, the reviewer picked")
assert "Review model" not in text and "Reviewer agent" not in text, text[-3000:]
for part in ("Using the trust file " + str(home / ".config/sortie-kit/trust.json"), "Shift to sign",
             "What the builder may do:", "Reviewer:   fixture, model fixture-review-model", PICKED,
             "What the reviewer can reach:", "driver fixture done", "Shift ended: driver exited", "Report: "):
    assert part in text, (part, text[-3000:])
assert text.index("How long may the shift run") < text.index("Shift to sign") < text.index("driver fixture done")
first = Path(text.split("Report: ", 1)[1].split("\n", 1)[0].strip())
assert first.name == "report.md" and first.is_file() and first.parent.parent == repo / ".sortie/shifts"
assert json.loads((audit / "driver.json").read_text())["tty"] == [True] * 3
assert json.loads((audit / "builder.json").read_text())["tty"] == [False] * 3
assert "/.sortie/" in (repo / ".git/info/exclude").read_text() and not (repo / ".gitignore").exists()

# The flag form: asked nothing but the passphrase.
said("sortie start --repo PATH --direction FILE --for 4h \\\n"
     "  --agent driver=claude:DRIVER_MODEL --agent builder=codex:BUILDER_MODEL\n```", RUNNING)
text = tty("sortie start --repo project --direction direction.md --for 4h \\\n  " + flags, work, secret, prompts=1,
           note="fixture:fixture-model in place of claude:DRIVER_MODEL and codex:BUILDER_MODEL")
assert "Driver agent" not in text and "Report: " in text, text[-3000:]

# The last lines say the tasks and the shutdown apart, as the walk-through's example of them does.
for part in ("Shift ended: ", "\nTasks: ", "\nShutdown: confirmed.\n", "\nDriver process: ", "\nReport: "):
    assert part in text, (part, text[-3000:])
for line in ("  Shutdown: confirmed.", "  Tasks: 1 accepted locally of 1 briefed (version at aed7b35); the driver says the direction held no more."):
    said(line, WALK)

# The complete example, line for line as its page has it, with three stated differences:
# SORTIE_KIT is the exported tree; its sortie setup is run in a disposable terminal, with the key
# made above kept and the trust file removed so that the command writes it again; the fixture agent stands in for claude and codex.
assert pages[EXAMPLE].startswith("# A complete example\n") and pages[EXAMPLE].count("```sh\n") == 1
block = pages[EXAMPLE].split("```sh\n", 1)[1].split("```", 1)[0]
commands = [c.replace("\\\n", "") for c in block.replace("\\\n", "\\\n\0").replace("\n\0", "\0").split("\n") if c.strip()]
commands = [c.replace("\0", "\n") for c in commands]
assert commands[0] == 'SORTIE_KIT="$HOME/dev/sortie-kit"' and commands[-1].startswith("venv/bin/sortie start "), commands
# The account has no Git identity and Git may not make one up from the user and host
# names: the example's commit has only the identity the example itself gives its repository.
assert not any(name.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_")) or name == "EMAIL" for name in env), sorted(env)
env.update(SORTIE_KIT=str(tree), GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="user.useConfigOnly", GIT_CONFIG_VALUE_0="true")
try_dir = home / "dev/sortie-try"
(home / ".config/sortie-kit/trust.json").unlink()  # so that the example's own sortie setup writes it
assert commands.count("venv/bin/sortie setup") == 1 and not any("ssh-keygen" in c or "trust init" in c for c in commands)
for command in commands[1:-1]:
    if command == "venv/bin/sortie setup":
        shown = tty(command, try_dir, secret, prompts=0, note="complete example; the key is kept, the trust file written")
        assert "was already there and was kept" in shown and "Trust file written" in shown, shown
        continue
    sh(command, try_dir if try_dir.is_dir() and not command.startswith("mkdir -p \"$HOME/dev") else work,
       note="complete example" + ("; cd is this run's working directory" if command.startswith("cd ") else ""))
assert (try_dir / "venv/bin/sortie").is_file() and (try_dir / "repo/hello.py").is_file()
assert (try_dir / "direction.md").read_text().count("\n") == 2, (try_dir / "direction.md").read_text()
start = commands[-1]
assert "--review" not in start, start
for real in ("driver=claude:claude-sonnet-5-5", "builder=codex:gpt-6.1-sol",
             '--trust "$HOME/.config/sortie-kit/trust.json"', '--key "$HOME/.config/sortie-kit/signing-key"', "--for 30m"):
    assert real in start, (real, start)
start = start.replace("driver=claude:claude-sonnet-5-5", "driver=fixture:fixture-model").replace(
    "builder=codex:gpt-6.1-sol", "builder=fixture:fixture-model")
text = tty(start, try_dir, secret, prompts=1,
           note="complete example; fixture:fixture-model in place of the claude and codex seats")
assert "Driver agent" not in text and "Shutdown: confirmed." in text and "Report: " in text, text[-3000:]

# sortie stop, as named in "What you will see" in both pages, on the shift that just ended.
said("sortie stop --shift SHIFT")
said("sortie stop --shift SHIFT", WALK)
assert "Withdrawal recorded" in sh("sortie stop --shift " + str(first.parent), work,
                                   note="on a shift that had already ended").stdout

# Preparation only.
said("sortie start --repo PATH --direction FILE --for 4h --prepare-only", RUNNING)
before = set((repo / ".sortie/shifts").iterdir())
assert "Prepared (unsigned, no agent started)" in sh(
    "sortie start --repo project --direction direction.md --for 4h --prepare-only", work).stdout
prepared, = set((repo / ".sortie/shifts").iterdir()) - before
assert (prepared / "grant.draft.json").is_file() and not (prepared / "grant.json").exists()

# The development command, from the checkout, with the venv's interpreter left out of it.
# The page's one command runs every test; here its narrowed form runs one module.
said("python3 tests/run_suites.py\n", DEVELOPMENT)
bare = dict(env, PATH=os.pathsep.join([str(tools), "/usr/bin", "/bin"]))
assert "sortie_kit" not in subprocess.run([str(tools / "python3"), "-c", "import importlib.util as u; "
                                           "print(u.find_spec('sortie_kit') and 'sortie_kit')"], env=bare, cwd=work,
                                          capture_output=True, text=True, timeout=60).stdout, "sortie_kit is installed here"
sh(said("python3 tests/run_suites.py test_report", DEVELOPMENT), tree, env=bare,
   note="run in the exported tree, by an interpreter that has no sortie_kit installed")

print(json.dumps(dict(work=str(work), commands=done), indent=1))
print("README RUN PASS")
