"""`sortie doctor`: what a `sortie start` on this machine would find, with no model call.

For each built-in agent profile, and for a profile of the person's own that is named,
it says whether the program is found, which file would be pinned, what version the
program prints, and whether each flag the profile passes appears in the program's own
help. Then who would review, and whether a signing key and a trust file are where a
start looks. It ends with the list of what to do.

It writes nothing, signs nothing, makes no shift and asks no model anything. It runs
each program that a start would pin twice or three times, with `--version` and with
`--help`, each call bounded and each through a keeper of its own (see `call`). What
it finds is found with the functions a start uses, so that the two cannot drift, and
it is no gate: a start checks everything again.

"listed" is the word for a flag whose name stands in the help text. That shows the
name is there. It does not show that the flag works or means what it meant.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import sysconfig
import time

from . import __version__, agents, grants, launcher, signing, trust_init
from .layout import Refused

BOUND = 10.0  # seconds one call of a program may take
GRACE = 1.0  # seconds a stopped program gets to end before it is killed
DRAIN = 0.2  # seconds without output after which the open pipes of an ended program are read no further
FAULT = (OSError, ValueError, RecursionError)  # a refusal, or a file that cannot be read as what it should be
KEPT = 256 * 1024  # bytes kept of each stream of one call; the rest is read and dropped
VERSION_SHOWN = 200
NOWHERE = Path("/dev/null/sortie-kit-no-repository")  # as setup.py: nothing can lie beneath it
FORMS = (("driver form (interactive)", "interactive_argv"), ("builder form (headless)", "headless_argv"),
         ("builder form with the network on", "headless_network_argv"), ("reviewer form (review)", "review_argv"))
LISTED = ('"listed" means that the flag\'s name appears in the CLI\'s own help text. It does not show that the flag '
          "works, or that it means what it meant. The values passed with a flag are not looked for.")
NOT_SHOWN = "Not shown here: that you know the key's passphrase, and that each CLI is signed in."
THEIRS = ("A CLI may look for an update or write a log of its own when it is asked for its version or its help; "
          "sortie-kit wrote nothing, signed nothing and made no shift.")
NOTHING = ("Nothing found to do. This does not show that a shift will run: no CLI was asked to do any work.")


def flags(argv):
    """The flags of one argument template, each once, in order: every argument that
    begins with `-`, an `--x=y` or `-x=y` as its name. A bare `-` or `--` is none,
    nothing after a `--` is looked at, and a `-` before a digit is a value."""
    found = []
    for item in argv:
        if item == "--":
            break
        if item in agents.PLACEHOLDERS or not item.startswith("-") or item == "-" or item[1:2].isdigit():
            continue
        name = item.split("=", 1)[0]
        if name.lstrip("-") and name not in found:
            found.append(name)
    return found


def words(argv):
    """The leading arguments that are neither flags nor placeholders: `exec` for codex."""
    found = []
    for item in argv:
        if item.startswith("-") or item in agents.PLACEHOLDERS:
            break
        found.append(item)
    return found


def listed(name, text):
    """Whether `name` stands in the help text as a whole word: not directly beside a
    letter, a digit, `-` or `_`."""
    return re.search(r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])", text) is not None


def environment(base=None):
    """The person's environment without the variables the host drops for a seat."""
    from .lifecycle import DROPPED
    return {k: v for k, v in (os.environ if base is None else base).items()
            if not any(k == name or (name.endswith("_") and k.startswith(name)) for name in DROPPED)}


KEEPER = r"""
import os, signal, subprocess, sys
signal.signal(signal.SIGTERM, lambda *_: None)
said = int(sys.argv[1])
try:
    child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL)
except OSError as exc:
    child, line = None, "error " + (exc.strerror or str(exc))
null = os.open(os.devnull, os.O_WRONLY)
os.dup2(null, 1)
os.dup2(null, 2)
if child is not None:
    line = "code " + str(child.wait())
os.write(said, line.encode("utf-8", "replace"))
os.close(said)
sys.stdin.buffer.read()
"""
# The keeper is this Python, started in a session of its own with the lines above. It starts
# the program in its own process group, lets go of the two streams, says through a pipe how
# the program ended and then stays until its standard input is closed. A request to end,
# which is sent to the whole group, it catches and passes over; the program it starts has
# the default for that signal again, as every started program has for a signal that was
# caught and not ignored.


def call(path, arguments, bound):
    """Run one program once, in `/`, with no terminal, in a session that is not the
    caller's, for at most `bound` seconds. Returns {"out", "err", "code", "stopped",
    "error", "cut"}: the two streams as text with at most KEPT bytes of each, the exit
    status or None, whether it was stopped at the bound, why it could not be started at
    all, and whether more was printed than is kept.

    The program is not a child of this process but of a keeper (see KEEPER), which
    leads the process group and is alive, and not reaped, from before the program
    starts until after the last signal. A signal is only ever sent to that group while
    the keeper has not been reaped: until then no other process can have its number,
    so no other group can. Nothing is signalled after it.

    The two pipes and the keeper's word are read from here, without a thread, until
    the program has ended and both pipes are closed or nothing more came for a
    moment, or the bound has passed. At the bound the group is told to end and then
    killed; it is killed at the end of every call, for what the program left running
    in it. A process the program put into a session of its own is not in that group
    and is not stopped; it cannot hold this call, which stops reading and closes its
    ends. Should the keeper itself have ended early, by another's doing, the signal
    may be refused and what is left of the group is then not stopped."""
    result = {"out": "", "err": "", "code": None, "stopped": False, "error": "", "cut": False}
    said_read, said_write = os.pipe()
    try:
        keeper = subprocess.Popen([sys.executable, "-I", "-S", "-c", KEEPER, str(said_write), path, *arguments],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/",
                                  env=environment(), start_new_session=True, bufsize=0, pass_fds=(said_write,))
    except OSError as exc:
        os.close(said_read)
        os.close(said_write)
        result["error"] = exc.strerror or str(exc)
        return result
    os.close(said_write)
    kept, sizes = {"out": [], "err": [], "said": []}, {"out": 0, "err": 0, "said": 0}
    open_ = {"out", "err", "said"}

    def end(sig):
        if keeper.returncode is not None:
            return  # reaped: the number is no longer this call's to signal
        try:
            os.killpg(keeper.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    watching = None
    try:
        watching = selectors.DefaultSelector()
        for fd, name in ((keeper.stdout.fileno(), "out"), (keeper.stderr.fileno(), "err"), (said_read, "said")):
            os.set_blocking(fd, False)
            watching.register(fd, selectors.EVENT_READ, name)
        deadline = time.monotonic() + bound

        def read(wait):
            """Take what is there within `wait` seconds; whether the program printed anything."""
            came = False
            for key, _ in watching.select(max(0, wait)):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    watching.unregister(key.fd)
                    open_.discard(key.data)
                    continue
                came = came or key.data != "said"
                if sizes[key.data] < KEPT:
                    kept[key.data].append(chunk[:KEPT - sizes[key.data]])
                sizes[key.data] += len(chunk)  # read on and dropped, so that the program is not held up
            return came

        quiet = None  # when the program had ended and its pipes were still open: read on only this long
        while open_:
            now = time.monotonic()
            if now >= deadline:
                break
            if "said" not in open_:
                # Ended, with a pipe still open: something it started holds it. What was
                # written is taken; what a stranger writes later is not waited for.
                quiet = now + DRAIN if quiet is None else quiet
                if now >= quiet:
                    break
            if read(min(0.05, deadline - now)) and quiet is not None:
                quiet = time.monotonic() + DRAIN
        if "said" in open_:
            result["stopped"] = True
            end(signal.SIGTERM)
            grace = time.monotonic() + GRACE
            while "said" in open_ and time.monotonic() < grace:
                read(min(0.05, grace - time.monotonic()))
        elif open_:
            read(0)
    finally:
        # The keeper has not been reaped, so the group's number is still this call's:
        # the one kill of the group, then the keeper is let go and reaped, and nothing
        # is signalled after that.
        end(signal.SIGKILL)
        for stream in (keeper.stdin, keeper.stdout, keeper.stderr):
            try:
                stream.close()
            except OSError:
                pass
        try:
            keeper.kill()  # itself only, should the group's kill have been refused; it looks first whether it ended
        except OSError:
            pass
        keeper.wait()
        if watching is not None:
            watching.close()
        os.close(said_read)
    word, _, rest = b"".join(kept["said"]).decode("utf-8", "replace").partition(" ")
    if word == "error":
        result["error"], result["stopped"] = rest or "it could not be started", False
    elif word == "code" and not result["stopped"]:
        try:
            result["code"] = int(rest)
        except ValueError:
            pass
    result["cut"] = sizes["out"] > KEPT or sizes["err"] > KEPT
    for name in ("out", "err"):
        result[name] = b"".join(kept[name]).decode("utf-8", "replace")
    return result


def first_line(*texts, limit=VERSION_SHOWN):
    for text in texts:
        for line in text.splitlines():
            if line.strip():
                return line.strip()[:limit]
    return ""


def seconds(bound):
    return format(bound, "g") + "s"


def ended(shown, result, bound):
    """How a call that gave nothing usable ended, in words."""
    if result["error"]:
        return shown + " could not be started: " + result["error"]
    if result["stopped"]:
        return shown + " did not end within " + seconds(bound) + " and was stopped"
    line = first_line(result["err"], result["out"])
    if result["code"] is None:
        return shown + " ended, and how was not seen"
    return shown + " ended with exit " + str(result["code"]) + (": " + line if line else "")


def said_once(todo, item):
    """One line for each thing to do: two forms of a profile often pass the same flag."""
    if item not in todo:
        todo.append(item)


def kind(pin):
    return ("a script: the pin covers that one file and not its interpreter or any program it starts"
            if agents.launcher(pin) else "not a script")


class Pins:
    """`agents.pin_executable`, with a file that was hashed in this run and is still the
    same file (device, inode, size and times) not read again."""
    def __init__(self):
        self.seen = {}

    def __call__(self, profile, repo, *, search=None):
        program = profile["value"]["executable"]
        found = program if os.path.isabs(program) else shutil.which(program, path=search)
        if found:
            path = os.path.realpath(found)
            try:
                held = self.seen.get((path, str(repo), agents.observe(path)))
            except OSError:
                held = None
            if held is not None:
                return held
        result = agents.pin_executable(profile, repo, search=search)
        self.seen[(result[0]["path"], str(repo), result[1])] = result
        return result


def block(name, profile, source, repo, search, bound, pins, todo):
    """The lines of one profile, and its pin when a start could pin its program."""
    value = profile["value"]
    built_in = source == "built-in"
    lines = [name + (" (built-in profile)" if built_in else " (your own profile, " + source + ")")]
    program = value["executable"]
    found = program if os.path.isabs(program) else shutil.which(program, path=search)
    if not found or not os.path.lexists(found):
        lines.append("  Program: " + program + (", not there" if os.path.isabs(program) else ", not found on your PATH"))
        if not built_in:  # a built-in CLI that is not installed is nothing to do; a profile that was named is
            todo.append(name + ": the program of the profile you named, " + program + ", is not found; a start that "
                        "names this profile would be refused.")
        return lines, None
    lines.append("  Program: " + program + ", found at " + found)
    try:
        pin, _ = pins(profile, repo, search=search)
    except FAULT as exc:
        # What a start would not pin, this command does not run either.
        lines.append("  sortie start would refuse this program: " + str(exc))
        todo.append(name + ": sortie start would refuse its program at " + found + " (" + str(exc) + ").")
        return lines, None
    lines.append("  File that would be pinned: " + pin["path"] + " (sha256 " + pin["sha256"][:12] + "..., "
                 + str(pin["bytes"]) + " bytes), " + kind(pin))
    if not built_in:
        lines.append("  This is your own program; sortie-kit runs it with --version and with --help and cannot know "
                     "what it does with them.")
    shown = os.path.basename(program)
    version = call(pin["path"], ["--version"], bound)
    line = first_line(version["out"], version["err"])
    if version["stopped"] or version["error"] or version["code"] is None:
        lines.append("  Version it prints: " + ended(shown + " --version", version, bound))
    elif version["code"] != 0 or not line:
        lines.append("  Version it prints: did not print one (" + shown + " --version ended with exit "
                     + str(version["code"]) + ")")
    else:
        lines.append("  Version it prints: " + line)
    helps, cut = {}, set()
    long = ("the help is longer than the " + str(KEPT // 1024) + " KiB that were read, and it is not in that part")

    def help_of(leading):
        """The help text asked for with these leading words, or None and why. A help
        of which more was printed than is kept is in `cut`: what is not in the part
        that was read is not known, and is never said to be missing."""
        key = tuple(leading)
        if key not in helps:
            asked = " ".join([shown, *key, "--help"])
            result = call(pin["path"], [*key, "--help"], bound)
            if result["code"] == 0 and (result["out"] + result["err"]).strip():
                helps[key] = (result["out"] + "\n" + result["err"], "")
                if result["cut"]:
                    cut.add(key)
            else:
                helps[key] = (None, asked + " printed nothing" if result["code"] == 0 else ended(asked, result, bound))
                said_once(todo, name + ": its help could not be read (" + helps[key][1] + "), so its flags are not "
                            "known. Run " + asked + " yourself to see why.")
        return helps[key]

    def unread(leading):
        asked = " ".join([shown, *leading, "--help"])
        return (name + ": its help is longer than the " + str(KEPT // 1024) + " KiB that were read, so a flag or "
                "subcommand that is not in that part is not known. Run " + asked + " yourself to look for it.")

    for title, field in FORMS:
        argv = value.get(field)
        if argv is None:
            continue
        leading = words(argv) if built_in else []
        skipped = [] if built_in else words(argv)
        said = []
        text, why, missing = None, "", False
        # Each leading word is looked for in the help of what stands before it. The help of a
        # word that is not listed there is not asked for: the program might take the word as
        # something else, a prompt for one.
        for index, word in enumerate(leading):
            text, why = help_of(leading[:index])
            if text is None:
                break
            if listed(word, text):
                said.append("subcommand " + word + " listed")
            elif tuple(leading[:index]) in cut:
                missing = True
                said_once(todo, unread(leading[:index]))
                lines.append("  " + title + ": " + ", ".join(said + ["subcommand " + word + " not known (" + long
                                                                    + "); the flags after it were not looked for"]))
                break
            else:
                missing = True
                said_once(todo, name + ": the subcommand " + word + " is not found in the help of "
                          + " ".join([shown, *leading[:index]]) + ". The profile starts the "
                          + title.split(" form")[0] + " with it; a start may fail there.")
                lines.append("  " + title + ": " + ", ".join(said + ["subcommand " + word + " not found in the help; the "
                                                                    "flags after it were not looked for"]))
                break
        else:
            text, why = help_of(leading)
        if missing:
            continue
        if text is None:
            lines.append("  " + title + ": " + (why + "; its flags are not known" if why.endswith("was stopped")
                                                else "help not known (" + why + ")"))
            continue
        for flag in flags(argv):
            if listed(flag, text):
                said.append(flag + " listed")
            elif skipped:
                said.append(flag + ' not known from the top help (the help of "' + " ".join(skipped)
                            + '" was not asked for)')
            elif tuple(leading) in cut:
                said.append(flag + " not known (" + long + ")")
                said_once(todo, unread(leading))
            else:
                said.append(flag + " not found in the help")
                said_once(todo, name + ": " + flag + " is not found in the help of " + " ".join([shown, *leading])
                            + ". The profile passes it to the " + title.split(" form")[0] + "; a start may fail "
                            "there. docs/agents.md says how to write a profile of your own.")
        lines.append("  " + title + ": " + (", ".join(said) if said else "no flag is passed"))
    return lines, pin


def families(profile):
    """The families a builder of this profile can be of, canonical, each once."""
    value = profile["value"]
    found = []
    for name in (*value["models"].values(), value.get("unlisted_family")):
        if name is None:
            continue
        try:
            name = grants.family(name)
        except ValueError:
            continue
        if name not in found:
            found.append(name)
    return found


def review_lines(builders, repo, home, search, pins, todo):
    lines = []
    try:
        able = launcher.reviewers(repo, home=home, search=search)
    except FAULT:
        able = []
    if not able:
        # As a start finds it out: before anything is asked, and whoever the builder is.
        todo.append("Nobody would review: install and sign in to an agent CLI that can review, or name a profile "
                    "of your own that has a review form.")
        return [str(launcher.no_reviewer(repo, home=home)).split("; nothing was prepared")[0] + "."]
    for name, profile in builders:
        mine = families(profile)
        if not mine:
            lines.append(name + " as the builder: its family is whatever you state with --family; who would review "
                         "it is not shown here.")
        for family in mine:
            head = name + " as the builder (family " + family + ")"
            try:
                seat, choice = launcher.pick_reviewer(repo, {"agent": name, "family": family}, home=home,
                                                      search=search, pin=pins)
            except FAULT as exc:
                lines.append(head + ": sortie start would refuse: " + str(exc))
                said_once(todo, "The reviewer that would be picked for a " + name + " builder of family " + family
                          + " cannot be started (" + str(exc) + ").")
                continue
            lines.append(head + " would be reviewed by " + seat["agent"] + ", " + seat["model"] + " (family "
                         + seat["family"] + "): " + ("another family." if choice == "picked-other-family" else
                                                     "the same family, since no installed agent of another family "
                                                     "can review."))
    return lines


def key_lines(repo, home, todo):
    key, trust = trust_init.default_key(home), trust_init.default_trust(home)
    have_key, have_trust = os.path.lexists(key), os.path.lexists(trust)
    if not have_key and not have_trust:
        todo.append("Run sortie setup once in your own terminal: it makes the signing key and the trust file.")
        return ["Signing key and trust file: none at " + str(key) + " and " + str(trust) + ". sortie setup makes both."]
    if have_key and not have_trust:
        try:
            grants.external_file(key, repo)
        except FAULT as exc:
            todo.append("What is at " + str(key) + " is not a key sortie start would take (" + str(exc) + "), and "
                        "there is no trust file. docs/grants.md says what a start requires of the two files.")
            return ["Signing key: at " + str(key) + ", but sortie start would refuse it: " + str(exc) + ". Trust "
                    "file: none at " + str(trust) + "."]
        todo.append("Run sortie setup in your own terminal: it checks the key that is there and writes the trust "
                    "file for it, and replaces nothing. Or give sortie start a trust file with --trust.")
        return ["Signing key: found at " + str(key) + ". Trust file: none at " + str(trust) + "."]
    if not have_key:
        try:
            grants.Trust(trust, repo)
        except FAULT as exc:
            todo.append("What is at " + str(trust) + " is not a trust file sortie start would take (" + str(exc)
                        + "), and there is no key. docs/grants.md says what a start requires of the two files.")
            return ["Trust file: at " + str(trust) + ", but sortie start would refuse it: " + str(exc) + ". Signing "
                    "key: none at " + str(key) + "."]
        todo.append("Put the key that the trust file names at " + str(key) + ", or give it to sortie start with "
                    "--key. sortie setup makes no new key beside a trust file, which would name another.")
        return ["Trust file: found at " + str(trust) + ". Signing key: none at " + str(key) + "."]
    try:
        # The three checks a start makes of the two files, and no other.
        checked = grants.Trust(trust, repo)
        _, raw, _ = grants.external_file(key, repo)
        signing.encrypted_key(raw, checked.key_blob)
    except FAULT as exc:
        todo.append("The signing key and the trust file are there, but sortie start would refuse them (" + str(exc)
                    + "). docs/grants.md says what a start requires of the two files.")
        return ["Signing key and trust file: at " + str(key) + " and " + str(trust) + ", but sortie start would refuse "
                "them: " + str(exc)]
    return ["Signing key and trust file: found at " + str(key) + " and " + str(trust) + ", and the trust file names "
            "this key."]


def installed_scripts():
    """Where this installation's `sortie` script would be: the scripts directory of the
    running Python, and that of its user scheme. The interpreter's own path is not
    resolved, which would leave a virtual environment."""
    found = []
    for scheme in (None, getattr(sysconfig, "get_preferred_scheme", lambda kind: None)("user")):
        try:
            where = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
        except KeyError:
            continue
        if where and where not in found:
            found.append(where)
    return found


def path_line(search, scripts):
    first = shutil.which("sortie", path=search)
    if not first:
        return "The sortie first on your PATH: none."
    mine = False
    for where in installed_scripts() if scripts is None else scripts:
        try:
            mine = mine or os.path.samefile(first, os.path.join(where, "sortie"))
        except OSError:
            pass
    return ("The sortie first on your PATH: " + first + (" (this installation)." if mine else
            " (another file than this installation's; `type sortie` shows what your shell runs)."))


def run(names=(), repo=None, *, home=None, search=None, bound=BOUND, scripts=None):
    """The report as lines, and the exit code: 0 when nothing was found to do, 1 when
    something was, or something could not be found out. `names` are profiles of the
    person's own to look at beside the built-in ones; `repo` is the repository a
    shift would be for, or None. `search` stands for PATH and `scripts` for the
    scripts directories of this installation."""
    held = NOWHERE if repo is None else Path(repo)
    todo, pins = [], Pins()
    lines = ["sortie-kit " + __version__ + ", Python " + sys.version.split()[0] + ", running from "
             + os.path.dirname(os.path.abspath(__file__)),
             path_line(search, scripts)]
    for tool in ("git", "ssh-keygen"):
        found = shutil.which(tool, path=search)
        lines.append(tool + ": " + ("found at " + found if found else "not found on your PATH"))
        if not found:
            todo.append("Install " + tool + ": sortie start needs it and it is not found on your PATH.")
    if repo is None:
        lines.append("No repository was named with --repo, so nothing was held to lying outside one: a start "
                     "requires that of a profile of your own, of each agent program, of the key and the trust file, "
                     "and of the signing tool.")
    lines.append("")
    usable = []  # (name, profile) of each profile whose program a start could pin
    for name in dict.fromkeys([*agents.REVIEW_ORDER, *names]):
        try:
            profile = agents.load(name, held, home=home)
        except FAULT as exc:
            built_in = name in agents.REVIEW_ORDER
            lines += [name + ": " + ("sortie start would refuse this profile: " if built_in else "") + str(exc), ""]
            todo.append(name + ": " + ("its profile is refused (" + str(exc) + "); move your own file of that name "
                                       "away." if built_in else "the profile you named does not load (" + str(exc)
                                       + "). Profiles of your own are in " + str(agents.user_directory(home)) + "."))
            continue
        said, pin = block(name, profile, profile["source"], held, search, bound, pins, todo)
        lines += said + [""]
        if pin is not None:
            usable.append((name, profile))
    lines.append(LISTED)
    lines.append("")
    builders = [(name, profile) for name, profile in usable if profile["value"].get("headless_argv") is not None]
    drivers = [name for name, profile in usable if profile["value"].get("interactive_argv") is not None]
    lines.append("Who would review:")
    if not usable:
        lines.append("No agent CLI was found, so nobody would build or review.")
        todo.append("Install and sign in to an agent CLI: none of " + ", ".join(agents.REVIEW_ORDER) + " was found "
                    "that a start could use, and a shift needs a driver, a builder and a reviewer. docs/agents.md "
                    "says how to name another CLI in a profile of your own.")
    else:
        if not drivers:
            todo.append("No profile that was found has an interactive form, so nothing could be the driver.")
        if not builders:
            todo.append("No profile that was found has a headless form, so nothing could be the builder.")
        lines += review_lines(builders, held, home, search, pins, todo)
    lines.append("")
    lines += key_lines(held, home, todo)
    lines += [NOT_SHOWN, "", THEIRS, ""]
    if todo:
        lines.append("To do before a shift:")
        lines += ["- " + item for item in todo]
    else:
        lines.append(NOTHING)
    return lines, 1 if todo else 0
