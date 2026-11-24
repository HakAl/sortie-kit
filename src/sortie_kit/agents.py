"""Agent profiles and executable pins. Nothing here executes an agent.

A profile says how one agent CLI is started: its executable, argument templates
for an interactive run, a headless run and, where the CLI can review, a
read-only headless run, and which model belongs to which family. A profile may
also have a second headless form, with the network on, which a builder is
started with only when the start asks for it.
The signed grant binds what these functions return, so a seat is later started
only from signed values. A seat runs under the developer's own configuration of
that CLI; the toolkit supplies, fills and pins no permission file.
"""
from __future__ import annotations

import hashlib
from importlib.resources import files
import os
from pathlib import Path
import re
import shutil
import stat

from .grants import MODEL, FAMILY, decoded, family as canonical_family
from .layout import Refused

NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
FIELDS = {"version", "name", "executable", "interactive_argv", "headless_argv", "models", "default_model",
          "unlisted_family", "review_argv", "review_model", "headless_network_argv"}
OPTIONAL = {"default_model", "unlisted_family", "review_argv", "review_model", "headless_network_argv"}
PLACEHOLDERS = ("{prompt}", "{model}", "{cwd}")
MODES = {"driver": "interactive", "builder": "headless", "reviewer": "review"}
REVIEW_ORDER = ("claude", "codex", "agy")  # the built-in profiles, in the order a reviewer is picked from them
PROFILE_LIMIT = 64 * 1024
EXECUTABLE_LIMIT = 1024 * 1024 * 1024
SEAT = {"agent", "profile_sha256", "argv", "model", "family", "mode", "executable"}


def user_directory(home=None):
    return Path(home or Path.home()) / ".config" / "sortie-kit" / "agents"


def template_checked(argv):
    """Arguments only: argv[0] is always the pinned executable, never template text."""
    if argv is None:
        return None
    if (not isinstance(argv, list) or not 2 <= len(argv) <= 64
            or any(not isinstance(a, str) or not a or len(a) > 512 or "\x00" in a for a in argv)):
        raise Refused("argv template must be a short list of nonempty strings")
    for item in argv:
        # Whole-element placeholders only, so a substituted value can never be
        # split into, or merged with, another argument or option.
        if item not in PLACEHOLDERS and any(p in item for p in PLACEHOLDERS):
            raise Refused("a placeholder must be a whole argument: " + item)
    if argv.count("{prompt}") != 1 or argv.count("{model}") != 1:
        raise Refused("argv template needs {prompt} and {model} exactly once")
    if argv.count("{cwd}") > 1:
        raise Refused("argv template may name {cwd} at most once")
    return list(argv)


def profile_checked(value, name=None):
    if not isinstance(value, dict) or set(value) | OPTIONAL != FIELDS:
        raise Refused("unsupported agent profile fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise Refused("unsupported agent profile version")
    if not isinstance(value["name"], str) or not NAME.fullmatch(value["name"]):
        raise Refused("agent name must be a short lower-case token")
    if name is not None and value["name"] != name:
        raise Refused("agent profile name differs from its file name")
    program = value["executable"]
    if (not isinstance(program, str) or not program or len(program) > 1024 or "\x00" in program
            or program.startswith("-") or ("/" in program and not os.path.isabs(program))):
        raise Refused("profile executable must be a command name or an absolute path")
    for key in ("interactive_argv", "headless_argv"):
        template_checked(value[key])
    # Optional; absent and null both mean the profile has no headless form with the
    # network on. It is a whole second template, so that what is signed with it is
    # written down in one place, and it has to differ from the first: a start that
    # asks for the network must never sign the same command as one that does not.
    wide = template_checked(value.get("headless_network_argv"))
    if wide is not None and (value["headless_argv"] is None or wide == value["headless_argv"]):
        raise Refused("profile headless_network_argv needs a headless_argv and must differ from it")
    models = value["models"]
    if not isinstance(models, dict) or len(models) > 256:
        raise Refused("profile models must map model IDs to families")
    for model, kind in models.items():
        if not MODEL.fullmatch(model) or not isinstance(kind, str) or not FAMILY.fullmatch(kind):
            raise Refused("invalid model or family in agent profile")
    default = value.get("default_model")  # optional; absent and null both mean no default
    if default is not None and (not isinstance(default, str) or default not in models):
        raise Refused("profile default model must be one of its listed models")
    other = value.get("unlisted_family")  # optional: the family of any model the profile does not list
    if other is not None and (not isinstance(other, str) or not FAMILY.fullmatch(other)):
        raise Refused("profile unlisted_family must be a family token")
    # Optional, and only together: the read-only headless form a reviewer is started
    # with, and the model a reviewer picked without a named model runs with.
    form, chosen = template_checked(value.get("review_argv")), value.get("review_model")
    if (form is None) != (chosen is None):
        raise Refused("profile review_argv and review_model go together: a profile that can review names both")
    if chosen is not None and (not isinstance(chosen, str) or not MODEL.fullmatch(chosen)
                               or (chosen not in models and other is None)):
        raise Refused("profile review_model must be a model ID that the profile lists or that unlisted_family covers")
    return value


def can_review(profile):
    """Whether the profile has a review form (and so a review model)."""
    return profile["value"].get("review_argv") is not None


def read_external(path, repo, limit, what):
    """Developer-owned input: regular, no symlink, outside the repo, writable only by its owner."""
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise Refused(what + " path contains a symlink")
    if path.resolve().is_relative_to(Path(repo).resolve()):
        raise Refused(what + " must be outside the target repo")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise Refused(what + " must be a bounded regular file")
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Refused(what + " must be yours and not group or world writable")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise Refused(what + " is too large")
    return path, raw


def load(name, repo, *, home=None):
    """A developer's own profile or a packaged one; a custom file never shadows a built-in name."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused("agent name must be a short lower-case token")
    packaged = files("sortie_kit").joinpath("data", "agents", name + ".json")
    custom = user_directory(home) / (name + ".json")
    if packaged.is_file():
        if os.path.lexists(custom):
            raise Refused("custom agent profile reuses a built-in name: " + name)
        raw, source = packaged.read_bytes(), "built-in"
    elif os.path.lexists(custom):
        path, raw = read_external(custom, repo, PROFILE_LIMIT, "agent profile")
        source = str(path)
    else:
        raise Refused("no agent profile named " + name)
    value = profile_checked(decoded(raw), name)
    return {"value": value, "sha256": hashlib.sha256(raw).hexdigest(), "source": source}


def available(repo, *, home=None):
    """Names only, for guided choices; each is validated when it is chosen."""
    names = {p.name[:-5] for p in files("sortie_kit").joinpath("data", "agents").iterdir()
             if p.name.endswith(".json")}
    try:
        names |= {p.name[:-5] for p in user_directory(home).iterdir() if p.name.endswith(".json")}
    except OSError:
        pass
    return sorted(n for n in names if NAME.fullmatch(n))


def choose_model(profile, model=None, family=None):
    """The model is always named. An unlisted one takes the family the profile gives
    for such models, or needs its family stated. A stated family decides only where
    the profile says nothing: it may contradict neither the family of a listed model
    nor the family the profile gives for every model it does not list. Otherwise a
    word on the command line could make a reviewer of the builder's own family pass
    as one of another."""
    value = profile["value"]
    model = model or value.get("default_model")
    if model is None:
        raise Refused("name a model for agent " + value["name"] + "; none is assumed")
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        raise Refused("invalid model ID")
    listed = value["models"].get(model)
    if family is None:
        listed = listed or value.get("unlisted_family")
        if listed is None:
            raise Refused("model " + model + " is not in the " + value["name"]
                          + " profile; state its family explicitly")
        return model, canonical_family(listed)
    family = canonical_family(family)
    if listed is not None and canonical_family(listed) != family:
        raise Refused("stated family contradicts the profile for model " + model)
    other = value.get("unlisted_family")
    if listed is None and other is not None and canonical_family(other) != family:
        raise Refused("stated family contradicts the " + value["name"] + " profile, which places every model it "
                      "does not list in the family " + canonical_family(other) + "; a CLI that runs models of "
                      "another family needs a profile of your own without unlisted_family")
    return model, family


def observe(path):
    info = os.stat(path, follow_symlinks=False)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def hash_executable(path, repo):
    """Pin one program file by content. The program is never run to identify it."""
    path = Path(path)
    if not path.is_absolute() or path != Path(os.path.realpath(path)):
        raise Refused("agent executable must be a fully resolved absolute path")
    if path.is_relative_to(Path(repo).resolve()):
        raise Refused("agent executable must be outside the target repo")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > EXECUTABLE_LIMIT:
            raise Refused("agent executable must be a bounded regular file")
        # Executable by this caller, not merely carrying some execute bit.
        if info.st_mode & 0o022 or not os.access(path, os.X_OK):
            raise Refused("agent executable must be executable by you and not group or world writable")
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > EXECUTABLE_LIMIT:
                raise Refused("agent executable changed size while hashing")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    pin = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if pin(info) != pin(after) or size != info.st_size or observe(path) != pin(info):
        raise Refused("agent executable changed while hashing")
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": size}, pin(info)


def pin_executable(profile, repo, *, search=None):
    """Resolve the profile's program once, at signing, to the real file that is pinned.

    A launcher script pins only itself, not whatever it starts.
    """
    program = profile["value"]["executable"]
    found = program if os.path.isabs(program) else shutil.which(program, path=search)
    if not found:
        raise Refused("agent executable not found: " + program)
    return hash_executable(os.path.realpath(found), repo)


def check_executable(pin, repo):
    """Admission-time recheck of the signed pin; returns the observation to compare before each spawn."""
    current, seen = hash_executable(pin["path"], repo)
    if current != pin:
        raise Refused("agent executable differs from the signed pin")
    return seen


def network_argv(profile, model=None, cwd=None):
    """The profile's headless form with the network on, for a builder whose start asked
    for it (`sortie start --builder-network`). A profile without one refuses: the
    request is never taken and dropped. Two templates that differ can still be one
    command once the model, or the repository's path, stands in its place (a literal
    in one where the other has the placeholder), so with `model`, and `cwd` where it
    is known, the two are compared as they would be started, and refused when they
    are the same: the flag would be signed and would change nothing."""
    argv = profile["value"].get("headless_network_argv")
    if argv is None:
        raise Refused("--builder-network was given, but the builder profile " + profile["value"]["name"]
                      + " has no form with the network on (a profile has one when it has headless_network_argv, "
                      "as the built-in codex profile does); nothing was prepared or signed")
    fill = {**({} if model is None else {"{model}": model}), **({} if cwd is None else {"{cwd}": str(cwd)})}
    plain = profile["value"].get("headless_argv") or []
    if fill and [fill.get(a, a) for a in argv] == [fill.get(a, a) for a in plain]:
        raise Refused("--builder-network was given, but " + ("in this repository" if model is None else "with model "
                      + str(model)) + " the builder profile " + profile["value"]["name"] + "'s form with the network "
                      "on is the same command as its form without; nothing was prepared or signed")
    return argv


def seat(role, profile, model, family, executable, *, network=False):
    """The signed description of one seat. `network` asks for the builder's headless
    form with the network on; the seat's fields are the same either way, and the
    signed argv is what shows which it is."""
    mode = MODES[role]
    argv = profile["value"].get(mode + "_argv")
    if argv is None:
        raise Refused("agent " + profile["value"]["name"] + " has no " + mode
                      + " start and cannot be the " + role)
    if network:
        if role != "builder":
            raise Refused("only the builder has a form with the network on, not the " + role)
        argv = network_argv(profile, model)
    return seat_checked(role, {
        "agent": profile["value"]["name"], "profile_sha256": profile["sha256"], "argv": list(argv),
        "model": model, "family": family, "mode": mode, "executable": executable})


def seat_checked(role, value):
    if role not in MODES or not isinstance(value, dict) or set(value) != SEAT:
        raise Refused("invalid seat scope")
    program = value["executable"]
    if (not isinstance(value["agent"], str) or not NAME.fullmatch(value["agent"])
            or not isinstance(value["profile_sha256"], str) or not SHA.fullmatch(value["profile_sha256"])
            or not isinstance(value["model"], str) or not MODEL.fullmatch(value["model"])
            or not isinstance(value["family"], str) or not FAMILY.fullmatch(value["family"])
            or value["mode"] != MODES[role]
            or not isinstance(program, dict) or set(program) != {"path", "sha256", "bytes"}
            or not isinstance(program["path"], str) or not os.path.isabs(program["path"])
            or "\x00" in program["path"] or len(program["path"]) > 4096
            or not isinstance(program["sha256"], str) or not SHA.fullmatch(program["sha256"])
            or type(program["bytes"]) is not int or not 0 < program["bytes"] <= EXECUTABLE_LIMIT
            or value["argv"] is None):  # only a profile may leave a mode's template unset
        raise Refused("invalid seat scope")
    template_checked(value["argv"])
    return value


def render_argv(value, *, prompt, cwd):
    """One simultaneous whole-element pass from signed fields; no shell is involved."""
    if (not isinstance(prompt, str) or not prompt or prompt.startswith("-") or "\x00" in prompt
            or len(prompt) > 8192):
        raise Refused("host prompt must be bounded text that cannot be read as an option")
    if not os.path.isabs(str(cwd)) or "\x00" in str(cwd):
        raise Refused("seat working directory must be absolute")
    fill = {"{prompt}": prompt, "{model}": value["model"], "{cwd}": str(cwd)}
    return [value["executable"]["path"]] + [fill.get(a, a) for a in value["argv"]]


def shown_command(value, cwd):
    """The seat's command line for a person to read: signed values, with the prompt left as a marker."""
    fill = {"{prompt}": "PROMPT", "{model}": value["model"], "{cwd}": str(cwd)}
    return [value["executable"]["path"]] + [fill.get(a, a) for a in value["argv"]]


CODEX_NETWORK = ("-c", "sandbox_workspace_write.network_access=true")
CODEX_REACH = (
    (("--sandbox", "workspace-write"), "write anywhere in the repository"),
    (("--add-dir", ".git"), "write in .git too: commits, and also Git hooks and configuration, which later run as you"),
    (CODEX_NETWORK, "use the network"),
)
# What is said of the built-in codex builder's network, with the option and without it.
# Both are what the command line asks of the CLI. Without the option the developer's own
# codex configuration decides, and the CLI's connection to its model is outside the sandbox.
CODEX_NETWORK_ON = (" ".join(CODEX_NETWORK) + ": the builder can use the network in the commands it runs. "
                    "That is what the option asks of the CLI; sortie-kit has not checked it")
CODEX_NETWORK_OFF = ("no network option is passed: unless your own codex configuration turns it on, the builder "
                     "cannot use the network in the commands it runs (codex itself still reaches its model). "
                     "That is how the CLI runs this sandbox; sortie-kit does not enforce it")


AGY_HEADLESS = ("--print", "{prompt}", "--model", "{model}")  # the built-in agy builder's form


REVIEW_REACH = {
    "codex": (("--sandbox", "read-only"), "commands it runs cannot write files"),
    "claude": (("--permission-mode", "plan"), "the CLI's plan mode, meant for reading; edits and commands are not approved"),
    "agy": (("--mode", "plan", "--sandbox"), "the CLI's plan mode, with its terminal restrictions on"),
}


def has_run(argv, wanted):
    return any(tuple(argv[i:i + len(wanted)]) == tuple(wanted) for i in range(len(argv) - len(wanted) + 1))


def cli_name(value):
    """Which known CLI a signed reviewer seat starts, or None. The profile's name says it
    for a built-in profile; for a profile of the developer's own, the name of the pinned
    program does. The pin is the resolved file, which a CLI may name by its version
    (claude installs as .../claude/versions/N), so that name alone would not do.
    Either way it is a name and no proof of what the program is: what is said from
    it is what one run of arguments asks of that CLI, and nothing about the rest."""
    for name in (value["agent"], os.path.basename(value["executable"]["path"])):
        if name in REVIEW_REACH:
            return name
    return None


def reach(role, value):
    """Plain words on what a signed seat is started to do. Describes the signed
    command line only; the developer's own configuration of that CLI is not read."""
    name, argv = value["agent"], list(value["argv"])
    lines = []
    if value["mode"] == "review":
        wanted, meaning = REVIEW_REACH.get(cli_name(value), ((), ""))
        if wanted and has_run(argv, wanted):
            lines.append(" ".join(wanted) + ": " + meaning + ". That is what the option asks of the CLI; "
                         "sortie-kit has not checked what it stops")
            if name not in REVIEW_REACH:  # another argument beside it may undo it
                lines.append("a profile of your own: sortie-kit has not inspected what its other arguments allow")
        else:
            lines.append("sortie-kit has not inspected what these arguments allow; this reviewer may be able to write")
        lines.append("it runs as you, with your sign-in for " + name + ", in a directory under the shift that holds "
                     "a copy of the work and the review instructions")
        lines.append("sortie-kit does not confine it: it can read whatever your user can read and use the network "
                     "as its CLI allows")
        lines.append("a change to the repository or to the shift's receipts/ during a review fails that review")
        return lines
    # A builder. What a built-in profile does not pass is known and said. Of a profile of
    # the developer's own only the options found are said: its other arguments were not
    # read, so nothing is said of what it leaves out.
    codex = [" ".join(pair) + ": " + meaning for pair, meaning in CODEX_REACH if has_run(argv, pair)]
    if value["mode"] == "headless" and name == "codex":
        # The built-in profile: what it passes is known, so the network is said either way
        # of a seat in the workspace-write sandbox. Without that sandbox option nothing is
        # known of the network.
        lines += [CODEX_NETWORK_ON if pair == CODEX_NETWORK else " ".join(pair) + ": " + meaning
                  for pair, meaning in CODEX_REACH if has_run(argv, pair)]
        if has_run(argv, CODEX_REACH[0][0]) and not has_run(argv, CODEX_NETWORK):
            lines.append(CODEX_NETWORK_OFF)
        lines.append("reading is not limited by any of these: it can read whatever your user can read" if lines else
                     "no sandbox option is passed: what it can write is whatever your own codex configuration says")
    elif value["mode"] == "headless" and name == "claude":
        lines.append("no permission option is passed: it can do only what your Claude settings allow without asking")
    elif value["mode"] == "headless" and name == "agy":
        # The built-in profile's headless form is --print, the prompt and the model: it names
        # no mode and no sandbox, so what the builder may do unasked is agy's own configuration's
        # to say. That is said of this form alone; of any other arguments under the name, as a
        # later version's profile might sign, nothing is known here.
        lines.append("no permission, mode or sandbox option is passed: it can do whatever your own agy "
                     "configuration allows without asking" if argv == list(AGY_HEADLESS) else
                     "sortie-kit has not inspected what these arguments allow")
    elif value["mode"] == "headless" and codex and os.path.basename(value["executable"]["path"]) == "codex":
        lines += codex
        lines.append("a profile of your own: sortie-kit has not inspected what its other arguments allow")
    elif value["mode"] == "headless":
        lines.append("a profile of your own: sortie-kit has not inspected what these arguments allow")
    lines.append("anything your own " + name + " configuration allows on top still applies; sortie-kit does not read or limit it")
    return lines
